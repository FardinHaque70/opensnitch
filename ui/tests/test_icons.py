"""Shared icons must remain usable without desktop icons or SVG support."""

import unittest
from unittest.mock import patch

from PyQt5 import QtGui, QtWidgets

from opensnitch.utils import Icons
from opensnitch.utils.themes import Themes


class TestIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.widget = QtWidgets.QWidget()

    def tearDown(self):
        self.widget.close()

    def set_background(self, color):
        palette = self.widget.palette()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor(color))
        self.widget.setPalette(palette)

    def assert_visible(self, icon):
        for size in (16, 20, 24, 30, 32, 40):
            with self.subTest(size=size):
                image = icon.pixmap(size, size).toImage()
                self.assertFalse(image.isNull())
                self.assertTrue(any(image.pixelColor(x, y).alpha()
                                    for y in range(image.height())
                                    for x in range(image.width())))

    def test_all_registered_icons_have_png_fallbacks(self):
        with patch.object(QtGui.QIcon, 'fromTheme', return_value=QtGui.QIcon()):
            for background in ('#ffffff', '#232629'):
                self.set_background(background)
                for name in Icons.defaults:
                    with self.subTest(background=background, name=name):
                        self.assert_visible(Icons._bundled(self.widget, name))
                        self.assert_visible(Icons.new(self.widget, name))

    def test_valid_desktop_icon_is_preserved(self):
        desktop = self.widget.style().standardIcon(QtWidgets.QStyle.SP_MessageBoxInformation)
        with patch.object(QtGui.QIcon, 'fromTheme', return_value=desktop):
            result = Icons.new(self.widget, 'preferences-system')
        self.assertEqual(result.cacheKey(), desktop.cacheKey())

    def test_symbolic_icon_is_tried_after_an_unrenderable_icon(self):
        class UnrenderableIcon:
            def pixmap(self, *args):
                return QtGui.QPixmap()

        desktop = self.widget.style().standardIcon(QtWidgets.QStyle.SP_MessageBoxInformation)
        with patch.object(QtGui.QIcon, 'fromTheme', side_effect=[UnrenderableIcon(), desktop]) as load:
            result = Icons.new(self.widget, 'preferences-system')
        self.assertEqual(result.cacheKey(), desktop.cacheKey())
        self.assertEqual(load.call_args_list[1].args, ('preferences-system-symbolic',))

    def test_transparent_desktop_icon_uses_visible_fallback(self):
        transparent = QtGui.QPixmap(24, 24)
        transparent.fill(QtGui.QColor(0, 0, 0, 0))
        with patch.object(QtGui.QIcon, 'fromTheme', return_value=QtGui.QIcon(transparent)):
            self.assert_visible(Icons.new(self.widget, 'system-run'))

    def test_png_contrast_follows_the_window_palette(self):
        with patch.object(Themes, 'IS_DARK', False):
            self.set_background('white')
            light = Icons._bundled(self.widget, 'preferences-system').pixmap(64, 64).toImage()
            self.set_background('#232629')
            dark = Icons._bundled(self.widget, 'preferences-system').pixmap(64, 64).toImage()
        self.assertNotEqual(light, dark)
        for y in range(64):
            for x in range(64):
                self.assertEqual(light.pixelColor(x, y).alpha(), dark.pixelColor(x, y).alpha())

    def test_material_dark_theme_does_not_require_svg(self):
        with patch.object(Themes, 'IS_DARK', True), \
                patch.object(QtGui.QIcon, 'fromTheme', return_value=QtGui.QIcon()):
            self.assert_visible(Icons.new(self.widget, 'preferences-system'))

    def test_missing_asset_uses_standard_icon(self):
        with patch.object(QtGui.QIcon, 'fromTheme', return_value=QtGui.QIcon()), \
                patch.object(Icons, '_bundled', return_value=QtGui.QIcon()):
            self.assert_visible(Icons.new(self.widget, 'preferences-system'))

    def test_unknown_name_and_empty_style_still_have_a_fallback(self):
        class EmptyStyle:
            def standardIcon(self, *args):
                return QtGui.QIcon()

        class MinimalWidget:
            def palette(inner):
                return self.widget.palette()

            def style(inner):
                return EmptyStyle()

        with patch.object(QtGui.QIcon, 'fromTheme', return_value=QtGui.QIcon()):
            self.assert_visible(Icons.new(MinimalWidget(), 'unknown-plugin-action'))


if __name__ == '__main__':
    unittest.main()
