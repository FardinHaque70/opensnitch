"""Executable association and local service discovery must remain conservative."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

from opensnitch.systemd_catalog import (ServiceIdentity, SystemdReader, discover_scope,
                                        executable_association, shared_services)


class TestSystemdCatalog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.binary = Path(self.temp.name) / 'daemon'
        self.binary.write_bytes(b'\x7fELFfixture')
        self.binary.chmod(0o755)

    def properties(self, **kwargs):
        return dict(ExecStart=[(str(self.binary), [str(self.binary)], False)], MainPID=0, **kwargs)

    def test_single_native_executable_and_symlink(self):
        link = self.binary.parent / 'daemon-link'
        link.symlink_to(self.binary)
        configured, executable, reason = executable_association(self.properties())
        self.assertEqual((configured, executable, reason), (str(self.binary), str(self.binary), ''))
        props = {'ExecStart': [(str(link), [], False)]}
        self.assertEqual(executable_association(props)[1], str(self.binary))

    def test_scripts_shared_interpreters_and_missing_binaries_are_read_only(self):
        script = self.binary.parent / 'script'
        script.write_text('#!/bin/sh\nexec /usr/bin/daemon\n')
        for path in (str(script), '/usr/bin/python3', '/usr/bin/env', '/usr/bin/sh', '/does/not/exist'):
            with self.subTest(path=path):
                _, executable, reason = executable_association({'ExecStart': [(path, [], False)]})
                self.assertFalse(executable)
                self.assertTrue(reason)

    def test_multiple_commands_filesystem_namespaces_and_specifiers_are_read_only(self):
        cases = [dict(ExecStart=[]), dict(ExecStart=[(str(self.binary),), (str(self.binary),)]),
                 dict(ExecStart=[('/opt/%i/daemon',)]), self.properties(RootDirectory='/srv/container'),
                 self.properties(RootImage='/srv/image.raw'), self.properties(BindPaths=[('/tmp', '/usr/bin', False, 0)])]
        for properties in cases:
            self.assertFalse(executable_association(properties)[1])
            self.assertTrue(executable_association(properties)[2])

    def test_system_and_user_instances_share_executable_but_not_unit_identity(self):
        a = ServiceIdentity('system', 'a.service', executable_path=str(self.binary))
        b = replace(a, scope='user')
        c = replace(a, unit_name='c.service', executable_path='/different/daemon')
        rows = {record.key: record for record in (a, b, c)}
        self.assertEqual(shared_services(rows, str(self.binary)), (a.key, b.key))
        self.assertEqual(shared_services(rows, ''), ())

    def reader(self):
        path = '/org/freedesktop/systemd1/unit/example'
        calls = []
        def manager(method, *args):
            calls.append((method, args))
            if method == 'ListUnits':
                return [('example.service', 'Example service', 'loaded', 'active', 'running', '', path, 0, '', '/')]
            if method == 'ListUnitFiles':
                return [('/lib/systemd/system/example.service', 'enabled'),
                        ('/lib/systemd/system/alias.service', 'alias'),
                        ('/lib/systemd/system/inactive.service', 'disabled'),
                        ('/lib/systemd/system/example@.service', 'static')]
            if method == 'LoadUnit':
                return path if args[0] == 'alias.service' else path + '_inactive'
            raise AssertionError('Unexpected operation: ' + method)
        def properties(object_path, interface):
            if interface.endswith('.Unit'):
                inactive = object_path.endswith('_inactive')
                return dict(Id='inactive.service' if inactive else 'example.service',
                            Description='Inactive example' if inactive else 'Example service',
                            ActiveState='inactive' if inactive else 'active', SubState='dead' if inactive else 'running',
                            Names=['inactive.service'] if inactive else ['example.service', 'alias.service'])
            return self.properties()
        return SimpleNamespace(DESTINATION=SystemdReader.DESTINATION, manager=manager, properties=properties, calls=calls)

    def test_inactive_unit_files_templates_and_aliases_without_starting_units(self):
        reader = self.reader()
        rows = discover_scope('system', reader)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[('system', 'inactive.service')].active_state, 'inactive')
        self.assertEqual(rows[('system', 'inactive.service')].executable_path, str(self.binary))
        self.assertEqual(rows[('system', 'example.service')].aliases, ('alias.service',))
        template = rows[('system', 'example@.service')]
        self.assertFalse(template.executable_path)
        self.assertEqual(template.active_state, 'template')
        self.assertTrue(all(method in ('ListUnits', 'ListUnitFiles', 'LoadUnit') for method, _ in reader.calls))

    def test_unavailable_manager_propagates_for_explicit_unavailable_state(self):
        reader = self.reader()
        reader.manager = lambda *args: (_ for _ in ()).throw(OSError('Manager unavailable'))
        with self.assertRaises(OSError):
            discover_scope('system', reader)

    def test_discovery_reader_rejects_mutating_methods_before_using_bus(self):
        reader = SystemdReader(None)
        for method in ('StartUnit', 'StopUnit', 'RestartUnit', 'EnableUnitFiles', 'SetUnitProperties'):
            with self.assertRaises(ValueError):
                reader.call('/unit', 'interface', method)

    def test_unavailable_process_is_not_assigned_a_different_executable(self):
        reader = self.reader()
        original = reader.properties
        def properties(path, interface):
            result = original(path, interface)
            if interface.endswith('.Service'):
                result['MainPID'] = 99999999
            return result
        reader.properties = properties
        with patch('os.readlink', side_effect=PermissionError('restricted /proc')):
            rows = discover_scope('system', reader)
        self.assertEqual(rows[('system', 'example.service')].executable_path, str(self.binary))
        self.assertEqual(rows[('system', 'example.service')].observed_executable, '')


if __name__ == '__main__':
    unittest.main()
