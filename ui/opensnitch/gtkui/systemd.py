"""Gio D-Bus adapter. Blocking reads run on one discovery worker."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event as StopEvent
from gi.repository import Gio, GLib
from opensnitch.core.events import Event
from opensnitch.core.services import discover_scope


class SystemdReader:
    DESTINATION = 'org.freedesktop.systemd1'
    MANAGER = '/org/freedesktop/systemd1'
    SIGNATURES = {'ListUnits': '()', 'ListUnitFiles': '()', 'LoadUnit': '(s)',
                  'GetAll': '(s)', 'GetUnitByPID': '(u)'}

    def __init__(self, bus, cancellable=None):
        self.bus, self.cancellable = bus, cancellable

    def call(self, path, interface, method, *args):
        if method not in self.SIGNATURES:
            raise ValueError('Only service discovery methods are permitted')
        try:
            reply = self.bus.call_sync(self.DESTINATION, path, interface, method,
                GLib.Variant(self.SIGNATURES[method], args), None,
                Gio.DBusCallFlags.NO_AUTO_START, 1500, self.cancellable)
            values = reply.unpack()
            return values[0] if values else None
        except GLib.Error as error:
            raise OSError(error.message) from error

    def manager(self, method, *args):
        return self.call(self.MANAGER, self.DESTINATION + '.Manager', method, *args)

    def unit_for_pid(self, pid):
        return self.manager('GetUnitByPID', pid)

    def properties(self, path, interface):
        return self.call(path, 'org.freedesktop.DBus.Properties', 'GetAll', interface)


class SystemdCatalog:
    def __init__(self):
        self.records, self.errors = {}, {}
        self.generation = 0
        self.changed = Event()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='systemd-discovery')
        self._stop = StopEvent()
        self._cancellable = Gio.Cancellable()
        self._busy = self._active = False
        self._waiting = []
        self._timer = 0

    def set_active(self, active):
        self._active = active
        if active and not self._timer:
            self.refresh()
            self._timer = GLib.timeout_add_seconds(15, self._tick)
        elif not active and self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _tick(self):
        self.refresh()
        return not self._stop.is_set()

    def refresh(self, callback=None):
        if self._stop.is_set():
            return
        if self._busy:
            # A control requires a scan started AFTER its request.
            if callback:
                self._waiting.append(callback)
            return
        self._busy = True
        future = self._executor.submit(self._discover)
        future.add_done_callback(lambda result: GLib.idle_add(self._accept, result, callback))

    def _discover(self):
        records, errors = {}, {}
        for scope, bus_type in (('system', Gio.BusType.SYSTEM), ('user', Gio.BusType.SESSION)):
            if self._stop.is_set():
                break
            try:
                bus = Gio.bus_get_sync(bus_type, self._cancellable)
                records.update(discover_scope(scope, SystemdReader(bus, self._cancellable), self._stop.is_set))
            except (OSError, GLib.Error) as error:
                errors[scope] = 'Service manager unavailable: ' + str(error)
        return records, errors

    def _accept(self, result, callback):
        if self._stop.is_set():
            return False
        self._busy = False
        try:
            self.records, self.errors = result.result()
        except Exception as error:
            self.records, self.errors = {}, {'system': str(error), 'user': str(error)}
        self.generation += 1
        self.changed.emit()
        if callback:
            callback()
        waiting, self._waiting = self._waiting, []
        if waiting:
            self.refresh(lambda: [item() for item in waiting])
        return False

    def close(self):
        self._stop.set()
        self._cancellable.cancel()
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._waiting.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
