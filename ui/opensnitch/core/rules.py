"""Read-only rule presentation projected from authoritative daemon records."""
from dataclasses import dataclass, replace
import json


@dataclass(frozen=True)
class ApplicationMatch:
    executable: str = ''
    label: str = 'Application not specified'


def application_match(operator):
    """Identify one literal, case-sensitive executable without guessing from names."""
    paths, uncertain, has_match = set(), False, False
    def visit(condition):
        nonlocal uncertain, has_match
        kind, operand = condition.get('type'), condition.get('operand', '')
        children = condition.get('list', ())
        if operand == 'process.path':
            has_match = True
            path = condition.get('data', '')
            if kind == 'simple' and condition.get('sensitive') and path.startswith('/') and not children:
                paths.add(path)
            else:
                uncertain = True
        elif operand == 'process.command':
            has_match = True
        if children:
            if kind != 'list':
                uncertain = True
            for child in children:
                visit(child)
    visit(operator)
    if len(paths) == 1 and not uncertain:
        return ApplicationMatch(next(iter(paths)), '')
    # Lists are AND in daemon 1.7.2. Distinct path conditions are not proof
    # that a rule applies to multiple applications.
    return ApplicationMatch(label='Custom application match' if has_match else 'Application not specified')


@dataclass(frozen=True)
class Condition:
    kind: str
    operand: str
    data: str
    sensitive: bool
    children: tuple = ()

    @classmethod
    def from_operator(cls, operator):
        return cls(operator.get('type', ''), operator.get('operand', ''), operator.get('data', ''),
                   bool(operator.get('sensitive')), tuple(cls.from_operator(child) for child in operator.get('list', ())))

    @property
    def summary(self):
        if self.kind == 'list':
            return '(' + ' AND '.join(child.summary for child in self.children) + ')'
        comparison = ' matches ' if self.kind == 'regexp' else ' = ' if self.kind == 'simple' else ' [' + self.kind + '] '
        return self.operand + comparison + self.data


@dataclass(frozen=True)
class RuleRecord:
    node: str
    name: str
    executable: str
    application_label: str
    enabled: bool
    action: str
    duration: str
    precedence: bool
    nolog: bool
    created: int
    description: str
    condition: Condition
    json: str
    status: str

    @property
    def key(self):
        return self.node, self.name

    @property
    def conditions(self):
        return self.condition.summary


class RuleIndex:
    """Cache immutable presentation; the policy service remains the rule authority."""
    def __init__(self):
        self._cache = {}

    def project(self, rules_by_node, statuses):
        rows, keys = [], set()
        for node, rules in rules_by_node.items():
            for rule in rules:
                key = node, rule.name
                keys.add(key)
                cached = self._cache.get(key)
                status = statuses.get(node, 'Cached daemon snapshot')
                fingerprint = rule.fingerprint
                if cached and cached[0] == fingerprint:
                    record = cached[1]
                    if record.status != status:
                        record = replace(record, status=status)
                else:
                    match = application_match(rule.operator)
                    record = RuleRecord(node, rule.name, match.executable, match.label, rule.enabled, rule.action,
                        rule.duration, rule.precedence, rule.nolog, rule.created, rule.description,
                        Condition.from_operator(rule.operator), json.dumps(rule.__dict__, indent=2, ensure_ascii=False), status)
                self._cache[key] = fingerprint, record
                rows.append(record)
        self._cache = {key: value for key, value in self._cache.items() if key in keys}
        return tuple(sorted(rows, key=lambda row: (row.name, row.node)))
