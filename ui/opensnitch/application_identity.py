"""Shared, node-aware application metadata. No scanning occurs in resolve()."""

from dataclasses import dataclass
from pathlib import Path
import logging
import os

from PyQt5 import QtCore, QtGui, QtWidgets

from opensnitch.desktop_parser import LinuxDesktopParser, desktop_paths


@dataclass(frozen=True)
class ApplicationIdentity:
    node_id: str
    executable_path: str
    display_name: str
    icon_reference: str
    exact: bool
    icon_owner: str = ''

    @property
    def tooltip(self):
        artwork = QtCore.QCoreApplication.translate('applications', 'Application artwork: {0}').format(self.icon_owner) if self.icon_owner else ''
        return '\n'.join(filter(None, (self.display_name, self.executable_path, self.node_id, artwork)))


def icon_directories():
    roots = [os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')]
    roots.extend((os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':'))
    directories = [os.path.expanduser('~/.icons')]
    for root in roots:
        if os.path.isabs(root):
            directories.extend((os.path.join(root, 'icons'), os.path.join(root, 'pixmaps')))
    return tuple(dict.fromkeys(directories))


class _IndexWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, paths, icon_paths, parent):
        super().__init__(parent)
        self.paths = paths
        self.icon_paths = icon_paths

    def run(self):
        try:
            parser = LinuxDesktopParser(paths=self.paths, watch=False)
            icons = {}
            for root in self.icon_paths:
                for directory, dirs, files in os.walk(root):
                    dirs.sort()
                    for filename in sorted(files):
                        path = Path(directory) / filename
                        if path.suffix.lower() in ('.png', '.xpm'):
                            icons.setdefault(path.stem, []).append(str(path))
            roots = {}
            ambiguous_roots = set()
            for executable, entry in parser.apps.items():
                directory = str(Path(executable).parent)
                if (executable in parser.ambiguous or not entry[1]
                        or not any(directory.startswith(prefix) for prefix in ('/usr/lib/', '/usr/lib64/', '/opt/'))):
                    continue
                # Never borrow artwork for a shared library/bin directory.
                # Require an app-specific directory named by its metadata.
                identifiers = {Path(executable).name.casefold(), Path(entry[3]).stem.casefold(),
                               Path(entry[1]).stem.casefold()}
                if Path(directory).name.casefold() not in identifiers:
                    continue
                if directory in roots and roots[directory][:2] != entry[:2]:
                    ambiguous_roots.add(directory)
                roots[directory] = entry
            for root in ambiguous_roots:
                roots.pop(root, None)
            self.ready.emit((parser.apps, parser.ambiguous, parser.files, icons, roots))
        except Exception:
            logging.exception('Could not refresh application metadata; retaining previous index')


class ApplicationIdentityService(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, paths=None, icon_paths=None, auto_refresh=True, parent=None):
        super().__init__(parent or QtWidgets.QApplication.instance())
        self.paths = tuple(paths) if paths is not None else desktop_paths()
        self.icon_paths = tuple(icon_paths) if icon_paths is not None else icon_directories()
        self._entries = {}
        self._ambiguous = set()
        self._icon_paths = {}
        self._bundle_roots = {}
        self._local_nodes = set()
        self._cache = {}
        self._icons = {}
        self._worker = None
        self._pending = False
        self._stopped = False
        self._watcher = QtCore.QFileSystemWatcher(self)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._watcher.directoryChanged.connect(self._schedule_refresh)
        self._watcher.fileChanged.connect(self._schedule_refresh)
        self._timer.timeout.connect(self.refresh)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)
        if auto_refresh:
            self.refresh()

    def register_node(self, node_id, local):
        local = bool(local)
        was_local = node_id in self._local_nodes
        if local:
            self._local_nodes.add(node_id)
        else:
            self._local_nodes.discard(node_id)
        if was_local != local:
            self._cache.clear()
            self.changed.emit()

    def is_local_node(self, node_id):
        return node_id in self._local_nodes

    def installed_executables(self):
        return tuple(path for path in self._entries if path not in self._ambiguous)

    def resolve(self, node_id, executable_path):
        key = (node_id or '', executable_path or '')
        if key in self._cache:
            return self._cache[key]
        node_id, path = key
        name = os.path.basename(path.rstrip('/')) or QtCore.QCoreApplication.translate('applications', 'Unknown application')
        icon = ''
        exact = False
        icon_owner = ''
        lookup = os.path.normpath(path) if os.path.isabs(path) else ''
        if node_id in self._local_nodes and lookup and lookup not in self._ambiguous:
            entry = self._entries.get(lookup)
            if entry is not None:
                name, icon = entry[0], entry[1] or ''
                exact = True
            else:
                # Companion executables keep their own name and fallback
                # identity. Only the containing application's artwork is used.
                for directory in sorted(self._bundle_roots, key=len, reverse=True):
                    if lookup.startswith(directory + '/'):
                        owner = self._bundle_roots[directory]
                        icon, icon_owner = owner[1], owner[0]
                        break
        identity = ApplicationIdentity(node_id, path, name, icon, exact, icon_owner)
        self._cache[key] = identity
        return identity

    def icon_for(self, identity, widget):
        # QIcon/QPixmap and widget palettes are accessed only on the GUI thread.
        from opensnitch.utils import Icons, Themes
        key = (identity.icon_reference, widget.palette().cacheKey(), QtGui.QIcon.themeName(), Themes.IS_DARK)
        if key in self._icons:
            return self._icons[key]
        reference = identity.icon_reference
        candidates = []
        if reference:
            if os.path.isabs(reference):
                candidates.append(QtGui.QIcon(reference))
            else:
                candidates.extend((QtGui.QIcon.fromTheme(reference), QtGui.QIcon.fromTheme(reference + '-symbolic')))
            stem = Path(reference).stem if os.path.isabs(reference) or Path(reference).suffix in ('.png', '.svg', '.xpm') else reference
            candidates.extend(QtGui.QIcon(path) for path in self._icon_paths.get(stem, ()))
        icon = next((candidate for candidate in candidates if Icons._is_renderable(candidate)), None)
        if icon is None:
            icon = Icons.new(widget, 'system-run')
        self._icons[key] = icon
        return icon

    def refresh(self):
        if self._stopped:
            return
        if self._worker is not None:
            self._pending = True
            return
        worker = _IndexWorker(self.paths, self.icon_paths, self)
        self._worker = worker
        worker.ready.connect(self._accept_index)
        worker.finished.connect(self._finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @QtCore.pyqtSlot(str)
    def _schedule_refresh(self, path):
        self._timer.start()

    @QtCore.pyqtSlot(object)
    def _accept_index(self, snapshot):
        if self._stopped:
            return
        self._entries, self._ambiguous, files, self._icon_paths = snapshot[:4]
        self._bundle_roots = snapshot[4] if len(snapshot) > 4 else {}
        self._cache.clear()
        self._icons.clear()
        watched = self._watcher.files() + self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)
        paths = set(files)
        for directory in self.paths:
            current = Path(directory)
            # Watch the closest existing parent too, for newly created user dirs.
            while not current.is_dir() and current != current.parent:
                current = current.parent
            paths.add(str(current))
        paths.update(str(Path(filename).parent) for filename in files)
        if paths:
            self._watcher.addPaths(sorted(paths))
        self.changed.emit()

    @QtCore.pyqtSlot()
    def _finished(self):
        self._worker = None
        if self._pending:
            self._pending = False
            self.refresh()

    def stop(self):
        self._stopped = True
        self._pending = False
        self._timer.stop()
        self._watcher.blockSignals(True)
        paths = self._watcher.files() + self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)
        if self._worker is not None:
            self._worker.wait()
