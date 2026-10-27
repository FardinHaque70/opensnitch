"""Temporary Qt adapter for the shared application catalog."""
import os
from pathlib import Path
from PyQt5 import QtCore
from opensnitch.core.catalog import CatalogCore, CatalogEntry


class ApplicationCatalog(CatalogCore, QtCore.QObject):
    changed = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            root = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
            cls._instance = cls(Path(root) / 'opensnitch' / 'applications.json')
        return cls._instance

    def __init__(self, path, parent=None):
        QtCore.QObject.__init__(self, parent or QtCore.QCoreApplication.instance())
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(500)
        CatalogCore.__init__(self, path, timer, self.changed, self.error)
        timer.timeout.connect(self.flush)
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.flush)
