"""Connection-prompt rule construction, independent of widgets and transport."""
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import time
import uuid
from slugify import slugify
from opensnitch.application_policy import ApplicationRule


@dataclass(frozen=True)
class Target:
    label: str
    type: str
    operand: str
    data: str

    @property
    def operator(self):
        return dict(type=self.type, operand=self.operand, data=self.data, sensitive=False, list=[])


def targets(connection):
    c = connection
    values = [Target('This executable', 'simple', 'process.path', c.process_path),
              Target('This command line', 'simple', 'process.command', ' '.join(c.process_args)),
              Target('Destination port ' + str(c.dst_port), 'simple', 'dest.port', str(c.dst_port)),
              Target('Destination IP ' + c.dst_ip, 'simple', 'dest.ip', c.dst_ip),
              Target('User ' + str(c.user_id), 'simple', 'user.id', str(c.user_id)),
              Target('Process ID ' + str(c.process_id), 'simple', 'process.id', str(c.process_id))]
    if c.process_path.startswith('/tmp/.mount_'):
        mount = Path(c.process_path).parts[2]
        prefix = re.escape('/tmp/' + mount[:-6])
        pattern = '^' + prefix + r'[0-9A-Za-z]{6,7}/.*' + re.escape(Path(c.process_path).name) + '$'
        values.append(Target('This AppImage across mounts', 'regexp', 'process.path', pattern))
    try:
        ip = ipaddress.ip_address(c.dst_ip)
        aliases = Path(__file__).resolve().parents[1] / 'utils/network_aliases/network_aliases.json'
        for alias, networks in json.loads(aliases.read_text()).items():
            if any(ip in ipaddress.ip_network(network) for network in networks):
                values.append(Target('Destination ' + alias, 'network', 'dest.network', alias))
                break
        for prefix in ((24, 16, 8) if ip.version == 4 else (64, 128)):
            network = str(ipaddress.ip_network(c.dst_ip + '/' + str(prefix), strict=False))
            values.append(Target('Destination network ' + network, 'network', 'dest.network', network))
        if ip.version == 4:
            parts = c.dst_ip.split('.')
            for length in range(1, 4):
                pattern = r'\.'.join(parts[:length]) + r'\..*'
                values.append(Target('Destination ' + '.'.join(parts[:length]) + '.*', 'regexp', 'dest.ip', pattern))
    except (ValueError, OSError):
        pass
    if c.dst_host and c.dst_host != c.dst_ip:
        values.append(Target('Destination host ' + c.dst_host, 'simple', 'dest.host', c.dst_host))
        parts = c.dst_host.split('.')[1:]
        for start in range(max(0, len(parts) - 1)):
            host = '.'.join(parts[start:])
            values.append(Target('Destination *.' + host, 'regexp', 'dest.host', r'^(|.*\.)' + re.escape(host) + '$'))
    return tuple(values)


def build_rule(connection, action, duration, target, extra=()):
    if action not in ('allow', 'deny', 'reject'):
        raise ValueError('Invalid prompt action')
    from .settings import DURATIONS
    if duration not in DURATIONS or not target.data:
        raise ValueError('Choose a valid target and duration')
    operators = [op for op in extra if op.data and (op.operand, op.data) != (target.operand, target.data)]
    if target.operand == 'process.command':
        first = connection.process_args[0] if connection.process_args else ''
        if (not Path(first).is_absolute() or first.startswith('/proc/')) and connection.process_path:
            operators.append(Target('Executable', 'simple', 'process.path', connection.process_path))
    if target.type == 'network' and '/' not in target.data and connection.process_path:
        operators.append(Target('Executable', 'simple', 'process.path', connection.process_path))
    children = [value.operator for value in operators] + [target.operator]
    operator = target.operator if len(children) == 1 else dict(type='list', operand='list', data='', sensitive=False, list=children)
    # Unique names prevent a new prompt from overwriting an existing user rule.
    name = slugify(action + ' ' + duration + (' list ' if len(children) > 1 else ' simple ') + target.data)[:95]
    return ApplicationRule(name + '-' + uuid.uuid4().hex[:12], action, duration, True, False,
                           operator, int(time.time()))


def checksum_mismatches(connection, rules):
    actual = connection.process_checksums.get('process.hash.md5', '')
    if not actual:
        return ()
    def visit(operator):
        if operator.get('operand') == 'process.hash.md5' and operator.get('data') != actual:
            return (operator.get('data', ''),)
        return tuple(value for child in operator.get('list', ()) for value in visit(child))
    return tuple((rule.name, expected) for rule in rules if rule.enabled
                 for expected in visit(rule.operator))
