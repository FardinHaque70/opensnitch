"""Temporary Qt adapter over shared service discovery. Never starts or stops units."""
import time
from PyQt5 import QtCore
try:
    from PyQt5 import QtDBus
except ImportError:
    QtDBus = None
from .core.services import ServiceIdentity, executable_association, shared_services, discover_scope


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

    def unit_for_pid(self, pid):
        return self.manager('GetUnitByPID', QtDBus.QDBusArgument(pid, QtCore.QMetaType.UInt))

    def properties(self, path, interface):
        return self.call(path, 'org.freedesktop.DBus.Properties', 'GetAll', interface)


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

class ServiceVerificationAdapter(QtCore.QObject):
    """Translate Qt discovery generations into shared verification callbacks."""
    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self._callbacks = []
        provider.changed.connect(self._ready)

    @property
    def records(self):
        return self.provider.records

    @property
    def errors(self):
        return self.provider.errors

    def refresh(self, callback):
        target = self.provider.refresh(fresh=True)
        self._callbacks.append((target, callback))

    def _ready(self):
        ready = [callback for generation, callback in self._callbacks if generation <= self.provider.generation]
        self._callbacks = [(generation, callback) for generation, callback in self._callbacks if generation > self.provider.generation]
        for callback in ready:
            callback()
