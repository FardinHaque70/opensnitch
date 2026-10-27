"""Fresh service verification cannot redirect application policy changes."""
from dataclasses import replace
from types import SimpleNamespace
import subprocess
import sys
import unittest
from opensnitch.core.services import ServiceIdentity, ServiceAccessController


class Provider:
    def __init__(self, records):
        self.records = {record.key: record for record in records}
        self.errors = {}
        self.callback = None

    def refresh(self, callback):
        self.callback = callback


class ServiceAccessTests(unittest.TestCase):
    def setUp(self):
        self.a = ServiceIdentity('system', 'alpha.service', executable_path='/usr/bin/example')
        self.b = replace(self.a, scope='user', unit_name='beta.service')
        self.provider = Provider((self.a, self.b))
        self.sent, self.messages = [], []
        self.controller = ServiceAccessController(self.provider,
            lambda *args: self.sent.append(args), message=self.messages.append)

    def request(self):
        return self.controller.request(self.a.key, 'unix:/local', 'deny', '12h')

    def test_fresh_verified_shared_executable_uses_exact_node_path(self):
        self.assertTrue(self.request())
        self.assertFalse(self.sent)
        self.assertEqual(self.controller.pending, self.a.key)
        self.provider.callback()
        self.assertEqual(self.sent, [(('unix:/local', '/usr/bin/example'), 'deny', '12h')])
        self.assertIsNone(self.controller.pending)

    def test_changed_executable_or_shared_set_never_sends(self):
        for changed in ('path', 'shared', 'reason', 'scope-error', 'removed'):
            with self.subTest(changed=changed):
                self.setUp()
                self.request()
                if changed == 'path':
                    self.provider.records[self.a.key] = replace(self.a, executable_path='/different')
                elif changed == 'shared':
                    self.provider.records.pop(self.b.key)
                elif changed == 'reason':
                    self.provider.records[self.a.key] = replace(self.a, reason='Wrapper')
                elif changed == 'scope-error':
                    self.provider.errors['system'] = 'Disconnected'
                else:
                    self.provider.records.pop(self.a.key)
                self.provider.callback()
                self.assertFalse(self.sent)
                self.assertTrue(self.messages)

    def test_remote_unresolved_and_ambiguous_entries_are_read_only(self):
        self.assertFalse(self.controller.request(self.a.key, 'tcp:remote', 'allow'))
        self.provider.records[self.a.key] = replace(self.a, executable_path='', reason='Ambiguous')
        self.assertFalse(self.request())
        self.assertFalse(self.sent)

    def test_cancelled_or_duplicate_request_does_not_send(self):
        self.assertTrue(self.request())
        self.assertFalse(self.request())
        self.controller.cancel()
        self.provider.callback()
        self.assertFalse(self.sent)

    def test_service_core_import_needs_no_gui_toolkit(self):
        subprocess.run([sys.executable, '-c',
            "import opensnitch.core.services, sys; assert not any(n.startswith(('gi', 'PyQt')) for n in sys.modules)"], check=True)


if __name__ == '__main__':
    unittest.main()
