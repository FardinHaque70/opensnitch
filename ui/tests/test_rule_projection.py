"""Inspection never turns patterns or generated names into exact application IDs."""
from dataclasses import replace
import json
import subprocess
import sys
import unittest
from opensnitch.application_policy import ApplicationRule
from opensnitch.core.rules import application_match, Condition, RuleIndex


def exact(path='/usr/bin/example', sensitive=True):
    return dict(type='simple', operand='process.path', data=path, sensitive=sensitive, list=[])


class RuleProjectionTests(unittest.TestCase):
    def test_exact_app_and_conjunction_keep_one_executable(self):
        self.assertEqual(application_match(exact()).executable, '/usr/bin/example')
        compound = dict(type='list', list=[exact(), dict(type='simple', operand='dest.port', data='443')])
        self.assertEqual(application_match(compound).executable, '/usr/bin/example')
        compound['list'].append(dict(type='list', list=[exact()]))
        self.assertEqual(application_match(compound).executable, '/usr/bin/example')

    def test_patterns_casefold_conflicting_paths_and_commands_are_neutral(self):
        cases = [dict(exact(), type='regexp'), exact(sensitive=False), exact('relative'),
                 dict(type='list', list=[exact('/one'), exact('/two')]),
                 dict(type='simple', operand='process.command', data='example --args'),
                 dict(type='unsupported', list=[exact()])]
        for condition in cases:
            with self.subTest(condition=condition):
                match = application_match(condition)
                self.assertEqual(match.executable, '')
                self.assertEqual(match.label, 'Custom application match')
        match = application_match(dict(type='simple', operand='dest.port', data='443'))
        self.assertEqual(match.label, 'Application not specified')

    def test_condition_tree_retains_types_flags_empty_values_and_nested_and(self):
        operator = dict(type='list', operand='list', data='', sensitive=False,
            list=[exact(), dict(type='list', list=[dict(type='regexp', operand='dest.host', data='^$', sensitive=True)])])
        tree = Condition.from_operator(operator)
        self.assertEqual(tree.children[1].children[0].data, '^$')
        self.assertTrue(tree.children[1].children[0].sensitive)
        self.assertIn(' AND ', tree.summary)
        self.assertIn('dest.host matches ^$', tree.summary)

    def test_projected_rule_keeps_raw_fields_key_and_source_status(self):
        rule = ApplicationRule('allow-12h-simple-opaque', 'reject', '12h', False, True, exact(),
            123, 'Original description', True)
        index = RuleIndex()
        local, remote = index.project({'local': [rule], 'remote': [rule]}, {'local': 'Current', 'remote': 'Cached'})
        self.assertNotEqual(local.key, remote.key)
        self.assertEqual(json.loads(local.json), rule.__dict__)
        self.assertFalse(local.enabled)
        self.assertTrue(local.nolog and local.precedence)
        again = index.project({'local': [rule], 'remote': [rule]}, {'local': 'Current', 'remote': 'Cached'})
        self.assertIs(again[0], local)
        changed = index.project({'local': [replace(rule, action='allow')]}, {'local': 'Waiting'})[0]
        self.assertEqual(changed.key, local.key)
        self.assertEqual(changed.status, 'Waiting')
        self.assertEqual(changed.action, 'allow')
        self.assertEqual(len(index._cache), 1)

    def test_json_and_tree_are_immutable_even_if_source_operator_changes(self):
        operator = exact()
        rule = ApplicationRule('rule', 'allow', 'always', True, False, operator)
        index = RuleIndex(); before = index.project({'local': [rule]}, {})[0]
        operator['data'] = '/different'
        after = index.project({'local': [rule]}, {})[0]
        self.assertEqual(before.executable, '/usr/bin/example')
        self.assertEqual(before.condition.data, '/usr/bin/example')
        self.assertEqual(after.executable, '/different')

    def test_core_rule_inspection_has_no_ui_dependencies(self):
        subprocess.run([sys.executable, '-c', "import opensnitch.core.rules,opensnitch.core.backend,sys; assert not any(n.startswith(('gi','PyQt')) for n in sys.modules)"], check=True)


if __name__ == '__main__':
    unittest.main()
