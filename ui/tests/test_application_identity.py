"""Application metadata, provenance, and raw table identifiers."""

from pathlib import Path
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PyQt5 import QtCore, QtGui, QtWidgets

from opensnitch.application_identity import ApplicationIdentityService
from opensnitch.desktop_parser import LinuxDesktopParser, desktop_paths
from opensnitch.customwidgets.applicationidentity import (
    APPLICATION_IDENTITY_ROLE, ApplicationIdentityDelegate, IdentityTableModel,
)


class TestApplicationIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.user = self.root / 'user' / 'applications'
        self.system = self.root / 'system' / 'applications'
        self.user.mkdir(parents=True)
        self.system.mkdir(parents=True)
        self.executable = str(self.root / 'bin' / 'example app')
        Path(self.executable).parent.mkdir()
        Path(self.executable).write_text('')
        self.resolver = ApplicationIdentityService(
            paths=(str(self.user), str(self.system)), icon_paths=(), auto_refresh=False)
        self.resolver.register_node('unix:/local', True)

    def tearDown(self):
        self.resolver.stop()
        self.resolver.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.app.processEvents()
        self.temp.cleanup()

    def entry(self, directory=None, filename='example.desktop', name='Example', extra='', executable=None):
        directory = directory or self.system
        path = directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[Desktop Entry]\nType=Application\nName=' + name +
                        '\nExec="' + (executable or self.executable) + '" %U\nIcon=example\n' + extra)
        return path

    def load(self, locale_name='en_US', icon_paths=None):
        parser = LinuxDesktopParser(paths=self.resolver.paths, locale_name=locale_name, watch=False)
        self.resolver._accept_index((parser.apps, parser.ambiguous, parser.files, icon_paths or {}))
        return parser

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(predicate(), 'Timed out waiting for metadata refresh')

    def test_exact_match_and_cached_resolution(self):
        self.entry()
        self.load()
        with patch('os.path.realpath', side_effect=AssertionError('Filesystem lookup in resolve')), \
                patch.object(LinuxDesktopParser, '__init__', side_effect=AssertionError('Scan in resolve')):
            identity = self.resolver.resolve('unix:/local', self.executable)
            self.assertIs(identity, self.resolver.resolve('unix:/local', self.executable))
        self.assertEqual(identity.display_name, 'Example')
        self.assertEqual(identity.executable_path, self.executable)
        self.assertTrue(identity.exact)

    def test_remote_unknown_and_same_basename_do_not_use_local_metadata(self):
        self.entry()
        self.load()
        for node, path in [('ipv4:remote', self.executable), (None, self.executable),
                           ('unix:/local', '/different/example app')]:
            with self.subTest(node=node, path=path):
                identity = self.resolver.resolve(node, path)
                self.assertEqual(identity.display_name, 'example app')
                self.assertFalse(identity.exact)
                self.assertFalse(identity.icon_reference)
        self.assertTrue(self.resolver.resolve(None, '').display_name)

    def test_user_override_and_full_locale_precedence(self):
        self.entry(name='System')
        self.entry(self.user, name='User', extra='Name[fr]=French\nName[fr_FR]=France\n')
        self.load('fr_FR')
        self.assertEqual(self.resolver.resolve('unix:/local', self.executable).display_name, 'France')

    def test_hidden_override_suppresses_system_entry(self):
        self.entry()
        self.entry(self.user, extra='Hidden=true\n')
        self.load()
        self.assertFalse(self.resolver.resolve('unix:/local', self.executable).exact)

    def test_symlink_and_quoted_env_command(self):
        link = self.root / 'launcher'
        link.symlink_to(self.executable)
        path = self.entry(executable=str(link))
        path.write_text('[Desktop Entry]\nName=Example\nExec=env MODE=test "' + str(link) + '" %U\n')
        self.load()
        self.assertTrue(self.resolver.resolve('unix:/local', self.executable).exact)
        self.assertTrue(self.resolver.resolve('unix:/local', str(link)).exact)

    def test_ambiguous_executable_uses_fallback(self):
        self.entry(filename='one.desktop', name='One')
        self.entry(filename='two.desktop', name='Two')
        self.load()
        self.assertFalse(self.resolver.resolve('unix:/local', self.executable).exact)

    def test_nested_desktop_id_override_and_nodisplay_metadata(self):
        self.entry(filename='vendor/app.desktop', name='System')
        self.entry(self.user, filename='vendor-app.desktop', name='User', extra='NoDisplay=true\n')
        self.load()
        self.assertEqual(self.resolver.resolve('unix:/local', self.executable).display_name, 'User')

    def test_shared_launcher_and_malformed_entries_are_not_app_identities(self):
        self.entry(executable='/usr/bin/python3')
        (self.user / 'broken.desktop').write_text('Not a desktop entry')
        self.load()
        self.assertFalse(self.resolver.resolve('unix:/local', '/usr/bin/python3').exact)

    def test_xdg_paths_include_user_and_standard_system_roots(self):
        with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'data'),
                                     'XDG_DATA_DIRS': '/usr/local/share:/usr/share'}):
            self.assertEqual(desktop_paths(), (str(self.root / 'data/applications'),
                                               '/usr/local/share/applications', '/usr/share/applications'))

    def test_icon_uses_png_when_svg_or_theme_is_unavailable(self):
        self.entry()
        png = self.root / 'example.png'
        image = QtGui.QImage(32, 32, QtGui.QImage.Format_ARGB32)
        image.fill(QtGui.QColor('red'))
        image.save(str(png))
        self.load(icon_paths={'example': [str(png)]})
        widget = QtWidgets.QWidget()
        with patch.object(QtGui.QIcon, 'fromTheme', return_value=QtGui.QIcon()):
            icon = self.resolver.icon_for(self.resolver.resolve('unix:/local', self.executable), widget)
            self.assertEqual(icon.pixmap(24, 24).toImage().pixelColor(12, 12), QtGui.QColor('red'))
            fallback = self.resolver.icon_for(self.resolver.resolve('remote', self.executable), widget)
            self.assertFalse(fallback.pixmap(24, 24).isNull())
        widget.close()

    def test_worker_refresh_and_filesystem_changes_invalidate_cache(self):
        path = self.entry(name='Before')
        self.resolver.refresh()
        self.wait_for(lambda: self.resolver.resolve('unix:/local', self.executable).display_name == 'Before')
        self.wait_for(lambda: self.resolver._worker is None)
        path.write_text(path.read_text().replace('Before', 'After'))
        self.wait_for(lambda: self.resolver.resolve('unix:/local', self.executable).display_name == 'After')
        path.unlink()
        self.wait_for(lambda: not self.resolver.resolve('unix:/local', self.executable).exact)

    def test_model_presentation_preserves_raw_values_and_delegate_renders(self):
        self.entry()
        self.load()
        view = QtWidgets.QTableView()
        def identity_for_cell(index, row):
            return self.resolver.resolve('unix:/local', row[0]) if index.column() == 0 else None
        model = IdentityTableModel('procs', ['Application', 'Hits'], self.resolver, identity_for_cell, view)
        model.items = [[self.executable, '5']]
        model.setRowCount(1)
        view.setModel(model)
        view.setItemDelegate(ApplicationIdentityDelegate(view))
        index = model.index(0, 0)
        self.assertEqual(index.data(), self.executable)
        self.assertEqual(index.data(QtCore.Qt.EditRole), self.executable)
        self.assertEqual(index.data(APPLICATION_IDENTITY_ROLE).display_name, 'Example')
        self.assertIn(self.executable, index.data(QtCore.Qt.ToolTipRole))
        self.assertFalse(index.data(QtCore.Qt.DecorationRole).pixmap(24, 24).isNull())
        model.setColumnCount(14)
        model.setColumnCount(2)
        self.assertEqual(model.headerData(0, QtCore.Qt.Horizontal), 'Application')
        for background in ('#ffffff', '#232629'):
            palette = view.palette()
            palette.setColor(QtGui.QPalette.Base, QtGui.QColor(background))
            palette.setColor(QtGui.QPalette.Text, QtGui.QColor('black' if background == '#ffffff' else 'white'))
            view.setPalette(palette)
            view.resize(480, 120)
            view.show()
            view.selectRow(0)
            self.app.processEvents()
            self.assertFalse(view.grab().isNull())
            self.assertEqual(index.data(), self.executable)
        # Custom highlight actions must still receive raw identifiers, while
        # the shared highlight renderer draws the friendly label and icon.
        from opensnitch.customwidgets.colorizeddelegate import ColorizedDelegate
        from opensnitch.plugins.highlight.highlight import Highlight
        captured = []
        class RawPathHighlighter:
            TYPE = ('views',)
            def run(inner, parent, args):
                captured.append(args[11])
                Highlight.paintCell(None, args[3], args[0], args[1], args[2],
                                     args[7], args[8], args[9], args[10],
                                     QtGui.QColor('red'), QtGui.QColor('#eeeeee'), args[11])
                return (True,)
        view.setItemDelegate(ColorizedDelegate(view, actions={'actions': {'highlight': RawPathHighlighter()}}))
        view.grab()
        self.assertIn(self.executable, captured)
        view.close()
        model.deleteLater()

    def test_applications_summary_requires_known_local_provenance(self):
        from opensnitch.dialogs.stats import StatsDialog
        self.entry()
        self.load()
        context = SimpleNamespace(
            _address=None, _application_identity=self.resolver,
            _application_source_nodes={'unix:/local'},
            _db=SimpleNamespace(DB_IN_MEMORY='memory', get_db_file=lambda: 'memory'))
        index = SimpleNamespace(column=lambda: 0)
        identity = StatsDialog._application_cell_identity(context, index, [self.executable, '5'])
        self.assertTrue(identity.exact)
        context._application_source_nodes.add('ipv4:remote')
        self.assertFalse(StatsDialog._application_cell_identity(context, index, [self.executable, '5']).exact)
        context._application_source_nodes = {'unix:/local'}
        context._db.get_db_file = lambda: 'saved-history.sqlite'
        self.assertFalse(StatsDialog._application_cell_identity(context, index, [self.executable, '5']).exact)
        self.assertIsNone(StatsDialog._application_cell_identity(context, index, [self.executable] * 14))


if __name__ == '__main__':
    unittest.main()
