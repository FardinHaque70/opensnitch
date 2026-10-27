"""Account launcher/autostart defaults preserve exact prior files and fallback."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch, Mock
from opensnitch.gtkui.defaults import install, restore, launcher_source


class DefaultFrontendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config, self.data, self.binary = (root / name for name in ('config', 'data', 'bin'))
        self.repository = Path(__file__).resolve().parents[2]
        self.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config), 'XDG_DATA_HOME': str(self.data)})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.startup = self.config / 'autostart/opensnitch_ui.desktop'
        self.startup.parent.mkdir(parents=True)
        self.original = b'[Desktop Entry]\nType=Application\nName=Original\nName[bn]=Original translation\nExec=opensnitch-ui\nX-Custom=keep\n'
        self.startup.write_bytes(self.original)

    def install(self):
        return install(self.repository, self.config, self.data, self.binary)

    def test_install_sets_explicit_persistent_launch_paths_and_preserves_fields(self):
        state = self.install()
        command = str(self.binary / 'opensnitch-gtk')
        self.assertIn('Exec="' + command + '" --start-hidden\n', self.startup.read_text())
        menu = self.data / 'applications/opensnitch_ui.desktop'
        self.assertNotIn('--start-hidden', menu.read_text())
        self.assertIn('Name[bn]=Original translation', self.startup.read_text())
        self.assertIn('X-Custom=keep', self.startup.read_text())
        self.assertTrue((self.binary / 'opensnitch-gtk').stat().st_mode & 0o111)
        self.assertIn("'--default'", (self.binary / 'opensnitch-gtk').read_text())
        app = self.data / 'applications/io.github.opensnitch.GtkInventory.desktop'
        self.assertIn('Exec="' + command + '"', app.read_text())
        self.assertTrue(state['enabled'])

    def test_repeated_install_keeps_original_backup_and_restore_is_exact(self):
        self.install(); self.install()
        restore(self.config, self.data, self.binary)
        self.assertEqual(self.startup.read_bytes(), self.original)
        self.assertFalse((self.binary / 'opensnitch-gtk').exists())
        self.assertFalse((self.data / 'applications/opensnitch_ui.desktop').exists())
        state = json.loads((self.config / 'opensnitch/gtk-default.json').read_text())
        self.assertFalse(state['enabled'])

    def test_existing_application_override_and_launcher_are_backed_up(self):
        application = self.data / 'applications/opensnitch_ui.desktop'
        application.parent.mkdir(parents=True)
        application.write_bytes(self.original)
        self.binary.mkdir(); launcher = self.binary / 'opensnitch-gtk'; launcher.write_text('previous launcher\n')
        self.install(); restore(self.config, self.data, self.binary)
        self.assertEqual(application.read_bytes(), self.original)
        self.assertEqual(launcher.read_text(), 'previous launcher\n')

    def test_missing_checkout_falls_back_without_importing_frontend(self):
        connection = Mock(); connection.connect.side_effect = OSError('No listener')
        with patch('socket.socket', return_value=connection), patch('sys.argv', ['launcher']), patch('os.execv', side_effect=SystemExit('fallback')) as execute:
            with self.assertRaisesRegex(SystemExit, 'fallback'):
                exec(compile(launcher_source(self.config / 'missing-repository'), '<launcher>', 'exec'), {})
        self.assertEqual(execute.call_args.args[0], '/usr/bin/opensnitch-ui')

    def test_bootstrap_remains_self_contained_and_has_installed_fallback(self):
        source = launcher_source('/missing checkout')
        compile(source, '<launcher>', 'exec')
        self.assertIn("os.execv('/usr/bin/opensnitch-ui'", source)
        self.assertIn('probe.connect', source)
        self.assertNotIn('from opensnitch', source)


if __name__ == '__main__': unittest.main()
