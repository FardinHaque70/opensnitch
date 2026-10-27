"""Temporary inventory cleanup planning, with no access to live rules."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import re
from opensnitch.application_policy import ApplicationRule
from opensnitch.core.catalog import CatalogEntry
from opensnitch.core.cleanup import build_plan, literal_prefix, missing


class CleanupPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.mount_CleanupFixture')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = CatalogEntry('unix:/local', str(self.root / 'unityhub-bin'), ['observed'])
        self.rule = ApplicationRule('fixture-rule', 'allow', 'always', True, False,
            dict(type='regexp', operand='process.path', data='^' + re.escape(str(self.root)) + '/unityhub-bin$', sensitive=True, list=[]))

    def test_missing_appimage_pattern_is_reviewed_once(self):
        second = replace(self.entry, executable_path=str(self.root / 'license-client'))
        rule = replace(self.rule, operator=dict(self.rule.operator, data='^' + re.escape(str(self.root)) + '/.*$'))
        plan = build_plan((self.entry, second), (rule,), ())
        self.assertEqual(len(plan.entries), 2)
        self.assertEqual(plan.rules, (rule,))
        self.assertFalse(plan.protected)

    def test_active_mount_or_other_matching_entry_protects_rule(self):
        plan = build_plan((self.entry,), (self.rule,), (str(self.root),))
        self.assertFalse(plan.rules)
        self.assertIn('active', plan.protected[0][1])
        hidden = replace(self.entry, hidden=True)
        plan = build_plan((self.entry, hidden), (self.rule,), ())
        # Same identity is already selected, so hiding does not create a distinct app.
        self.assertTrue(plan.rules)
        remote = replace(self.entry, node_id='ipv4:192.0.2.1')
        plan = build_plan((self.entry, remote), (self.rule,), ())
        self.assertTrue(plan.rules)  # A local rule cannot affect another computer.

    def test_broad_or_compound_pattern_is_never_auto_selected(self):
        for operator in (dict(self.rule.operator, data='^/tmp/.*$'),
                         dict(self.rule.operator, data=self.rule.operator['data'] + '|^/usr/bin/other$'),
                         dict(type='list', operand='list', list=[self.rule.operator]),
                         dict(self.rule.operator, data=re.escape(str(self.root)) + '/.*$')):
            with self.subTest(operator=operator):
                plan = build_plan((self.entry,), (replace(self.rule, operator=operator),), ())
                self.assertFalse(plan.rules)
                self.assertTrue(plan.protected)

    def test_existing_remote_non_temporary_and_permission_unknown_are_excluded(self):
        Path(self.entry.executable_path).write_text('alive')
        self.assertFalse(build_plan((self.entry,), (self.rule,), ()).entries)
        self.assertFalse(build_plan((replace(self.entry, node_id='remote'),), (), ()).entries)
        self.assertFalse(build_plan((replace(self.entry, executable_path='/opt/missing'),), (), ()).entries)
        with patch('opensnitch.core.cleanup.os.stat', side_effect=PermissionError(13, 'denied')):
            self.assertFalse(missing('/tmp/unknown'))

    def test_unknown_mount_state_protects_patterns(self):
        plan = build_plan((self.entry,), (self.rule,), None)
        self.assertFalse(plan.rules)
        self.assertIn('could not be verified', plan.protected[0][1])

    def test_literal_prefix_does_not_treat_regex_escapes_as_paths(self):
        self.assertEqual(literal_prefix(r'^/tmp/\.mount_UnityH[0-9A-Za-z]{6,7}/.*$'), '/tmp/.mount_UnityH')
        self.assertEqual(literal_prefix(r'^/tmp/\d/mount'), '/tmp/')


if __name__ == '__main__': unittest.main()
