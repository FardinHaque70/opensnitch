"""Entry points for the existing advanced tools; no new backend workflows."""

from PyQt5 import QtCore, QtWidgets
from opensnitch.utils import Icons


class AdvancedToolsView(QtWidgets.QWidget):
    tool_requested = QtCore.pyqtSignal(int)
    firewall_requested = QtCore.pyqtSignal()
    new_rule_requested = QtCore.pyqtSignal()

    def __init__(self, pages, management, diagnostics, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        title = QtWidgets.QLabel(self.tr('Advanced tools'))
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        description = QtWidgets.QLabel(self.tr('Manage rules and connected computers, or inspect detailed traffic breakdowns.'))
        description.setWordWrap(True)
        layout.addWidget(description)
        self.buttons = {}
        manage = QtWidgets.QGroupBox(self.tr('Manage'))
        manage.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        grid = QtWidgets.QGridLayout(manage)
        for position, logical in enumerate(management):
            button = self._button(logical, pages)
            grid.addWidget(button, position // 2, position % 2)
        self.firewall = QtWidgets.QPushButton(Icons.new(self, 'security-high'), self.tr('System Firewall'))
        self.firewall.clicked.connect(self.open_firewall)
        self.firewall.setToolTip(self.tr('Open the existing system firewall editor.'))
        self.new_rule = QtWidgets.QPushButton(Icons.new(self, 'document-new'), self.tr('New rule'))
        self.new_rule.clicked.connect(self.open_new_rule)
        self.new_rule.setToolTip(self.tr('Create an exact or conditional application rule.'))
        row = (len(management) + 1) // 2
        grid.addWidget(self.firewall, row, 0)
        grid.addWidget(self.new_rule, row, 1)
        layout.addWidget(manage)
        group = QtWidgets.QGroupBox(self.tr('Diagnostics'))
        group.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        grid = QtWidgets.QGridLayout(group)
        for position, logical in enumerate(diagnostics):
            grid.addWidget(self._button(logical, pages), position // 2, position % 2)
        layout.addWidget(group)
        layout.addStretch(1)

    def _button(self, logical, pages):
        label, icon = pages[logical]
        button = QtWidgets.QPushButton(icon, label)
        button.setProperty('logicalPage', logical)
        button.clicked.connect(self.open_tool)
        self.buttons[logical] = button
        return button

    @QtCore.pyqtSlot()
    def open_tool(self):
        self.tool_requested.emit(self.sender().property('logicalPage'))

    @QtCore.pyqtSlot()
    def open_firewall(self):
        self.firewall_requested.emit()

    @QtCore.pyqtSlot()
    def open_new_rule(self):
        self.new_rule_requested.emit()
