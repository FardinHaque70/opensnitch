"""Live session behavior using temporary files and a fake daemon only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path
from queue import Queue, Empty
import json
import tempfile
import time
import unittest

from opensnitch.application_policy import ApplicationRule
from opensnitch.core.protocol import to_proto
from opensnitch.core.session import ApplicationSession
from opensnitch.core.history import HistoryReader
from opensnitch.core.transport import pb
from tests.test_transport import FakeDaemon


class Timer:
    def start(self): pass
    def stop(self): pass


class TestLiveSession(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='opensnitch-session-test-')
        self.root = Path(self.temp.name)
        self.rules = self.root / 'rules'; self.rules.mkdir()
        self.binary = self.root / 'example'; self.binary.write_text('')
        self.key = ('unix:/local', str(self.binary))
        self.rule = ApplicationRule('existing', 'allow', 'always', True, False,
            dict(type='simple', operand='process.path', data=str(self.binary), sensitive=True, list=[]), int(time.time()))
        (self.rules / 'existing.json').write_text('original rule fixture')
        (self.root / 'settings.conf').write_text('[database]\ntype=1\nfile=' + str(self.root / 'history.db') + '\n')
        self.events = Queue()
        self.model = ApplicationSession(self.root, Timer(), lambda fn, *args: self.events.put((fn, args)))
        self.model.accept_index(({str(self.binary): ('Example', '', '', '')}, set(), (), {}))
        self.address = 'unix://' + str(self.root / 'ui.sock')
        self.model.start(self.address, lambda prompt: None)
        self.daemon = FakeDaemon(self.address)
        self.readers = ThreadPoolExecutor(max_workers=2)
        self.connect()

    def tearDown(self):
        self.daemon.close()
        self.model.close()
        self.readers.shutdown(wait=False, cancel_futures=True)
        self.temp.cleanup()

    def pump(self, predicate=lambda: False, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError('Session fixture timeout')
            try:
                fn, args = self.events.get(timeout=.01)
            except Empty:
                continue
            fn(*args)

    def connect(self, rules=None):
        self.serial = getattr(self, 'serial', 0) + 1
        configuration = pb.ClientConfig(id=self.serial, name='Fixture computer', isFirewallRunning=True,
            config=json.dumps({'Rules': {'Path': str(self.rules)}}),
            rules=[to_proto(rule, pb.Rule) for rule in ([self.rule] if rules is None else rules)])
        self.daemon.stub.Subscribe(configuration, timeout=2)
        self.stream = self.daemon.open_stream()
        self.incoming = self.readers.submit(next, self.stream)
        self.pump(lambda: self.model.online(self.key[0]) and self.model.nodes[self.key[0]].id == self.serial)

    def row(self):
        return next(row for row in self.model.snapshot.applications if row.key == self.key)

    def apply(self, success=True):
        notification = self.incoming.result(timeout=2)
        if success:
            for rule in notification.rules:
                path = self.rules / (rule.name + '.json')
                if notification.type == pb.DELETE_RULE:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(rule.SerializeToString().hex())
        self.daemon.replies.put(pb.NotificationReply(id=notification.id, code=pb.OK if success else pb.ERROR, data='' if success else 'Fixture rejection'))
        self.pump(lambda: not self.model.controller.pending)
        return notification

    def test_disabled_temporary_rule_is_not_treated_as_active_expiry(self):
        self.model.policies.replace_rules(self.key[0], [replace(self.rule, enabled=False, duration='1h', created=0)])
        self.model.tick()
        self.assertTrue(self.model.online(self.key[0]))
        self.assertNotIn(self.key[0], self.model._stale)

    def test_fractional_expiry_rechecks_until_daemon_removal_is_confirmed(self):
        expired = replace(self.rule, duration='1ms', created=0)
        self.model.policies.replace_rules(self.key[0], [expired])
        self.model.tick()
        self.pump(lambda: not self.model.online(self.key[0]))
        self.connect(rules=[expired])
        with patch('opensnitch.core.session.time.monotonic', return_value=time.monotonic() + 2):
            self.model.tick()
        self.assertNotIn(self.key[0], self.model.transport.sessions)

    def test_authoritative_rules_identity_and_pending_ack(self):
        self.assertEqual(self.row().policy, 'Allowed')
        self.assertEqual(self.row().identity.display_name, 'Example')
        self.assertTrue(self.model.request_policy(self.key, 'deny'))
        self.assertTrue(self.row().pending)
        self.assertEqual(self.row().policy, 'Allowed')
        notification = self.apply()
        self.assertEqual(notification.type, pb.CHANGE_RULE)
        self.assertEqual(self.row().policy, 'Blocked')
        self.assertFalse(self.row().pending)
        self.assertTrue(self.model.catalog.entries[self.key].ownership['adopted'])
        self.assertTrue(json.loads((self.root / 'applications.json').read_text())['apps'])

    def test_rejection_never_displays_success(self):
        self.model.request_policy(self.key, 'deny')
        self.apply(success=False)
        self.assertEqual(self.row().policy, 'Unavailable')
        self.assertIn('unconfirmed', self.row().error)
        self.assertFalse(self.model.request_policy(self.key, 'allow'))

    def test_external_edit_blocks_controls_and_marks_snapshot_stale(self):
        (self.rules / 'external.json').write_text('external modification')
        self.model.tick()
        self.assertEqual(self.row().policy, 'Unavailable')
        self.assertFalse(self.model.request_policy(self.key, 'deny'))
        self.assertFalse(self.model.controller.pending)

    def test_defaults_only_after_ack_and_inventory_remains(self):
        self.model.request_policy(self.key, 'defaults')
        self.assertEqual(self.row().policy, 'Allowed')
        notification = self.apply()
        self.assertEqual(notification.type, pb.DELETE_RULE)
        self.assertEqual(self.row().policy, 'Not configured')
        self.assertIn(self.key, self.model.catalog.entries)
        self.assertEqual(self.model.catalog.entries[self.key].ownership, {})

    def test_late_ack_after_timeout_requires_fresh_subscription(self):
        self.model.controller.timeout = .01
        self.model.request_policy(self.key, 'deny')
        notification = self.incoming.result(timeout=2)
        time.sleep(.02); self.model.tick()
        self.assertEqual(self.row().policy, 'Unavailable')
        (self.rules / 'existing.json').write_text('backend applied the delayed change')
        self.daemon.replies.put(pb.NotificationReply(id=notification.id, code=pb.OK))
        # A sentinel Ping ensures the reader has delivered its queued callback.
        self.daemon.stub.Ping(pb.PingRequest(id=3), timeout=2)
        self.pump(lambda: self.events.empty(), timeout=1) if not self.events.empty() else None
        self.assertEqual(self.row().policy, 'Unavailable')
        self.daemon.stream.cancel()
        self.pump(lambda: not self.model.online(self.key[0]))
        changed = ApplicationRule.from_proto(notification.rules[0])
        self.connect([changed])
        self.assertEqual(self.row().policy, 'Blocked')
        self.assertFalse(self.model.catalog.entries[self.key].ownership.get('uncertain'))

    def test_history_records_events_without_changing_schema_or_raw_path(self):
        event = pb.Event(unixnano=1700000000000000000, rule=to_proto(self.rule, pb.Rule),
            connection=pb.Connection(process_path=str(self.binary), protocol='tcp', dst_ip='203.0.113.1', dst_port=443))
        stats = pb.Statistics(events=[event], by_executable={str(self.binary): 1})
        self.daemon.stub.Ping(pb.PingRequest(id=5, stats=stats), timeout=2)
        self.pump(lambda: self.model.catalog.entries[self.key].last_seen == 1700000000)
        self.model.history.submit('query', 'SELECT 1').result(timeout=2)
        rows = HistoryReader(self.root / 'history.db').activity()
        self.assertEqual(rows[0][:2], self.key)
        version = self.model.history.submit('query', 'PRAGMA user_version').result(timeout=2)
        self.assertEqual(version, ((3,),))

    def test_second_writer_lease_is_rejected(self):
        other = ApplicationSession(self.root, Timer(), lambda *args: None)
        with self.assertRaises(BlockingIOError):
            other.start('unix://' + str(self.root / 'other.sock'), lambda prompt: None)
        self.assertTrue(self.model.online(self.key[0]))

    def test_checksum_update_is_acknowledged_and_preserves_conditions(self):
        from dataclasses import replace
        compound = replace(self.rule, operator=dict(type='list', operand='list', data='', sensitive=False,
            list=[self.rule.operator, dict(type='simple', operand='process.hash.md5', data='old', sensitive=False, list=[])]))
        self.connect([compound])
        connection = pb.Connection(process_path=str(self.binary), process_checksums={'process.hash.md5': 'new'})
        results = []
        self.model.update_checksums(self.key[0], ['existing'], connection, lambda *args: results.append(args))
        self.assertFalse(results)
        notification = self.incoming.result(timeout=2)
        self.assertEqual(notification.rules[0].operator.list[1].data, 'new')
        self.assertEqual(notification.rules[0].duration, 'always')
        (self.rules / 'existing.json').write_text(notification.rules[0].SerializeToString().hex())
        self.daemon.replies.put(pb.NotificationReply(id=notification.id, code=pb.OK))
        self.pump(lambda: bool(results))
        self.assertTrue(results[0][0])
        updated = self.model.policies.rule_by_name(self.key[0], 'existing')
        self.assertEqual(updated.operator['list'][0], compound.operator['list'][0])
        self.assertEqual(updated.operator['list'][1]['data'], 'new')
        self.assertFalse(self.model._checksum_pending)

    def test_checksum_timeout_does_not_accept_late_success(self):
        from dataclasses import replace
        compound = replace(self.rule, operator=dict(type='list', operand='list', data='', sensitive=False, list=[self.rule.operator, dict(type='simple', operand='process.hash.md5', data='old', sensitive=False, list=[])]))
        self.connect([compound])
        connection = pb.Connection(process_path=str(self.binary), process_checksums={'process.hash.md5': 'new'})
        results = []
        self.model.update_checksums(self.key[0], ['existing'], connection, lambda *args: results.append(args))
        nonce, (deadline, callback) = next(iter(self.model._checksum_operations.items()))
        self.model._checksum_operations[nonce] = (0, callback)
        self.model.tick()
        self.assertFalse(results[0][0])
        callback(True, '')
        self.assertEqual(len(results), 1)
        self.assertEqual(self.row().policy, 'Unavailable')

    def test_frontend_and_backend_permissions_are_separate(self):
        from dataclasses import replace
        helper = self.root / 'example-helper'
        helper.write_text('')
        front = replace(self.rule, action='deny')
        backend = replace(self.rule, name='helper-allow', operator=dict(
            type='simple', operand='process.path', data=str(helper), sensitive=True, list=[]))
        self.connect([front, backend])
        policies = {row.identity.executable_path: row.policy for row in self.model.snapshot.applications}
        self.assertEqual(policies[str(self.binary)], 'Blocked')
        self.assertEqual(policies[str(helper)], 'Allowed')

    def test_daemon_rewritten_creation_time_does_not_lock_controls(self):
        from dataclasses import replace
        self.model.request_policy(self.key, 'deny')
        notification = self.apply()
        actual = replace(ApplicationRule.from_proto(notification.rules[0]), created=int(time.time()) + 10)
        self.connect([actual])
        self.assertEqual(self.row().policy, 'Blocked')
        self.assertEqual(self.row().error, '')
        self.assertTrue(self.model.request_policy(self.key, 'allow'))

    def test_legacy_owner_migrates_from_fresh_exact_rule_without_changing_policy(self):
        from dataclasses import replace
        owner = self.model.controller._ownership(self.key, self.rule)
        owner.pop('policy_fingerprint')
        self.model.catalog.set_ownership(self.key, owner)
        actual = replace(self.rule, created=self.rule.created + 100)
        self.connect([actual])
        migrated = self.model.catalog.entries[self.key].ownership
        self.assertEqual(migrated['policy_fingerprint'], actual.policy_fingerprint)
        self.assertEqual(self.row().policy, 'Allowed')
        self.assertEqual(self.row().error, '')
        self.assertFalse(self.model.controller.pending)

    def test_meaningful_external_edit_still_requires_explicit_refresh(self):
        from dataclasses import replace
        self.model.catalog.set_ownership(self.key, self.model.controller._ownership(self.key, self.rule))
        actual = replace(self.rule, action='deny', created=self.rule.created + 100)
        self.connect([actual])
        self.assertIn('changed outside', self.row().error)
        self.assertFalse(self.model.request_policy(self.key, 'allow'))
        self.model.refresh_policies()
        self.pump(lambda: not self.model.online(self.key[0]))
        self.connect([actual])
        self.assertEqual(self.row().policy, 'Blocked')
        self.assertEqual(self.row().error, '')
        self.assertFalse(self.model.controller.pending)

    def test_legacy_ownership_does_not_rebase_a_different_executable(self):
        from dataclasses import replace
        owner = self.model.controller._ownership(self.key, self.rule)
        owner.pop('policy_fingerprint')
        self.model.catalog.set_ownership(self.key, owner)
        actual = replace(self.rule, created=self.rule.created + 100,
                         operator=dict(self.rule.operator, data='/different/executable'))
        self.connect([actual])
        self.assertNotIn('policy_fingerprint', self.model.catalog.entries[self.key].ownership)
        self.assertFalse(self.model.request_policy(self.key, 'allow'))
