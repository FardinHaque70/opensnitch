"""Visual page ordering must never renumber legacy identifiers."""

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from PyQt5 import QtCore, QtGui, QtWidgets, QtTest
from opensnitch.customwidgets.navigation import OrderedNavigation


class TestNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.tabs = QtWidgets.QTabWidget()
        self.pages = []
        labels = ['Activity', 'Nodes', 'Rules', 'Hosts', 'Apps', 'Addresses', 'Ports', 'Users', 'Connections']
        for label in labels:
            page = QtWidgets.QWidget()
            self.pages.append(page)
            self.tabs.addTab(page, label)
        self.services = QtWidgets.QWidget()
        self.advanced = QtWidgets.QWidget()
        advanced = {i: (labels[i], QtGui.QIcon()) for i in (1, 2, 3, 5, 6, 7)}
        self.navigation = OrderedNavigation(self.tabs, [4, 'services', 8, 0, 'advanced'],
            extra_pages={'services': ('Services', QtGui.QIcon(), self.services),
                         'advanced': ('Advanced', QtGui.QIcon(), self.advanced)}, advanced_pages=advanced)
        self.tabs.setCurrentIndex(4)

    def tearDown(self):
        self.navigation.close()
        self.navigation.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)

    def test_primary_order_and_legacy_pages_are_independent(self):
        self.assertEqual([self.navigation.bar.tabText(i) for i in range(5)],
                         ['Apps', 'Services', 'Connections', 'Activity', 'Advanced'])
        self.assertEqual(self.tabs.count(), 9)
        for index, page in enumerate(self.pages):
            self.assertIs(self.tabs.widget(index), page)
        self.navigation.bar.setCurrentIndex(2)
        self.assertEqual(self.tabs.currentIndex(), 8)

    def test_existing_navigation_to_diagnostic_tables_selects_advanced_context(self):
        for logical in (1, 2, 3, 5, 6, 7):
            self.tabs.setCurrentIndex(logical)
            self.assertEqual(self.navigation.bar.currentIndex(), 4)
            self.assertFalse(self.navigation.extra_active('advanced'))
            self.assertIn(self.tabs.tabText(logical), self.navigation.context_label.text())
            self.navigation.advanced_back.click()
            self.assertTrue(self.navigation.extra_active('advanced'))
            self.navigation.open_advanced(logical)
            self.assertIs(self.navigation.stack.currentWidget(), self.tabs)
            self.assertEqual(self.tabs.currentIndex(), logical)

    def test_saved_legacy_indices_restore_without_visual_index_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QtCore.QSettings(str(Path(directory) / 'settings.ini'), QtCore.QSettings.IniFormat)
            for logical, visual in ((8, 2), (0, 3), (2, 4), (4, 0)):
                settings.setValue('lastTab', logical)
                self.tabs.setCurrentIndex(settings.value('lastTab', type=int))
                self.assertEqual(self.navigation.bar.currentIndex(), visual)

    def test_keyboard_navigation_follows_primary_order(self):
        self.navigation.resize(900, 400)
        self.navigation.show()
        self.navigation.activateWindow()
        self.navigation.bar.setFocus()
        self.app.processEvents()
        for visual in (1, 2, 3, 4, 0):
            QtTest.QTest.keyClick(self.navigation.bar, QtCore.Qt.Key_Tab, QtCore.Qt.ControlModifier)
            self.app.processEvents()
            self.assertEqual(self.navigation.bar.currentIndex(), visual)
        self.assertEqual(self.tabs.currentIndex(), 4)

    def test_new_application_first_layout_preserves_saved_order_widths_and_logical_ids(self):
        from opensnitch.dialogs.stats import StatsDialog
        table = QtWidgets.QTableView()
        model = QtGui.QStandardItemModel(1, 14, table)
        model.setItem(0, 11, QtGui.QStandardItem('/usr/bin/example'))
        table.setModel(model)
        context = SimpleNamespace(_cfg=SimpleNamespace(getSettings=lambda key: None))
        StatsDialog._default_application_column(context, table, 'header', 11)
        header = table.horizontalHeader()
        self.assertEqual(header.visualIndex(11), 0)
        self.assertEqual(model.index(0, 11).data(), '/usr/bin/example')
        header.moveSection(0, 3)
        header.resizeSection(11, 360)
        saved = header.saveState()
        context._cfg.getSettings = lambda key: saved
        StatsDialog._default_application_column(context, table, 'header', 11)
        self.assertEqual(header.saveState(), saved)
        self.assertEqual(header.visualIndex(11), 3)
        self.assertEqual(header.sectionSize(11), 360)
        table.deleteLater()


if __name__ == '__main__':
    unittest.main()
