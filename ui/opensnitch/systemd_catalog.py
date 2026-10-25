"""Local, read-only systemd discovery. Never starts, stops or enables units."""

from dataclasses import dataclass, replace
from pathlib import Path
import os
import time

from PyQt5 import QtCore
try:
    from PyQt5 import QtDBus
except ImportError:
    QtDBus = None


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


class SystemdReader:
    """Blocking D-Bus reads, used exclusively by the discovery worker."""
    DESTINATION = 'org.freedesktop.systemd1'
    MANAGER = '/org/freedesktop/systemd1'
    METHODS = {'ListUnits', 'ListUnitFiles', 'LoadUnit', 'GetAll', 'GetUnitByPID'}

    def __init__(self, bus):
        self.bus = bus

    def call(self, path, interface, method, *args):
        if method not in self.METHODS:
            raise ValueError('Only service discovery methods are permitted')
        message = QtDBus.QDBusMessage.createMethodCall(self.DESTINATION, path, interface, method)
        message.setAutoStartService(False)
        message.setArguments(list(args))
        reply = self.bus.call(message, QtDBus.QDBus.Block, 1500)
        if reply.type() == QtDBus.QDBusMessage.ErrorMessage:
            raise OSError(reply.errorMessage())
        values = reply.arguments()
        return values[0] if values else None

    def manager(self, method, *args):
        return self.call(self.MANAGER, self.DESTINATION + '.Manager', method, *args)

    def properties(self, path, interface):
        return self.call(path, 'org.freedesktop.DBus.Properties', 'GetAll', interface)


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
                    owner = reader.manager('GetUnitByPID', QtDBus.QDBusArgument(pid, QtCore.QMetaType.UInt))
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


class _ServiceWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(int, object, object)

    def __init__(self, generation, parent):
        super().__init__(parent)
        self.generation = generation

    def run(self):
        records, errors = {}, {}
        if QtDBus is None:
            self.ready.emit(self.generation, records, {'system': 'Qt D-Bus support is unavailable.'})
            return
        for scope, bus in (('system', QtDBus.QDBusConnection.systemBus()), ('user', QtDBus.QDBusConnection.sessionBus())):
            if self.isInterruptionRequested():
                return
            if not bus.isConnected():
                errors[scope] = 'The {} D-Bus service manager is unavailable.'.format(scope)
                continue
            try:
                records.update(discover_scope(scope, SystemdReader(bus), self.isInterruptionRequested))
            except Exception as exc:
                errors[scope] = 'Systemd discovery unavailable: ' + str(exc)
        self.ready.emit(self.generation, records, errors)


class SystemdCatalog(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, auto_refresh=True, parent=None):
        super().__init__(parent or QtCore.QCoreApplication.instance())
        self.records, self.errors = {}, {}
        self.generation = 0
        self._serial = 0
        self._worker = None
        self._pending = False
        self._stopped = False
        self.auto_refresh = auto_refresh
        self.last_refresh = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(15000)
        self._timer.timeout.connect(self.refresh)
        QtCore.QCoreApplication.instance().aboutToQuit.connect(self.stop)

    def set_active(self, active):
        if active and self.auto_refresh:
            if not self._timer.isActive():
                self._timer.start()
            if time.monotonic() - self.last_refresh > 15:
                self.refresh()
        else:
            self._timer.stop()

    def refresh(self, fresh=False):
        if self._stopped or not self.auto_refresh:
            return self.generation
        if self._worker is not None:
            self._pending = self._pending or fresh
            return self._serial + (1 if fresh else 0)
        self._serial += 1
        self._worker = _ServiceWorker(self._serial, self)
        self._worker.ready.connect(self._accept)
        self._worker.finished.connect(self._finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()
        return self._serial

    @QtCore.pyqtSlot(int, object, object)
    def _accept(self, generation, records, errors):
        if not self._stopped:
            self.generation, self.records, self.errors = generation, records, errors
            self.last_refresh = time.monotonic()
            self.changed.emit()

    @QtCore.pyqtSlot()
    def _finished(self):
        self._worker = None
        if self._pending and not self._stopped:
            self._pending = False
            self.refresh()

    def stop(self):
        self._stopped = True
        self._timer.stop()
        if self._worker is not None:
            self._worker.requestInterruption()
            self._worker.wait()
