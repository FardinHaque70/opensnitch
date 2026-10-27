"""Shared domain checks run without a display or GUI libraries."""
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from opensnitch.core.catalog import CatalogCore
from opensnitch.core.controls import AccessControllerCore
from opensnitch.core.history import HistoryReader
from opensnitch.core.identity import IdentityCore
from opensnitch.application_policy import ApplicationPolicyService


class Timer:
    def start(self):
        pass

    def stop(self):
        pass


class TestCore(unittest.TestCase):
    def test_imports_need_no_gui(self):
        code = '''
import sys
class BlockGUI:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('PyQt5', 'PyQt6', 'gi'):
            raise AssertionError('GUI dependency: ' + fullname)
sys.meta_path.insert(0, BlockGUI())
from opensnitch.core import catalog, controls, identity, backend, history, protocol, transport, prompts, settings, session, history_writer, schema
'''
        subprocess.run([sys.executable, '-c', code], check=True, env=os.environ.copy())

    def test_atomic_failure_retains_old_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'applications.json'
            catalog = CatalogCore(path, Timer())
            catalog.observe_many('node', ['/usr/bin/one'], 'manual')
            self.assertTrue(catalog.flush())
            original = path.read_bytes()
            catalog.observe_many('node', ['/usr/bin/two'], 'installed')
            with patch('opensnitch.core.catalog.os.replace', side_effect=OSError('fixture failure')):
                self.assertFalse(catalog.flush())
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
            self.assertTrue(catalog.flush())
            self.assertEqual(len(CatalogCore(path, Timer()).entries), 2)

    def test_unknown_inventory_version_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'apps.json'
            path.write_text('{"version":999,"apps":[]}')
            catalog = CatalogCore(path, Timer())
            self.assertTrue(catalog.read_only)
            catalog.observe_many('node', ['/bin/example'], 'manual')
            self.assertFalse(catalog.flush())
            self.assertEqual(json.loads(path.read_text())['version'], 999)

    def test_ack_delivery_is_owned_by_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = CatalogCore(Path(directory) / 'apps.json', Timer())
            catalog.observe_many('local', ['/bin/example'], 'manual')
            policy = ApplicationPolicyService()
            policy.replace_rules('local', [])
            replies, queue, applied = [], [], []
            controller = AccessControllerCore(catalog, policy,
                lambda node, operation, rule, callback: replies.append(callback),
                lambda node: True, lambda *args: applied.append(args),
                dispatch=lambda callback, *args: queue.append((callback, args)))
            self.assertTrue(controller.request(('local', '/bin/example'), 'deny'))
            replies[0](True, '')
            self.assertFalse(applied)
            callback, args = queue.pop()
            callback(*args)
            self.assertEqual(len(applied), 1)
            self.assertFalse(controller.pending)
            self.assertTrue(catalog.entries['local', '/bin/example'].ownership)

    def test_identity_cache_and_remote_boundary(self):
        resolver = IdentityCore()
        resolver.accept_index(({'/bin/example': ('Example', 'example-icon', '', '')}, set(), (), {}))
        resolver.register_node('local', True)
        self.assertEqual(resolver.resolve('local', '/bin/example').display_name, 'Example')
        remote = resolver.resolve('remote', '/bin/example')
        self.assertEqual(remote.display_name, 'example')
        self.assertFalse(remote.icon_reference)
        self.assertIs(remote, resolver.resolve('remote', '/bin/example'))

    def test_history_reads_existing_schema_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.db'
            connection = sqlite3.connect(path)
            connection.execute('CREATE TABLE connections (node text, process text, time text)')
            connection.executemany('INSERT INTO connections VALUES (?, ?, ?)', [
                ('local', '/bin/example', '2026-01-01 12:00:00'),
                ('local', '/bin/example', '2026-01-02 12:00:00'),
                ('remote', '/bin/example', '2026-01-01 13:00:00')])
            connection.commit(); connection.close()
            original = path.read_bytes()
            rows = HistoryReader(path).activity()
            self.assertEqual(len(rows), 2)
            self.assertIn(('local', '/bin/example', '2026-01-02 12:00:00'), rows)
            self.assertEqual(path.read_bytes(), original)
            missing = path.parent / 'missing.db'
            with self.assertRaises(sqlite3.OperationalError):
                HistoryReader(missing).activity()
            self.assertFalse(missing.exists())

    def test_package_verified_companion_borrows_only_artwork(self):
        from opensnitch.core.identity import packaged_companions
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary, helper = root / 'example-ui', root / 'example'
            primary.write_text(''); helper.write_text('')
            manifests = root / 'packages' / 'example-1'
            manifests.mkdir(parents=True)
            manifest = manifests / 'files'
            manifest.write_text('%FILES%\n' + str(primary).lstrip('/') + '\n' + str(helper).lstrip('/') + '\n')
            entry = ('Example', 'example-icon', '', 'example.desktop')
            companions, files = packaged_companions({str(primary): entry}, set(), root / 'packages')
            self.assertIn(str(helper), companions)
            resolver = IdentityCore()
            resolver.accept_index(({str(primary): entry}, set(), (), {}, {}, companions))
            resolver.register_node('local', True)
            identity = resolver.resolve('local', str(helper))
            self.assertEqual(identity.display_name, 'example')
            self.assertEqual(identity.icon_reference, 'example-icon')
            self.assertEqual(identity.icon_owner, 'Example')
            self.assertFalse(identity.exact)
            self.assertFalse(resolver.resolve('remote', str(helper)).icon_reference)
            manifest.write_text('%FILES%\n' + str(primary).lstrip('/') + '\n')
            self.assertEqual(packaged_companions({str(primary): entry}, set(), root / 'packages')[0], {})

    def test_other_network_rule_description_does_not_claim_app_match(self):
        from opensnitch.application_policy import ApplicationRule, describe_rule
        rule = ApplicationRule('deny-smtp', 'deny', 'always', True, False,
                               dict(type='simple', operand='dest.port', data='25'))
        self.assertEqual(describe_rule(rule), 'Block · Destination port: 25')
