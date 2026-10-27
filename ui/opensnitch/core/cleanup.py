"""Reviewed cleanup of unavailable temporary apps through existing rule operations."""
from dataclasses import asdict, dataclass
import errno
import json
import os
from pathlib import Path
import re
import time
import uuid
from .catalog import atomic_write
from opensnitch.application_policy import references_executable


@dataclass(frozen=True)
class CleanupPlan:
    entries: tuple
    rules: tuple
    protected: tuple = ()


def missing(path):
    try:
        os.stat(path)
    except OSError as error:
        return error.errno in (errno.ENOENT, errno.ENOTDIR)
    return False


def literal_prefix(pattern):
    if not pattern.startswith('^'):
        return ''
    result, escaped = '', False
    for char in pattern[1:]:
        if escaped:
            if char.isalnum():
                break  # \d and other regex escapes are not literal path text.
            result += char; escaped = False
        elif char == '\\':
            escaped = True
        elif char in '.*+?[](){}|$^':
            break
        else:
            result += char
    return result


def active_appimage_mounts():
    try:
        result = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            path = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), line.split()[4])
            if path.startswith('/tmp/.mount_'):
                result.append(path)
        return tuple(result)
    except (OSError, IndexError):
        return None  # An unreadable mount list must not imply no active mounts.


def build_plan(entries, rules, mounts=None):
    """Run filesystem inspection in a worker; never infer policy from rule names."""
    entries = tuple(entries)
    stale = tuple(entry for entry in entries if entry.node_id == 'unix:/local' and not entry.hidden
                  and entry.executable_path.startswith('/tmp/') and missing(entry.executable_path))
    keys = {entry.key for entry in stale}
    selected, protected = [], []
    for rule in rules:
        matched = [entry for entry in stale if references_executable(rule.operator, entry.executable_path)]
        if not matched:
            continue
        op = rule.operator
        reason = ''
        prefix = literal_prefix(op.get('data', '')) if op.get('type') == 'regexp' else ''
        if op.get('operand') != 'process.path' or op.get('list'):
            reason = 'Compound or non-executable rule; review it in Rules.'
        elif op.get('type') == 'simple':
            if not op.get('data', '').startswith('/tmp/'):
                reason = 'The rule does not exclusively target a temporary executable.'
        elif op.get('type') == 'regexp':
            if (not prefix.startswith('/tmp/.mount_') or len(prefix) <= len('/tmp/.mount_')
                    or not op.get('data', '').endswith('$') or re.search(r'(?<!\\)\|', op.get('data', ''))):
                reason = 'The executable pattern is broader than an anchored AppImage mount.'
            elif mounts is None:
                reason = 'Active AppImage mounts could not be verified.'
            elif any((mount.casefold().startswith(prefix.casefold()) or prefix.casefold().startswith(mount.casefold() + '/')) for mount in mounts):
                reason = 'An AppImage mount covered by this pattern is currently active.'
        else:
            reason = 'Unsupported executable matcher; review it in Rules.'
        if any(entry.node_id == 'unix:/local' and entry.key not in keys
               and (not entry.hidden or not missing(entry.executable_path))
               and references_executable(rule.operator, entry.executable_path) for entry in entries):
            reason = 'This rule also matches another inventory entry; it will be preserved.'
        if reason:
            protected.append((rule.name, reason))
        else:
            selected.append(rule)
    return CleanupPlan(stale, tuple(selected), tuple(protected))


class CleanupJob:
    """Sequential acknowledged deletion, fresh-subscription checks, then list removal."""
    def __init__(self, session, plan, changed):
        self.session, self.plan, self.changed = session, plan, changed
        self.index = 0
        self.pending = self.finished = False
        self.error = ''
        self.removed = 0
        self.backup = None
        self.deadline = time.monotonic() + 90

    def start(self):
        session = self.session
        reason = session.controller.rule_reason('unix:/local')
        if reason:
            return self.fail(reason)
        for entry in self.plan.entries:
            current = session.catalog.entries.get(entry.key)
            if current != entry or not missing(entry.executable_path):
                return self.fail('An application changed or is available again. Reload cleanup.')
        for rule in self.plan.rules:
            current = session.policies.rule_by_name('unix:/local', rule.name)
            if current is None or current.policy_fingerprint != rule.policy_fingerprint:
                return self.fail('A matching rule changed. Reload cleanup before removing it.')
        current = build_plan(tuple(session.catalog.entries.values()), session.policies.rules.get('unix:/local', ()), active_appimage_mounts())
        if {rule.name for rule in self.plan.rules} - {rule.name for rule in current.rules}:
            return self.fail('A rule is now shared or its AppImage is active. Reload cleanup.')
        directory = session.directory / 'cleanup-backups'
        self.backup = directory / (uuid.uuid4().hex + '.json')
        try:
            directory.mkdir(exist_ok=True)
            atomic_write(self.backup, json.dumps(dict(version=1, entries=[asdict(entry) for entry in self.plan.entries],
                rules=[asdict(rule) for rule in self.plan.rules]), indent=2).encode())
        except OSError:
            return self.fail('The cleanup backup could not be saved; nothing was removed.')
        self.tick()

    def fail(self, message):
        self.error = message
        self.finished = True
        self.changed()
        return False

    def tick(self):
        if self.finished or self.pending:
            return
        if time.monotonic() > self.deadline:
            self.fail('Cleanup is incomplete; refresh and review the remaining rules. The backup is preserved.')
            return
        session = self.session
        if session.controller.rule_reason('unix:/local'):
            return  # Wait for the daemon's fresh subscription after each deletion.
        if any(session.policies.rule_by_name('unix:/local', rule.name) for rule in self.plan.rules[:self.index]):
            self.fail('The daemon still reports a deleted rule. Entries were retained; refresh and review.')
            return
        if self.index < len(self.plan.rules):
            expected = self.plan.rules[self.index]
            current = session.policies.rule_by_name('unix:/local', expected.name)
            if current is None:
                self.fail('A rule disappeared outside this cleanup. Reload and review the remaining entries.')
                return
            self.pending = True
            self.changed()
            def completed(success, error):
                self.pending = False
                if success:
                    self.index += 1
                    self.changed()
                else:
                    self.fail(error or 'The daemon did not confirm deletion. Entries were retained.')
            if not session.request_rule('unix:/local', current, expected, 'delete', completed):
                self.pending = False
                self.fail(session.controller.last_error)
            return
        # Every rule deletion has been followed by a verified fresh subscription.
        removable = [entry for entry in self.plan.entries if missing(entry.executable_path)
                     and not any(references_executable(rule.operator, entry.executable_path)
                                 for rule in session.policies.rules.get(entry.node_id, ()))]
        prior = {}
        for entry in removable:
            current = session.catalog.entries.get(entry.key)
            if current and not current.hidden:
                prior[entry.key] = current.hidden
                current.hidden = True
        if prior:
            session.catalog._dirty = True
            if not session.catalog.flush():
                for key, value in prior.items():
                    session.catalog.entries[key].hidden = value
                session.catalog._dirty = True
                self.fail('Rules were removed, but the Apps list could not be saved. The backup is preserved.')
                return
        self.removed = len(prior)
        self.finished = True
        session.publish()
        self.changed()
