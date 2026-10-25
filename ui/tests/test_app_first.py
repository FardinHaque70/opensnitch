"""Inventory and policy controls must not confuse UI state with enforcement."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from PyQt5 import QtCore, QtWidgets

from opensnitch.application_catalog import ApplicationCatalog
from opensnitch.application_controls import ApplicationAccessController
from opensnitch.application_policy import ApplicationPolicyService, ApplicationRule, managed_rule_name


def app_rule(path='/usr/bin/example', name='existing', action='allow', duration='always', enabled=True):
    return ApplicationRule(name, action, duration, enabled, False,
                           {'type': 'simple', 'operand': 'process.path', 'data': path,
                            'sensitive': True, 'list': []}, 1700000000)


class TestAppFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'applications.json'
        self.catalog = ApplicationCatalog(self.path)
        self.key = ('unix:/local', '/usr/bin/example')
        self.catalog.observe_many(*[self.key[0], [self.key[1]], 'installed'])
        self.policies = ApplicationPolicyService()
        self.policies.replace_rules(self.key[0], [])
        self.sent = []
        self.connected = True
        self.applied = []
        def transport(node, operation, rule, callback):
            self.sent.append((node, operation, rule, callback))
        def apply(node, operation, rule):
            self.applied.append((operation, rule))
            rules = [r for r in self.policies.rules[node] if r.name != rule.name]
            if operation != 'delete':
                rules.append(rule)
            self.policies.replace_rules(node, rules)
        self.controller = ApplicationAccessController(
            self.catalog, self.policies, transport, lambda node: self.connected, apply, timeout=1)

    def tearDown(self):
        self.controller._timer.stop()
        self.catalog._save_timer.stop()
        self.controller.deleteLater(); self.catalog.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.temp.cleanup()

    def reply(self, success=True):
        self.sent[-1][3](success, '' if success else 'Rejected')
        self.app.processEvents()

    def own(self, rule):
        self.catalog.set_ownership(self.key, {'name': rule.name, 'fingerprint': rule.fingerprint})

    def test_catalog_merges_sources_but_not_nodes_or_paths(self):
        self.catalog.observe_many(self.key[0], [self.key[1]], 'observed', 123)
        self.catalog.observe_many('remote', [self.key[1]], 'rules')
        self.catalog.observe_many(self.key[0], ['/different/example'], 'manual')
        self.assertEqual(len(self.catalog.entries), 3)
        self.assertEqual(set(self.catalog.entries[self.key].sources), {'installed', 'observed'})
        self.assertEqual(self.catalog.entries[self.key].last_seen, 123)

    def test_inventory_and_hidden_entries_survive_restart_without_policy_replay(self):
        self.catalog.hide(self.key)
        self.assertTrue(self.catalog.flush())
        restored = ApplicationCatalog(self.path)
        self.assertTrue(restored.entries[self.key].hidden)
        self.assertEqual(self.sent, [])
        restored.deleteLater()

    def test_corrupt_and_newer_inventory_files_are_preserved(self):
        for content in ('not json', '{"version":999,"apps":[]}'):
            self.path.write_text(content)
            restored = ApplicationCatalog(self.path)
            self.assertTrue(restored.read_only)
            restored.observe_many('node', ['/bin/other'], 'manual')
            self.assertFalse(restored.flush())
            self.assertEqual(self.path.read_text(), content)
            restored.deleteLater()

    def test_policy_groups(self):
        for action, duration, group in [('allow', 'always', 'Allowed'), ('deny', 'always', 'Blocked'),
                                         ('reject', 'always', 'Blocked'), ('allow', '12h', 'Temporary rules'),
                                         ('deny', 'until restart', 'Temporary rules')]:
            with self.subTest(action=action, duration=duration):
                self.policies.replace_rules(self.key[0], [app_rule(action=action, duration=duration)])
                self.assertEqual(self.policies.summarize(*self.key).group, group)
        self.policies.replace_rules(self.key[0], [app_rule(enabled=False)])
        self.assertEqual(self.policies.summarize(*self.key).group, 'Not configured')

    def test_compound_regex_and_conflicting_rules_are_custom(self):
        base = app_rule()
        variants = [
            [base, replace(base, name='block', action='deny')],
            [replace(base, operator={'type': 'list', 'list': [base.operator, {'type': 'simple', 'operand': 'dest.port', 'data': '443'}]})],
            [replace(base, operator={'type': 'regexp', 'operand': 'process.path', 'data': '.*example$', 'sensitive': True})],
        ]
        for rules in variants:
            self.policies.replace_rules(self.key[0], rules)
            self.assertEqual(self.policies.summarize(*self.key).group, 'Custom rules')

    def test_global_rule_does_not_invent_an_application_policy(self):
        global_rule = replace(app_rule(), operator={'type': 'simple', 'operand': 'dest.port', 'data': '53'})
        self.policies.replace_rules(self.key[0], [global_rule])
        summary = self.policies.summarize(*self.key)
        self.assertEqual(summary.group, 'Not configured')
        self.assertEqual(summary.global_rules, (global_rule,))

    def test_allow_is_pending_until_acknowledged(self):
        self.assertTrue(self.controller.request(self.key, 'allow'))
        self.assertIn(self.key, self.controller.pending)
        self.assertEqual(self.policies.summarize(*self.key).group, 'Not configured')
        self.assertEqual(self.catalog.entries[self.key].ownership, {})
        self.reply()
        self.assertEqual(self.policies.summarize(*self.key).group, 'Allowed')
        rule = self.sent[-1][2]
        self.assertFalse(rule.precedence)
        self.assertTrue(rule.operator['sensitive'])
        self.assertEqual(rule.operator['data'], self.key[1])
        self.assertEqual(self.catalog.entries[self.key].ownership['fingerprint'], rule.fingerprint)

    def test_block_directly_updates_single_existing_rule_after_ack(self):
        original = replace(app_rule(enabled=False), description='Existing description', nolog=True)
        self.policies.replace_rules(self.key[0], [original])
        self.assertTrue(self.controller.request(self.key, 'deny'))
        sent = self.sent[-1][2]
        self.assertEqual(sent, replace(original, action='deny', enabled=True))
        self.assertEqual(self.policies.rule_by_name(self.key[0], original.name), original)
        self.reply()
        self.assertEqual(self.policies.summarize(*self.key).group, 'Blocked')
        owner = self.catalog.entries[self.key].ownership
        self.assertTrue(owner['adopted'])
        self.assertEqual(owner['executable_path'], self.key[1])
        self.assertTrue(self.controller.request(self.key, 'allow'))
        self.reply()
        self.assertEqual(self.policies.summarize(*self.key).group, 'Allowed')

    def test_custom_or_conflicting_rules_stay_unchanged_with_inline_reason(self):
        rule = app_rule()
        cases = [[rule, replace(rule, name='second', action='deny')],
                 [replace(rule, operator={'type': 'regexp', 'operand': 'process.path', 'data': '.*example'})],
                 [replace(rule, operator={'type': 'list', 'list': [rule.operator]})]]
        for rules in cases:
            self.policies.replace_rules(self.key[0], rules)
            self.assertFalse(self.controller.request(self.key, 'deny'))
            self.assertIn('Custom or conflicting', self.controller.reason(self.catalog.entries[self.key]))
        self.assertEqual(self.sent, [])

    def test_defaults_removes_only_single_existing_exact_app_rule_after_ack(self):
        original = app_rule()
        other = app_rule(path='/usr/bin/other', name='other')
        self.policies.replace_rules(self.key[0], [original, other])
        self.assertTrue(self.controller.request(self.key, 'defaults'))
        self.assertEqual(self.sent[-1][1:3], ('delete', original))
        self.assertEqual(self.policies.summarize(*self.key).group, 'Allowed')
        self.reply()
        self.assertEqual(self.policies.rules[self.key[0]], (other,))
        self.assertEqual(self.policies.summarize(*self.key).group, 'Not configured')

    def test_owned_rule_can_change_without_changing_its_name(self):
        self.controller.request(self.key, 'allow'); self.reply()
        name = self.sent[-1][2].name
        self.controller.request(self.key, 'deny'); self.reply()
        self.assertEqual(self.sent[-1][2].name, name)
        self.assertEqual(self.policies.summarize(*self.key).group, 'Blocked')

    def test_use_defaults_only_deletes_owned_rule_after_ack(self):
        self.controller.request(self.key, 'allow'); self.reply()
        self.controller.request(self.key, 'defaults')
        self.assertEqual(self.sent[-1][1], 'delete')
        self.assertEqual(self.policies.summarize(*self.key).group, 'Allowed')
        self.reply()
        self.assertEqual(self.policies.summarize(*self.key).group, 'Not configured')
        self.assertEqual(self.catalog.entries[self.key].ownership, {})

    def test_failure_does_not_display_success_or_allow_blind_retry(self):
        self.controller.request(self.key, 'allow'); self.reply(False)
        self.assertEqual(self.applied, [])
        self.assertEqual(self.policies.summarize(*self.key).group, 'Not configured')
        self.assertTrue(self.catalog.entries[self.key].ownership['uncertain'])
        self.assertFalse(self.controller.request(self.key, 'allow'))

    def test_failed_existing_rule_change_keeps_prior_access_until_reconciliation(self):
        original = app_rule()
        self.policies.replace_rules(self.key[0], [original])
        self.controller.request(self.key, 'deny'); self.reply(False)
        self.assertEqual(self.policies.summarize(*self.key).group, 'Allowed')
        self.assertTrue(self.catalog.entries[self.key].ownership['uncertain'])
        self.assertFalse(self.controller.request(self.key, 'deny'))
        self.controller.reconcile(self.key[0], [original])
        self.assertFalse(self.catalog.entries[self.key].ownership.get('uncertain'))
        self.assertTrue(self.controller.request(self.key, 'deny'))

    def test_timeout_late_reply_and_reconciliation(self):
        self.controller.request(self.key, 'allow')
        nonce, _, operation, rule = self.controller.pending[self.key]
        self.controller.pending[self.key] = nonce, 0, operation, rule
        self.controller.check_timeouts(); self.reply()
        self.assertEqual(self.applied, [])
        self.assertTrue(self.catalog.entries[self.key].ownership['uncertain'])
        self.controller.reconcile(self.key[0], [rule])
        self.assertNotIn('uncertain', self.catalog.entries[self.key].ownership)

    def test_disconnect_and_external_changes_block_controls(self):
        self.connected = False
        self.assertFalse(self.controller.request(self.key, 'allow'))
        self.connected = True
        rule = app_rule(name=managed_rule_name(self.key[1]))
        self.own(rule)
        self.policies.replace_rules(self.key[0], [replace(rule, duration='1h')])
        self.assertFalse(self.controller.request(self.key, 'deny'))

    def test_temporary_ownership_records_expiry_without_replaying_rules(self):
        self.controller.request(self.key, 'allow', '1h'); self.reply()
        owner = self.catalog.entries[self.key].ownership
        self.assertGreater(owner['expires_at'], time.time() + 3500)
        self.assertEqual(self.policies.summarize(*self.key).group, 'Temporary rules')

    def test_expired_frontend_timer_does_not_delete_daemon_policy_mirror(self):
        from opensnitch.application_runtime import ApplicationRuntime
        rule = app_rule(name=managed_rule_name(self.key[1]), duration='1h')
        self.own(rule)
        self.catalog.entries[self.key].ownership['expires_at'] = 0
        self.policies.replace_rules(self.key[0], [rule])
        runtime = SimpleNamespace(
            catalog=self.catalog, policies=self.policies,
            nodes=SimpleNamespace(get_nodes=lambda: {}),
            db=SimpleNamespace(get_rules=lambda node: None), errors={}, availability={},
            identities=SimpleNamespace(is_local_node=lambda node: False),
            changed=SimpleNamespace(emit=lambda: None))
        # No delete_rule method: expiry must never remove a rule locally.
        ApplicationRuntime._poll(runtime)
        owner = self.catalog.entries[self.key].ownership
        self.assertTrue(owner['uncertain'])
        self.assertEqual(owner['operation'], 'expiry')
        self.assertEqual(owner['name'], rule.name)
        self.assertEqual(self.policies.rule_by_name(self.key[0], rule.name), rule)
        self.assertIn(self.key[0], runtime.errors)

    def test_connected_empty_snapshot_allows_configuring_first_application(self):
        from opensnitch.application_runtime import ApplicationRuntime
        self.policies.rules.clear()
        configuration = {'data': object()}
        runtime = SimpleNamespace(catalog=self.catalog, policies=self.policies,
            nodes=SimpleNamespace(get_nodes=lambda: {self.key[0]: configuration},
                                  get_node=lambda node: configuration),
            db=SimpleNamespace(get_rules=lambda node: SimpleNamespace(next=lambda: False)),
            errors={}, availability={}, identities=SimpleNamespace(is_local_node=lambda node: False),
            changed=SimpleNamespace(emit=lambda: None))
        ApplicationRuntime._poll(runtime)
        self.assertIn(self.key[0], self.policies.rules)
        self.assertEqual(self.policies.rules[self.key[0]], ())
        self.assertTrue(self.controller.request(self.key, 'deny'))

    def test_wrong_executable_ownership_and_rule_name_collisions_are_rejected(self):
        unrelated = app_rule(path='/bin/other', name=managed_rule_name('/bin/other'))
        self.own(unrelated)
        self.policies.replace_rules(self.key[0], [unrelated])
        self.assertFalse(self.controller.request(self.key, 'defaults'))

    def test_existing_rule_review_targets_selected_node_without_applying_access(self):
        from opensnitch.application_identity import ApplicationIdentity
        from opensnitch.dialogs.applications import ApplicationRuleReview
        from opensnitch.utils import Icons
        self.policies.replace_rules(self.key[0], [app_rule()])
        self.policies.replace_rules('remote', [app_rule(name='remote-rule')])
        runtime = SimpleNamespace(policies=self.policies, identities=SimpleNamespace(
            resolve=lambda node, path: ApplicationIdentity(node, path, 'Example', '', False),
            icon_for=lambda identity, widget: Icons.new(widget, 'system-run')))
        review = ApplicationRuleReview(runtime, self.catalog.entries[self.key], None)
        self.addCleanup(review.deleteLater)
        edited = []
        review.edit_requested.connect(lambda *args: edited.append(args))
        self.assertIn('Changes take effect when saved', review.explanation.text())
        self.assertEqual(review.rules.topLevelItemCount(), 1)
        self.assertEqual(self.sent, [])
        review.edit_rule()
        self.assertEqual(edited, [(self.key[0], self.key[1], 'existing')])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.policies.summarize(*self.key).group, 'Allowed')

    def test_unconfirmed_review_without_rules_cannot_edit_an_unrelated_rule(self):
        from opensnitch.application_identity import ApplicationIdentity
        from opensnitch.dialogs.applications import ApplicationRuleReview
        from opensnitch.utils import Icons
        self.catalog.entries[self.key].ownership['uncertain'] = True
        runtime = SimpleNamespace(policies=self.policies, identities=SimpleNamespace(
            resolve=lambda node, path: ApplicationIdentity(node, path, 'Example', '', False),
            icon_for=lambda identity, widget: Icons.new(widget, 'system-run')))
        review = ApplicationRuleReview(runtime, self.catalog.entries[self.key], None)
        self.addCleanup(review.deleteLater)
        self.assertFalse(review.edit_button.isEnabled())
        self.assertEqual(review.rules.topLevelItemCount(), 0)
        self.assertIn('unconfirmed', review.explanation.text())
        self.assertEqual(self.sent, [])

    def test_local_disk_modifications_are_detected(self):
        from opensnitch.application_runtime import ApplicationRuntime, rule_files_snapshot
        rule = app_rule(name=managed_rule_name(self.key[1]))
        self.own(rule)
        config = json.dumps({'Rules': {'Path': str(self.path.parent)}})
        runtime = SimpleNamespace(errors={}, identities=SimpleNamespace(is_local_node=lambda node: True),
                                  nodes=SimpleNamespace(get_node_config=lambda node: config), policies=self.policies)
        document = dict(rule.__dict__)
        document['created'] = datetime.fromtimestamp(rule.created, timezone.utc).isoformat()
        filename = self.path.parent / (rule.name + '.json')
        filename.write_text(json.dumps(document))
        runtime._rule_files = {self.key[0]: rule_files_snapshot(config)}
        with patch.object(Path, 'read_text', side_effect=AssertionError('Must not read root-only rule contents')):
            self.assertEqual(ApplicationRuntime._verify_current(runtime, self.catalog.entries[self.key]), '')
        self.assertEqual(ApplicationRuntime._verify_current(runtime, self.catalog.entries[self.key]), '')
        document['action'] = 'deny'; filename.write_text(json.dumps(document))
        self.assertTrue(ApplicationRuntime._verify_current(runtime, self.catalog.entries[self.key]))


if __name__ == '__main__':
    unittest.main()
