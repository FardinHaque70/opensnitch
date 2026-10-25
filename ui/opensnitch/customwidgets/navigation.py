"""Visual navigation order independent of legacy QTabWidget identifiers."""

from PyQt5 import QtCore, QtGui, QtWidgets
from opensnitch.utils import Icons


class OrderedNavigation(QtWidgets.QWidget):
    page_changed = QtCore.pyqtSignal(str)

    def __init__(self, tabs, order, parent=None, extra_pages=None, advanced_pages=None):
        super().__init__(parent)
        self.tabs = tabs
        self.extra_pages = extra_pages or {}
        self.advanced_pages = advanced_pages or {}
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.bar = QtWidgets.QTabBar()
        self.bar.setAccessibleName(self.tr('Main navigation'))
        self.bar.setExpanding(False)
        self.bar.setUsesScrollButtons(True)
        for logical in order:
            if logical in self.extra_pages:
                label, icon, _ = self.extra_pages[logical]
                position = self.bar.addTab(icon, label)
            else:
                position = self.bar.addTab(tabs.tabIcon(logical), tabs.tabText(logical))
            self.bar.setTabData(position, logical)
            if logical not in self.extra_pages:
                self.bar.setTabToolTip(position, tabs.tabToolTip(logical))
        tabs.tabBar().hide()
        layout.addWidget(self.bar)
        self.context = QtWidgets.QWidget()
        context_layout = QtWidgets.QHBoxLayout(self.context)
        context_layout.setContentsMargins(4, 4, 4, 4)
        self.advanced_back = QtWidgets.QPushButton(Icons.new(self, 'go-previous'), self.tr('Advanced'))
        self.advanced_back.setAccessibleName(self.tr('Back to advanced tools'))
        self.advanced_back.clicked.connect(self.show_advanced)
        self.context_label = QtWidgets.QLabel()
        context_layout.addWidget(self.advanced_back)
        context_layout.addWidget(self.context_label)
        context_layout.addStretch()
        self.context.hide()
        layout.addWidget(self.context)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(tabs)
        for _, _, widget in self.extra_pages.values():
            self.stack.addWidget(widget)
        layout.addWidget(self.stack, 1)
        self.bar.currentChanged.connect(self.select_page)
        tabs.currentChanged.connect(self.select_tab)
        self.forward = QtWidgets.QShortcut(QtGui.QKeySequence('Ctrl+Tab'), self)
        self.backward = QtWidgets.QShortcut(QtGui.QKeySequence('Ctrl+Shift+Tab'), self)
        self.forward.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.backward.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        self.forward.activated.connect(self.next_page)
        self.backward.activated.connect(self.previous_page)
        self.select_tab(tabs.currentIndex())

    @QtCore.pyqtSlot(int)
    def select_page(self, position):
        logical = self.bar.tabData(position)
        if logical in self.extra_pages:
            self.context.hide()
            self.stack.setCurrentWidget(self.extra_pages[logical][2])
            self.page_changed.emit(logical)
        elif logical == self.tabs.currentIndex():
            self.select_tab(logical)
        else:
            self.tabs.setCurrentIndex(logical)

    @QtCore.pyqtSlot(int)
    def select_tab(self, logical):
        primary = 'advanced' if logical in self.advanced_pages else logical
        for position in range(self.bar.count()):
            if self.bar.tabData(position) == primary:
                with QtCore.QSignalBlocker(self.bar):
                    self.bar.setCurrentIndex(position)
                self.stack.setCurrentWidget(self.tabs)
                self.context.setVisible(logical in self.advanced_pages)
                if logical in self.advanced_pages:
                    self.context_label.setText('/ ' + self.advanced_pages[logical][0])
                self.page_changed.emit('legacy')
                break

    def extra_active(self, name):
        return name in self.extra_pages and self.stack.currentWidget() is self.extra_pages[name][2]

    def any_extra_active(self):
        return self.stack.currentWidget() is not self.tabs

    @QtCore.pyqtSlot()
    def show_advanced(self):
        for position in range(self.bar.count()):
            if self.bar.tabData(position) == 'advanced':
                with QtCore.QSignalBlocker(self.bar):
                    self.bar.setCurrentIndex(position)
                self.select_page(position)
                break

    @QtCore.pyqtSlot(int)
    def open_advanced(self, logical):
        if logical in self.advanced_pages:
            if self.tabs.currentIndex() == logical:
                self.select_tab(logical)
            else:
                self.tabs.setCurrentIndex(logical)

    @QtCore.pyqtSlot()
    def next_page(self):
        self.bar.setCurrentIndex((self.bar.currentIndex() + 1) % self.bar.count())

    @QtCore.pyqtSlot()
    def previous_page(self):
        self.bar.setCurrentIndex((self.bar.currentIndex() - 1) % self.bar.count())
