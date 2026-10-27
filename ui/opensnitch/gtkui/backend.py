"""Read-only inventory/metadata source. No daemon endpoint and no permission writes."""
from concurrent.futures import ThreadPoolExecutor
import configparser
from datetime import datetime
import os
import sqlite3
from pathlib import Path

from gi.repository import Gio, GLib
from opensnitch.core.backend import ApplicationSnapshot, BackendSnapshot
from opensnitch.core.catalog import CatalogCore
from opensnitch.core.history import HistoryReader
from opensnitch.core.identity import IdentityCore, build_index, icon_directories
from opensnitch.desktop_parser import desktop_paths


class _NoTimer:
    def stop(self):
        pass


class InventoryBackend:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path(
            os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opensnitch'
        self._callbacks = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='gtk-apps')
        self._busy = False
        self._pending = False
        self._reindex = False
        self._index = None
        self._closed = False
        self._debounce = 0
        self._monitors = []
        self._watched = set()
        self.snapshot = BackendSnapshot((), message='Loading application inventory…')
        self._watch(self.directory)
        for path in desktop_paths():
            self._watch(Path(path))
        self._poll = GLib.timeout_add_seconds(5, self._periodic_refresh)
        self.refresh(reindex=True)

    def _watch(self, path):
        while not path.is_dir() and path != path.parent:
            path = path.parent
        if str(path) in self._watched:
            return
        try:
            monitor = Gio.File.new_for_path(str(path)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            monitor.connect('changed', self._file_changed)
            self._monitors.append(monitor)
            self._watched.add(str(path))
        except GLib.Error:
            pass  # Periodic refresh also handles unavailable monitors.

    def _file_changed(self, monitor, file, other, event):
        self._reindex |= file.get_basename() not in ('applications.json', 'settings.conf') and file.get_parent().get_path() != str(self.directory)
        if self._debounce:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(350, self._refresh_watched)

    def _periodic_refresh(self):
        self.refresh()
        return not self._closed

    def _refresh_watched(self):
        self._debounce = 0
        self.refresh(reindex=self._reindex)
        return False

    def subscribe(self, callback):
        self._callbacks.append(callback)
        callback(self.snapshot)
        return lambda: self._callbacks.remove(callback)

    @property
    def metadata_index(self):
        return self._index

    def request_policy(self, key, action, duration='always'):
        return False

    def refresh(self, reindex=False):
        if self._closed:
            return
        self._reindex |= reindex
        if self._busy:
            self._pending = True
            return
        self._busy = True
        rebuild, self._reindex = self._reindex, False
        self._executor.submit(self._read, rebuild).add_done_callback(
            lambda future: GLib.idle_add(self._accept, future))

    def _read(self, rebuild):
        if rebuild or self._index is None:
            self._index = build_index(desktop_paths(), icon_directories())
        identities = IdentityCore()
        identities.accept_index(self._index)
        catalog = CatalogCore(self.directory / 'applications.json', _NoTimer())
        if catalog.load_error:
            raise ValueError('Application inventory could not be read: ' + catalog.load_error)
        settings = configparser.ConfigParser(interpolation=None)
        settings.read(self.directory / 'settings.conf')
        path = settings.get('database', 'file', fallback='file::memory:')
        memory = settings.get('database', 'type', fallback='0') == '0' or path == 'file::memory:'
        history_error = ''
        try:
            activity = HistoryReader(None if memory else path).activity()
        except (OSError, ValueError, sqlite3.Error) as error:
            activity = ()
            history_error = ' History unavailable: ' + str(error)
        recent = {}
        for node, executable, timestamp in activity:
            try:
                recent[node, executable] = datetime.fromisoformat(timestamp).timestamp()
            except (ValueError, TypeError):
                continue
        rows = []
        for entry in catalog.entries.values():
            if entry.hidden or set(entry.sources) == {'services'}:
                continue
            # This is the explicit local Unix node emitted by the Qt backend.
            # TCP/unknown nodes never receive local metadata.
            local = entry.node_id == 'unix:/local'
            identities.register_node(entry.node_id, local)
            rows.append(ApplicationSnapshot(
                identities.resolve(*entry.key), tuple(entry.sources),
                max(entry.last_seen, recent.get(entry.key, 0)),
                Path(entry.executable_path).exists() if local else None))
        rows.sort(key=lambda row: (row.identity.display_name.casefold(), row.key))
        message = 'Read-only inventory · policy and daemon status unavailable.'
        if memory:
            message += ' Live history belongs to the running Qt session.'
        return BackendSnapshot(tuple(rows), message=message + history_error)

    def _accept(self, future):
        self._busy = False
        if self._closed:
            return False
        try:
            self.snapshot = future.result()
            self._watch(self.directory)
            if self._index is not None:
                for filename in self._index[2]:
                    self._watch(Path(filename).parent)
        except Exception as error:
            self.snapshot = BackendSnapshot(self.snapshot.applications,
                message='Refresh failed; retaining previous entries. ' + str(error))
        for callback in tuple(self._callbacks):
            callback(self.snapshot)
        if self._pending:
            self._pending = False
            self.refresh()
        return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._debounce:
            GLib.source_remove(self._debounce)
        GLib.source_remove(self._poll)
        for monitor in self._monitors:
            monitor.cancel()
        self._callbacks.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
