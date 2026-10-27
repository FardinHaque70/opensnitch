"""Shared async native save dialog for text exports."""
from gi.repository import Gio, GLib, Gtk


def choose_file(window, name, callback):
    dialog = Gtk.FileDialog(title='Export', initial_name=name)
    def selected(dialog, result):
        try:
            callback(dialog.save_finish(result))
        except GLib.Error as error:
            if not error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                window.show_message('Export failed: ' + error.message)
    dialog.save(window, None, selected)


def save_text(window, name, text):
    content = text.encode('utf-8')
    def selected(file):
        file.replace_contents_async(content, None, False, Gio.FileCreateFlags.NONE, None, saved)
    def saved(file, result):
        try:
            file.replace_contents_finish(result)
            window.show_message('Export completed.')
        except GLib.Error as error:
            window.show_message('Export failed: ' + error.message)
    choose_file(window, name, selected)
