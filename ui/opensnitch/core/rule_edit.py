"""Lossless drafts and conservative validation for daemon 1.7.2 rule updates."""
from decimal import Decimal
from functools import lru_cache
import ipaddress
import json
from pathlib import Path
import re
import pwd
import time
from opensnitch.application_policy import ApplicationRule

OPERANDS = ('process.path', 'process.parent.path', 'process.command', 'process.id', 'user.id', 'user.name',
    'source.ip', 'source.port', 'source.network', 'dest.ip', 'dest.host', 'dest.port', 'dest.network',
    'protocol', 'iface.in', 'iface.out', 'process.hash.md5', 'process.hash.sha1',
    'lists.domains', 'lists.domains_regexp', 'lists.ips', 'lists.nets', 'lists.hash.md5', 'true')
KINDS = ('simple', 'regexp', 'network', 'lists')
DURATION_PART = re.compile(r'([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|µs|μs|ms|s|m|h)')
UNITS = {'ns': Decimal('0.000000001'), 'us': Decimal('0.000001'), 'µs': Decimal('0.000001'),
         'μs': Decimal('0.000001'), 'ms': Decimal('0.001'), 's': Decimal(1), 'm': Decimal(60), 'h': Decimal(3600)}


def duration_seconds(duration):
    if duration in ('always', 'until restart', 'once'):
        return None
    parts = list(DURATION_PART.finditer(duration))
    if not parts or ''.join(part.group() for part in parts) != duration or len(duration) > 64:
        raise ValueError('Use always, until restart, once, or a positive duration such as 1h30m.')
    seconds = sum((Decimal(part[1]) * UNITS[part[2]] for part in parts), Decimal(0))
    if not Decimal(0) < seconds <= Decimal('9223372036.854775807'):
        raise ValueError('Duration is outside the daemon’s supported range.')
    return float(seconds)


def transport_edit_reason(operator):
    if operator.get('type') == 'list':
        if operator.get('data') or operator.get('operand') not in ('', 'list') or not operator.get('list'):
            return 'This legacy or empty condition group cannot be safely rewritten by daemon 1.7.2.'
        children = operator['list']
    else:
        children = (operator,)
    for child in children:
        if child.get('type') not in KINDS or child.get('list'):
            return 'Daemon 1.7.2 cannot safely round-trip nested or unknown condition structures. Use the Qt/disk workflow for this rule.'
        operand = child.get('operand', '')
        if operand == 'user.name' and re.fullmatch('[0-9]+', child.get('data', '')):
            return 'Daemon 1.7.2 returns resolved user names as numeric IDs, which cannot be safely rewritten as user names. Inspect/delete this rule or create a separate User ID rule.'
        if operand not in OPERANDS and not (operand.startswith('process.env.') and len(operand) > 12):
            return 'This rule uses an unknown operand; its conditions are preserved read-only.'
    return ''


def editable_conditions(operator):
    reason = transport_edit_reason(operator)
    if reason:
        raise ValueError(reason)
    return json.loads(json.dumps(operator['list'] if operator.get('type') == 'list' else [operator]))


@lru_cache(maxsize=1)
def network_aliases():
    path = Path(__file__).resolve().parents[1] / 'utils/network_aliases/network_aliases.json'
    return frozenset(json.loads(path.read_text()))


def validate_regexp(pattern):
    # Existing daemon-accepted expressions are retained verbatim. New/changed
    # expressions use a portable subset; Go's validator remains authoritative.
    if re.search(r'\\(?:[0-9]|[gkNuUZ])|\(\?(?!:|[ims-]+[:)])|(?:\*|\+|\?|\})\+', pattern):
        raise ValueError('Use a portable RE2 expression without lookaround, backreferences or advanced group extensions.')
    escaped = re.findall(r'\\([A-Za-z])', pattern)
    if any(value not in 'AbBdsSwWafnrtvx' for value in escaped):
        raise ValueError('This new regex escape is not supported by the portable editor validator. Existing expressions can be retained unchanged.')
    product = 1
    for count, upper in re.findall(r'(?<!\\)\{(\d+)(?:,(\d*))?\}', pattern):
        maximum = int(upper or count)
        product *= max(1, maximum)
        if maximum > 1000 or product > 1000:
            raise ValueError('RE2 repetition counts must stay within 1000.')
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError('Invalid regular expression: ' + str(error)) from error


def validate_condition(condition, accepted=()):
    kind, operand, data = (condition.get(field) for field in ('type', 'operand', 'data'))
    if kind not in KINDS or not isinstance(operand, str) or not isinstance(data, str) or condition.get('list'):
        raise ValueError('Choose a supported leaf condition.')
    if not isinstance(condition.get('sensitive', False), bool):
        raise ValueError('Case sensitivity must be a boolean.')
    if operand not in OPERANDS and not (operand.startswith('process.env.') and len(operand) > 12):
        raise ValueError('Unknown match field.')
    # Preserve server-accepted values, including Go-only regex syntax and aliases.
    if any(condition == value for value in accepted):
        return
    if operand == 'true':
        if kind != 'simple': raise ValueError('Any connection uses a simple condition.')
        return
    if kind == 'network':
        if operand not in ('source.network', 'dest.network'):
            raise ValueError('Network matching requires a source or destination network field.')
        try:
            ipaddress.ip_network(data, strict=False)
        except ValueError:
            if data not in network_aliases():
                raise ValueError('Enter a CIDR network or a bundled network alias.')
    elif kind == 'lists':
        if not operand.startswith('lists.') or not data.startswith('/'):
            raise ValueError('List matching requires a supported list field and an absolute directory path.')
    elif operand.startswith('lists.') or operand in ('source.network', 'dest.network'):
        raise ValueError('Use the matching Network or List files operator for this field.')
    elif kind == 'regexp':
        validate_regexp(data)
    elif kind == 'simple':
        if operand in ('source.port', 'dest.port') and (not re.fullmatch('[0-9]+', data) or not 0 <= int(data) <= 65535):
            raise ValueError('Ports must be numbers from 0 to 65535; use a regex for multiple ports.')
        if operand in ('user.id', 'process.id') and (not re.fullmatch('[0-9]+', data) or int(data) > 4294967295):
            raise ValueError('Enter a non-negative numeric identifier.')
        if operand == 'user.name':
            try:
                pwd.getpwnam(data)
            except KeyError as error:
                raise ValueError('This local user name does not exist. Use User ID for a numeric account identifier.') from error
        if operand in ('source.ip', 'dest.ip'):
            try: ipaddress.ip_address(data)
            except ValueError as error: raise ValueError('Enter an IP address or choose Regex.') from error
        if operand in ('process.path', 'process.parent.path') and not data.startswith('/'):
            raise ValueError('An executable path must be absolute.')
        if operand in ('process.hash.md5', 'process.hash.sha1'):
            length = 32 if operand.endswith('md5') else 40
            if not re.fullmatch('[0-9a-fA-F]{' + str(length) + '}', data):
                raise ValueError('Enter a complete hexadecimal checksum.')


def validate_identifier(name):
    if (not isinstance(name, str) or not name or name != name.strip() or name.startswith('.')
            or len(name.encode('utf-8')) > 250 or any(char in name for char in '/\\\0\r\n')):
        raise ValueError('Use a non-empty rule identifier without path separators, leading dots or newlines (maximum 250 bytes).')


def validate_rule(rule, original=None):
    validate_identifier(rule.name)
    if not all(isinstance(getattr(rule, field), bool) for field in ('enabled', 'precedence', 'nolog')):
        raise ValueError('Rule flags must be booleans.')
    if original and original.name != rule.name:
        raise ValueError('Identifiers stay fixed while editing. Duplicate the rule to create a new identifier.')
    if rule.action not in ('allow', 'deny', 'reject'):
        raise ValueError('Choose Allow, Block or Reject.')
    duration_seconds(rule.duration)
    conditions = editable_conditions(rule.operator)
    accepted = editable_conditions(original.operator) if original and (original.enabled or not rule.enabled or (rule.operator == original.operator and rule.duration == original.duration)) else ()
    for condition in conditions:
        validate_condition(condition, accepted)
    return rule


def draft_rule(name, action, duration, enabled, precedence, nolog, description, conditions, original=None):
    if not conditions:
        raise ValueError('Add at least one condition. Choose “Any connection” explicitly for an unrestricted rule.')
    conditions = json.loads(json.dumps(conditions))
    if original and original.operator.get('type') == 'list':
        operator = dict(original.operator, list=conditions)
    elif len(conditions) == 1:
        operator = conditions[0]
    else:
        operator = dict(type='list', operand='list', data='', sensitive=False, list=conditions)
    rule = ApplicationRule(name, action, duration, enabled, precedence, operator,
        original.created if original else int(time.time()), description, nolog)
    return validate_rule(rule, original)


def rule_from_json(raw):
    return ApplicationRule(**json.loads(raw))
