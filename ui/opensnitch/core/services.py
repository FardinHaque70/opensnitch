"""Toolkit-independent service identities and conservative executable discovery."""
from dataclasses import dataclass, replace
from pathlib import Path
import os

@dataclass(frozen=True)
class ServiceIdentity:
    scope: str
    unit_name: str
    description: str = ''
    active_state: str = 'inactive'
    sub_state: str = ''
    file_state: str = ''
    configured_executable: str = ''
    executable_path: str = ''
    reason: str = ''
    main_pid: int = 0
    observed_executable: str = ''
    aliases: tuple = ()

    @property
    def key(self):
        return self.scope, self.unit_name


WRAPPERS = {'sh', 'bash', 'dash', 'zsh', 'fish', 'env', 'java', 'node', 'nodejs',
            'perl', 'ruby', 'mono', 'flatpak', 'systemd-run', 'docker', 'podman', 'busybox',
            'setsid', 'nice', 'nohup', 'runuser', 'su', 'sudo', 'pkexec', 'chroot',
            'bwrap', 'firejail', 'start-stop-daemon'}


def executable_association(properties, inspect_binary=None):
    """Return configured path, verifiable host executable, or a read-only reason."""
    commands = properties.get('ExecStart', ())
    if len(commands) != 1 or not commands[0]:
        return '', '', 'Multiple or missing start commands; no single executable can be controlled.'
    configured = str(commands[0][0])
    if not os.path.isabs(configured) or '%' in configured:
        return configured, '', 'The executable path is unresolved.'
    if any(properties.get(field) for field in ('RootDirectory', 'RootImage', 'MountImages',
                                               'ExtensionImages', 'BindPaths', 'BindReadOnlyPaths')):
        return configured, '', 'This service uses a filesystem mapping; its host executable is uncertain.'
    name = Path(configured).name
    if name in WRAPPERS or name.startswith(('python', 'pypy')):
        return configured, '', 'A shared interpreter or launcher cannot identify this service executable.'
    if inspect_binary is None:
        def inspect_binary(path):
            if not os.access(path, os.X_OK):
                return False
            with open(path, 'rb') as stream:
                return stream.read(4) == b'\x7fELF'
    try:
        canonical = os.path.realpath(configured)
        if not inspect_binary(canonical):
            return configured, '', 'Scripts, wrappers and non-executable files require manual rule review.'
    except OSError:
        return configured, '', 'The configured executable is unavailable or unreadable.'
    return configured, canonical, ''


def shared_services(records, executable):
    return tuple(sorted((record.key for record in records.values()
                         if executable and record.executable_path == executable)))


def discover_scope(scope, reader, interrupted=lambda: False):
    loaded = {row[0]: row for row in reader.manager('ListUnits') if row[0].endswith('.service')}
    files = {Path(row[0]).name: row[1] for row in reader.manager('ListUnitFiles')
             if str(row[0]).endswith('.service')}
    records = {}
    for name in sorted(set(loaded) | set(files)):
        if interrupted():
            break
        row = loaded.get(name)
        base = ServiceIdentity(scope, name, row[1] if row else name,
                               row[3] if row else 'inactive', row[4] if row else '', files.get(name, ''))
        if name.endswith('@.service'):
            records[base.key] = replace(base, active_state='template', reason='A service template needs an instantiated unit.')
            continue
        try:
            path = str(row[6]) if row else str(reader.manager('LoadUnit', name))
            unit = reader.properties(path, reader.DESTINATION + '.Unit')
            properties = reader.properties(path, reader.DESTINATION + '.Service')
            canonical_name = unit.get('Id') or name
            configured, executable, reason = executable_association(properties)
            pid = int(properties.get('MainPID', 0))
            observed = ''
            if pid:
                try:
                    process = Path('/proc') / str(pid) / 'exe'
                    observed = os.readlink(str(process))
                    # GetUnitByPID validates that this PID still belongs to the
                    # same manager/unit, rather than trusting a recycled PID.
                    owner = reader.unit_for_pid(pid)
                    if str(owner) != path:
                        executable, reason = '', 'The observed process no longer belongs to this service.'
                    elif executable:
                        # Compare inode/device, not mutable file access times.
                        left, right = process.stat(), Path(executable).stat()
                        if observed != executable or (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
                            executable, reason = '', 'Configured and observed executables differ; review its rules manually.'
                except (OSError, TypeError):
                    if observed:
                        executable, reason = '', 'The observed process could not be verified; review its rules manually.'
                    # Restricted /proc is not evidence of a different binary.
            aliases = tuple(sorted(set(unit.get('Names', ())) - {canonical_name}))
            record = replace(base, unit_name=canonical_name, description=unit.get('Description') or base.description,
                active_state=unit.get('ActiveState', base.active_state), sub_state=unit.get('SubState', base.sub_state),
                configured_executable=configured, executable_path=executable, reason=reason,
                main_pid=pid, observed_executable=observed, aliases=aliases)
            records[record.key] = record
        except (OSError, ValueError, TypeError, IndexError) as exc:
            records[base.key] = replace(base, reason='Service details unavailable: ' + str(exc))
    return records


class ServiceAccessController:
    """Verify fresh service associations before using the shared policy controller."""
    def __init__(self, provider, request_policy, changed=lambda: None, message=lambda text: None):
        self.provider = provider
        self.request_policy = request_policy
        self.changed, self.message = changed, message
        self.pending = None
        self._serial = 0

    def cancel(self):
        self._serial += 1
        self.pending = None

    def request(self, key, node, action, duration='always'):
        import time
        record = self.provider.records.get(key)
        if self.pending or node != 'unix:/local' or not record or record.reason or not record.executable_path:
            return False
        expected = record.executable_path
        shared = shared_services(self.provider.records, expected)
        self._serial += 1
        serial, started = self._serial, time.monotonic()
        self.pending = key
        self.changed()
        def verified():
            if serial != self._serial:
                return
            self._serial += 1
            self.pending = None
            current = self.provider.records.get(key)
            valid = (time.monotonic() - started < 30 and current and not current.reason
                     and current.executable_path == expected and not self.provider.errors.get(current.scope)
                     and shared_services(self.provider.records, expected) == shared)
            if valid:
                self.request_policy((node, expected), action, duration)
            else:
                self.message('Service associations changed or could not be verified. Review the executable and shared services before trying again.')
            self.changed()
        self.provider.refresh(verified)
        return True
