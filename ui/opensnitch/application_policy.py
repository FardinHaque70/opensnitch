"""Conservative summaries of application rules, separate from enforcement."""

from dataclasses import dataclass
import hashlib
import json
import re


def operator_dict(operator):
    return {'type': operator.type, 'operand': operator.operand, 'data': operator.data,
            'sensitive': operator.sensitive, 'list': [operator_dict(o) for o in operator.list]}


def managed_rule_name(path):
    return 'opensnitch-ui-app-' + hashlib.sha256(path.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class ApplicationRule:
    name: str
    action: str
    duration: str
    enabled: bool
    precedence: bool
    operator: dict
    created: int = 0
    description: str = ''
    nolog: bool = False

    @classmethod
    def from_proto(cls, rule):
        return cls(rule.name, rule.action, rule.duration, rule.enabled, rule.precedence,
                   operator_dict(rule.operator), rule.created, rule.description, rule.nolog)

    @property
    def policy_fingerprint(self):
        # The daemon regenerates Created in Deserialize(); it is not evidence
        # that the configured rule changed outside the frontend.
        document = dict(self.__dict__)
        document.pop('created')
        return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    @property
    def fingerprint(self):
        # Include fields whose external modification must prevent overwrites.
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()


def executable_paths(operator):
    paths = set()
    if operator.get('type') == 'simple' and operator.get('operand') == 'process.path':
        paths.add(operator.get('data', ''))
    for child in operator.get('list', ()):
        paths.update(executable_paths(child))
    return {path for path in paths if path.startswith('/')}


def references_executable(operator, path):
    if operator.get('operand') == 'process.path':
        if operator.get('type') == 'simple':
            value = operator.get('data', '')
            return value == path if operator.get('sensitive') else value.casefold() == path.casefold()
        if operator.get('type') == 'regexp':
            try:
                flags = 0 if operator.get('sensitive') else re.IGNORECASE
                return re.search(operator.get('data', ''), path, flags) is not None
            except re.error:
                return True  # Uncertain application match: do not offer quick controls.
    return any(references_executable(child, path) for child in operator.get('list', ()))


@dataclass(frozen=True)
class PolicySummary:
    group: str
    rules: tuple
    global_rules: tuple


class ApplicationPolicyService:
    GROUPS = ('Custom rules', 'Temporary rules', 'Allowed', 'Blocked', 'Not configured')

    def __init__(self):
        self.rules = {}

    def replace_rules(self, node, rules):
        self.rules[node] = tuple(rules)

    def summarize(self, node, path):
        active = [rule for rule in self.rules.get(node, ()) if rule.enabled]
        associated = tuple(rule for rule in active if references_executable(rule.operator, path))
        global_rules = tuple(rule for rule in active if not self._has_application_match(rule.operator))
        if not associated:
            return PolicySummary('Not configured', (), global_rules)
        if len(associated) != 1:
            return PolicySummary('Custom rules', associated, global_rules)
        rule = associated[0]
        operator = rule.operator
        if (operator.get('type') != 'simple' or operator.get('operand') != 'process.path'
                or operator.get('data') != path or rule.action not in ('allow', 'deny', 'reject')):
            return PolicySummary('Custom rules', associated, global_rules)
        if rule.duration != 'always':
            return PolicySummary('Temporary rules', associated, global_rules)
        return PolicySummary('Allowed' if rule.action == 'allow' else 'Blocked', associated, global_rules)

    @staticmethod
    def _has_application_match(operator):
        return (operator.get('operand') == 'process.path'
                or any(ApplicationPolicyService._has_application_match(o) for o in operator.get('list', ())))

    def rule_by_name(self, node, name):
        return next((rule for rule in self.rules.get(node, ()) if rule.name == name), None)

    def application_rules(self, node, path):
        return tuple(rule for rule in self.rules.get(node, ()) if references_executable(rule.operator, path))

    def simple_application_rule(self, node, path):
        related = self.application_rules(node, path)
        if len(related) != 1:
            return None
        rule = related[0]
        operator = rule.operator
        if (operator.get('type') == 'simple' and operator.get('operand') == 'process.path'
                and operator.get('data') == path and not operator.get('list')
                and rule.action in ('allow', 'deny', 'reject') and '/' not in rule.name):
            return rule
        return None

    def quick_control_reason(self, entry):
        if entry.node_id not in self.rules:
            return 'The current application rules are not available yet.'
        related = self.application_rules(entry.node_id, entry.executable_path)
        owner = entry.ownership
        adopted = owner.get('adopted') and owner.get('executable_path') == entry.executable_path
        if owner and owner.get('name') != managed_rule_name(entry.executable_path) and not adopted:
            return 'Rule ownership does not match this executable. Review its rules.'
        if owner.get('uncertain'):
            return 'Previous operation is unconfirmed. Review the rules before making another change.'
        if related and self.simple_application_rule(entry.node_id, entry.executable_path) is None:
            return 'Custom or conflicting rules apply. Use Manage rules to change this application’s access.'
        if owner:
            rule = self.rule_by_name(entry.node_id, owner.get('name'))
            if rule is None or (rule.policy_fingerprint != owner['policy_fingerprint'] if owner.get('policy_fingerprint') else rule.fingerprint != owner.get('fingerprint')):
                return 'This rule changed outside Apps. Reconnect to refresh its policy before changing access.'
            if not related or related[0].name != rule.name:
                return 'Rule ownership does not match this executable. Review its rules.'
        return ''


def describe_rule(rule):
    """Readable conditions; this does not predict a connection's winning rule."""
    labels = {'dest.port': 'Destination port', 'dest.ip': 'Destination IP', 'dest.host': 'Destination host',
              'dest.network': 'Destination network', 'user.id': 'User ID', 'protocol': 'Protocol',
              'process.path': 'Executable', 'process.command': 'Command line', 'process.id': 'Process ID'}
    def describe(operator):
        if operator.get('type') == 'list':
            return ' AND '.join(describe(child) for child in operator.get('list', ()))
        return labels.get(operator.get('operand'), operator.get('operand', 'Condition')) + (
            ' matches ' if operator.get('type') == 'regexp' else ': ') + operator.get('data', '')
    return {'allow': 'Allow', 'deny': 'Block', 'reject': 'Reject'}.get(rule.action, rule.action) + ' · ' + describe(rule.operator) + (
        ' · Priority rule' if rule.precedence else '')
