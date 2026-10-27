"""Draft validation and one shared acknowledgment ledger for Apps and Rules."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from opensnitch.application_policy import ApplicationRule, ApplicationPolicyService
from opensnitch.core.catalog import CatalogCore
from opensnitch.core.controls import AccessControllerCore
from opensnitch.core.rule_edit import draft_rule, duration_seconds, editable_conditions, transport_edit_reason


def leaf(path='/usr/bin/example'):
    return dict(type='simple', operand='process.path', data=path, sensitive=True, list=[])


class Timer:
    def start(self): pass
    def stop(self): pass


class RuleEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.catalog = CatalogCore(Path(self.temp.name) / 'applications.json', Timer())
        self.key = ('unix:/local', '/usr/bin/example')
        self.catalog.observe_many(self.key[0], [self.key[1]], 'rules')
        self.rule = ApplicationRule('existing', 'allow', 'always', True, False, leaf(), 123, 'Original', True)
        self.policies = ApplicationPolicyService(); self.policies.replace_rules(self.key[0], (self.rule,))
        self.sent, self.completed = [], []
        def transport(*args): self.sent.append(args)
        def confirmed(node, operation, rule):
            rules = [item for item in self.policies.rules[node] if item.name != rule.name]
            if operation != 'delete': rules.append(rule)
            self.policies.replace_rules(node, rules)
        self.controller = AccessControllerCore(self.catalog, self.policies, transport,
            lambda node: True, confirmed, verify_node=lambda node: '')

    def request(self, proposed=None, expected='default', operation='change'):
        return self.controller.request_rule(self.key[0], proposed or self.rule,
            self.rule if expected == 'default' else expected, operation,
            lambda *args: self.completed.append(args))

    def test_draft_roundtrip_preserves_group_metadata_flags_and_order(self):
        grouped = dict(type='list', operand='list', data='', sensitive=True,
            list=[leaf(), dict(type='regexp', operand='dest.host', data=r'example\.test$', sensitive=False, list=[])])
        original = replace(self.rule, operator=grouped)
        result = draft_rule(original.name, original.action, original.duration, original.enabled,
            original.precedence, original.nolog, original.description, editable_conditions(grouped), original)
        self.assertEqual(result, original)
        self.assertEqual(result.operator['list'][0]['data'], '/usr/bin/example')

    def test_duration_and_condition_validation(self):
        self.assertEqual(duration_seconds('1h30m'), 5400)
        self.assertEqual(duration_seconds('500ms'), .5)
        self.assertIsNone(duration_seconds('once'))
        for value in ('0s', '-1h', 'forever', '١h', '1000000000000h'):
            with self.assertRaises(ValueError): duration_seconds(value)
        for condition in (dict(leaf(), data='relative'), dict(leaf(), type='regexp', data='(?=lookahead)'),
                          dict(leaf(), operand='dest.port', data='99999'), dict(leaf(), type='network', operand='dest.network', data='bad/CIDR')):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                draft_rule('new', 'allow', 'always', True, False, False, '', [condition])

    def test_create_and_edit_commit_only_after_ack_and_block_parallel_app_actions(self):
        proposed = replace(self.rule, action='deny', description='Changed')
        self.assertTrue(self.request(proposed))
        self.assertEqual(self.policies.rule_by_name(self.key[0], 'existing').action, 'allow')
        self.assertTrue(self.controller.general_pending(self.key[0]))
        self.assertFalse(self.controller.request(self.key, 'allow'))
        self.sent[-1][-1](True, '')
        actual = self.policies.rule_by_name(self.key[0], 'existing')
        self.assertEqual((actual.action, actual.description, actual.operator, actual.nolog), ('deny', 'Changed', self.rule.operator, True))
        self.assertEqual(self.completed, [(True, '')])
        new = replace(self.rule, name='new', operator=leaf('/new'))
        self.assertTrue(self.request(new, expected=None))
        self.assertIsNone(self.policies.rule_by_name(self.key[0], 'new'))
        self.sent[-1][-1](True, '')
        self.assertIsNotNone(self.policies.rule_by_name(self.key[0], 'new'))

    def test_owned_rule_updates_and_delete_keep_apps_metadata_consistent(self):
        self.catalog.set_ownership(self.key, self.controller._ownership(self.key, self.rule))
        self.request(replace(self.rule, action='deny', duration='1h')); self.sent[-1][-1](True, '')
        owner = self.catalog.entries[self.key].ownership
        self.assertIn('expires_at', owner)
        self.assertFalse(self.policies.quick_control_reason(self.catalog.entries[self.key]))
        current = self.policies.rule_by_name(self.key[0], self.rule.name)
        self.assertTrue(self.request(current, expected=current, operation='delete'))
        self.sent[-1][-1](True, '')
        self.assertFalse(self.catalog.entries[self.key].ownership)
        self.assertIsNone(self.policies.rule_by_name(self.key[0], self.rule.name))

    def test_conflict_collision_rename_and_unsafe_names_send_nothing(self):
        self.assertFalse(self.request(self.rule))
        self.assertIn('No changes', self.controller.last_error)
        self.assertFalse(self.request(self.rule, expected=None))
        self.assertFalse(self.request(replace(self.rule, name='renamed')))
        for name in ('../escape', '.hidden', 'with\nnewline', 'with\\separator'):
            self.assertFalse(self.request(replace(self.rule, name=name), expected=None))
        self.policies.replace_rules(self.key[0], [replace(self.rule, action='reject')])
        self.assertFalse(self.request(replace(self.rule, action='deny')))
        self.assertFalse(self.sent)

    def test_nested_rules_are_read_only_for_rewrite_but_can_be_deleted(self):
        nested = replace(self.rule, operator=dict(type='list', operand='list', data='', list=[dict(type='list', list=[leaf()])]))
        self.policies.replace_rules(self.key[0], [nested])
        self.assertTrue(transport_edit_reason(nested.operator))
        self.assertTrue(transport_edit_reason(dict(leaf(), operand='user.name', data='1000')))
        self.assertFalse(self.request(replace(nested, enabled=False), expected=nested, operation='disable'))
        self.assertTrue(self.request(nested, expected=nested, operation='delete'))

    def test_toggle_cannot_modify_other_fields(self):
        proposed = replace(self.rule, enabled=False)
        self.assertFalse(self.request(replace(proposed, action='deny'), operation='disable'))
        self.assertTrue(self.request(proposed, operation='disable'))
        self.sent[-1][-1](True, '')
        self.assertFalse(self.policies.rule_by_name(self.key[0], 'existing').enabled)

    def test_disabled_temporary_rule_does_not_get_an_active_expiry(self):
        self.catalog.set_ownership(self.key, self.controller._ownership(self.key, self.rule))
        self.assertTrue(self.request(replace(self.rule, enabled=False, duration='1h')))
        self.sent[-1][-1](True, '')
        self.assertNotIn('expires_at', self.catalog.entries[self.key].ownership)

    def test_timeout_and_late_ack_never_become_success(self):
        self.request(replace(self.rule, action='deny'))
        late_reply = self.sent[-1][-1]
        with patch('opensnitch.core.controls.time.monotonic', return_value=10**12):
            self.controller.check_timeouts()
        self.assertFalse(self.completed[0][0])
        late_reply(True, '')
        self.assertEqual(len(self.completed), 1)
        self.assertEqual(self.policies.rule_by_name(self.key[0], 'existing').action, 'allow')
        self.assertFalse(self.controller.pending or self.controller._general)

    def test_daemon_only_regex_is_preserved_unchanged(self):
        original = replace(self.rule, operator=dict(leaf(), type='regexp', data=r'\p{L}+'))
        result = draft_rule(original.name, 'deny', 'always', True, False, original.nolog,
            original.description, editable_conditions(original.operator), original)
        self.assertEqual(result.operator, original.operator)
        disabled = replace(original, enabled=False)
        enabled = draft_rule(disabled.name, disabled.action, disabled.duration, True, disabled.precedence,
            disabled.nolog, disabled.description, editable_conditions(disabled.operator), disabled)
        self.assertEqual(enabled.operator, original.operator)


if __name__ == '__main__': unittest.main()
