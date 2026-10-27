"""Rule construction and settings compatibility for native prompts."""
from pathlib import Path
import tempfile
import unittest
from opensnitch.core.prompts import build_rule, checksum_mismatches, targets
from opensnitch.core.settings import PromptSettings, DURATIONS
from opensnitch.proto import import_
pb, _ = import_()


class TestPrompts(unittest.TestCase):
    def setUp(self):
        self.connection = pb.Connection(process_path='/usr/bin/example', process_args=['example', '--test'],
            process_id=123, user_id=1000, protocol='tcp', dst_ip='192.0.2.10', dst_host='api.example.org', dst_port=443)

    def test_actions_durations_and_unique_rule_names(self):
        target = targets(self.connection)[0]
        names = set()
        for action in ('allow', 'deny', 'reject'):
            for duration in DURATIONS:
                rule = build_rule(self.connection, action, duration, target)
                self.assertFalse(rule.precedence)
                self.assertEqual(rule.action, action)
                self.assertEqual(rule.duration, duration)
                self.assertEqual(rule.operator['data'], '/usr/bin/example')
                names.add(rule.name)
        self.assertEqual(len(names), 27)

    def test_untrusted_command_line_includes_exact_executable(self):
        rule = build_rule(self.connection, 'deny', 'once', targets(self.connection)[1])
        self.assertEqual(rule.operator['type'], 'list')
        self.assertEqual({child['operand'] for child in rule.operator['list']}, {'process.path', 'process.command'})

    def test_compound_prompt_conditions(self):
        values = targets(self.connection)
        rule = build_rule(self.connection, 'allow', '12h', values[0], (values[2], values[4]))
        self.assertEqual({child['operand'] for child in rule.operator['list']}, {'dest.port', 'user.id', 'process.path'})

    def test_destination_patterns_and_ipv6_networks(self):
        values = targets(self.connection)
        self.assertTrue(any(t.data == '192.0.2.0/24' for t in values))
        self.assertTrue(any(t.operand == 'dest.host' and t.type == 'regexp' for t in values))
        self.connection.dst_ip = '2001:db8::1'
        self.assertTrue(any(t.data == '2001:db8::/64' for t in targets(self.connection)))

    def test_network_alias_also_limits_executable(self):
        self.connection.dst_ip = '192.168.1.1'
        target = next(t for t in targets(self.connection) if t.data == 'LAN')
        rule = build_rule(self.connection, 'deny', 'always', target)
        self.assertEqual(rule.operator['type'], 'list')
        self.assertIn('process.path', [child['operand'] for child in rule.operator['list']])

    def test_settings_and_zero_timeout_are_read_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.conf'
            text = '[global]\ndefault_action=2\ndefault_duration=8\ndefault_timeout=0\ndefault_target=4\ndefault_popup_advanced=true\n'
            path.write_text(text)
            settings = PromptSettings.read(directory)
            self.assertEqual((settings.action, settings.duration, settings.timeout, settings.target), ('reject', 'always', 0, 4))
            self.assertTrue(settings.advanced)
            self.assertEqual(path.read_text(), text)

    def test_empty_or_invalid_target_and_duration_are_rejected(self):
        target = targets(pb.Connection())[0]
        with self.assertRaises(ValueError):
            build_rule(self.connection, 'allow', 'always', target)
        with self.assertRaises(ValueError):
            build_rule(self.connection, 'allow', 'invalid', targets(self.connection)[0])

    def test_changed_checksum_warning_identifies_existing_rule(self):
        self.connection.process_checksums['process.hash.md5'] = 'new'
        from opensnitch.core.prompts import Target
        rule = build_rule(self.connection, 'allow', 'always', targets(self.connection)[0],
                          (Target('', 'simple', 'process.hash.md5', 'old'),))
        self.assertEqual(checksum_mismatches(self.connection, (rule,)), ((rule.name, 'old'),))

    def test_appimage_pattern_matches_across_mounts(self):
        import re
        self.connection.process_path = '/tmp/.mount_ExampleAbCdEf/usr/bin/example'
        target = next(t for t in targets(self.connection) if t.label.startswith('This AppImage'))
        self.assertIsNotNone(re.search(target.data, '/tmp/.mount_Example123456/usr/bin/example'))
        self.assertIsNone(re.search(target.data, '/tmp/.mount_Other123456/usr/bin/example'))
