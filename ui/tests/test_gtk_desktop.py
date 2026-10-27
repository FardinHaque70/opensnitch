"""Desktop ID and bundled PNG are sufficient for the development taskbar icon."""
from pathlib import Path
import tempfile
import unittest
from opensnitch.gtkui.desktop import APPLICATION_ID, register_application, exec_argument


class DesktopTests(unittest.TestCase):
    def test_matching_desktop_id_and_bundled_png_without_theme_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop, icon = register_application(directory)
            self.assertEqual(desktop.name, APPLICATION_ID + '.desktop')
            self.assertEqual(icon.name, APPLICATION_ID + '.png')
            self.assertTrue(icon.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertIn('Icon=' + str(icon), desktop.read_text())
            self.assertIn('StartupWMClass=' + APPLICATION_ID, desktop.read_text())
            self.assertFalse((Path(directory) / 'applications/opensnitch_ui.desktop').exists())
            timestamp = desktop.stat().st_mtime_ns
            register_application(directory)
            self.assertEqual(desktop.stat().st_mtime_ns, timestamp)

    def test_account_default_hides_duplicate_but_retains_desktop_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            desktop, icon = register_application(directory, launcher=Path(directory) / 'opensnitch-gtk')
            text = desktop.read_text()
            self.assertIn('NoDisplay=true', text)
            self.assertNotIn('Hidden=true', text)
            self.assertIn('StartupWMClass=' + APPLICATION_ID, text)
            self.assertTrue(icon.is_file())

    def test_exec_paths_are_desktop_quoted(self):
        self.assertEqual(exec_argument('/a path/100%/app'), '"/a path/100%%/app"')
        self.assertIn('\\$', exec_argument('/path/$name'))
        with self.assertRaises(ValueError):
            exec_argument('/invalid\npath')


if __name__ == '__main__':
    unittest.main()
