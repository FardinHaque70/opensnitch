"""Temporary Qt scheduling and delivery adapter for shared policy controls."""
from PyQt5 import QtCore
from opensnitch.core.controls import AccessControllerCore


class ApplicationAccessController(AccessControllerCore, QtCore.QObject):
    changed = QtCore.pyqtSignal()
    message = QtCore.pyqtSignal(str)
    _reply = QtCore.pyqtSignal(str, bool, str)

    def __init__(self, catalog, policies, transport, online, apply_confirmed,
                 verify_current=None, parent=None, timeout=15):
        QtCore.QObject.__init__(self, parent)
        AccessControllerCore.__init__(
            self, catalog, policies, transport, online, apply_confirmed, verify_current,
            timeout, lambda callback, *args: self._reply.emit(*args), self.changed, self.message)
        self._reply.connect(self._receive_reply, QtCore.Qt.QueuedConnection)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.check_timeouts)
        self._timer.start()
