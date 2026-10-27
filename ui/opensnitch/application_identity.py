"""Shared, node-aware application metadata. No scanning occurs in resolve()."""

from pathlib import Path
import logging
import os

from PyQt5 import QtCore, QtGui, QtWidgets

from opensnitch.desktop_parser import desktop_paths
from opensnitch.core.identity import ApplicationIdentity, IdentityCore, build_index, icon_directories


class _IndexWorker(QtCore.QThread):
    ready = QtCore.pyqtSignal(object)

    def __init__(self, paths, icon_paths, parent):
        super().__init__(parent)
        self.paths = paths
        self.icon_paths = icon_paths

    def run(self):
        try:
            self.ready.emit(build_index(self.paths, self.icon_paths))
        except Exception:
            logging.exception('Could not refresh application metadata; retaining previous index')


class ApplicationIdentityService(IdentityCore, QtCore.QObject):
    changed = QtCore.pyqtSignal()
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, paths=None, icon_paths=None, auto_refresh=True, parent=None):
        QtCore.QObject.__init__(self, parent or QtWidgets.QApplication.instance())
        self.paths = tuple(paths) if paths is not None else desktop_paths()
        self.icon_paths = tuple(icon_paths) if icon_paths is not None else icon_directories()
        IdentityCore.__init__(self, self.changed,
            QtCore.QCoreApplication.translate('applications', 'Unknown application'),
            QtCore.QCoreApplication.translate('applications', 'Application artwork: {0}'))
        self._icon_paths = {}
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
        self.accept_index(snapshot)
        files, self._icon_paths = snapshot[2:4]
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
