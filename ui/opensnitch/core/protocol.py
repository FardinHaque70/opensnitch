"""Protocol conversion without importing a generated binding or GUI toolkit."""

import json
from pathlib import Path


def to_proto(rule, message_type):
    result = message_type(name=rule.name, action=rule.action, duration=rule.duration,
                         enabled=rule.enabled, precedence=rule.precedence,
                         created=rule.created, description=rule.description, nolog=rule.nolog)
    def fill(target, value):
        target.type = value.get('type', '')
        target.operand = value.get('operand', '')
        target.data = value.get('data', '')
        target.sensitive = value.get('sensitive', False)
        for child in value.get('list', ()):
            fill(target.list.add(), child)
    fill(result.operator, rule.operator)
    return result



def rule_files_snapshot(configuration):
    """Track external writes without reading the daemon's root-only files."""
    config = json.loads(configuration or '{}')
    directory = Path(config.get('Rules', {}).get('Path') or '/etc/opensnitchd/rules')
    result = {}
    for path in directory.iterdir():
        if path.suffix == '.json':
            stat = path.stat()
            result[path.name] = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return result
