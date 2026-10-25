"""Icon loading must work even when Qt's SVG image plugin is unavailable."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PyQt5 import QtGui, QtWidgets

from opensnitch.service import UIService
import opensnitch.service as service


class TestServiceIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.previous_icon = self.app.windowIcon()
        self.previous_desktop_file = self.app.desktopFileName()
        self.icons = SimpleNamespace(
            _app=self.app,
            _path=os.path.dirname(service.__file__),
            DESKTOP_FILENAME=UIService.DESKTOP_FILENAME,
        )

    def tearDown(self):
        self.app.setWindowIcon(self.previous_icon)
        self.app.setDesktopFileName(self.previous_desktop_file)

    def test_connected_icon_without_svg_support(self):
        pixmap = QtGui.QPixmap

        def load(path):
            if path.endswith("icon-white.svg"):
                return pixmap()
            return pixmap(path)

        with patch.object(service.QtGui, "QPixmap", side_effect=load):
            UIService._setup_icons(self.icons)

        self.assertFalse(self.icons.white_image.isNull())
        expected = pixmap(os.path.join(self.icons._path, "res/icon-white.png"))
        self.assertEqual(self.icons.white_image.toImage(), expected.toImage())
        self.assertFalse(self.icons.white_icon.pixmap(22, 22).isNull())
        self.assertFalse(self.app.windowIcon().pixmap(22, 22).isNull())

    def test_connected_icon_preserves_working_svg(self):
        pixmap = QtGui.QPixmap
        svg_image = pixmap(32, 32)
        svg_image.fill(QtGui.QColor("blue"))

        def load(path):
            if path.endswith("icon-white.svg"):
                return svg_image
            return pixmap(path)

        with patch.object(service.QtGui, "QPixmap", side_effect=load) as loader:
            UIService._setup_icons(self.icons)

        self.assertEqual(self.icons.white_image.toImage(), svg_image.toImage())
        loaded_paths = [call.args[0] for call in loader.call_args_list]
        self.assertFalse(any(path.endswith("icon-white.png") for path in loaded_paths))


if __name__ == "__main__":
    unittest.main()
