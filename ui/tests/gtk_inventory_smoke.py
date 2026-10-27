"""Native GTK integration check with isolated inventory; no daemon connection."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import time

from opensnitch.gtkui.apps import GtkApplication, AppsWindow, GLib, Gtk
from opensnitch.gtkui.backend import InventoryBackend
from opensnitch.core.catalog import atomic_write

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-gtk-test-')
root = Path(fixture.name)
path = root / 'applications.json'
samples = json.loads((Path(__file__).parent / 'fixtures/gtk_apps.json').read_text())
rows = [dict(node_id='unix:/local', executable_path=samples[0]['path'], sources=['installed'],
             first_seen=1, last_seen=123, hidden=False, ownership={'name': 'cached-allow'})]
rows += [dict(rows[0], node_id='ipv4:192.0.2.4', ownership={})]
rows += [dict(rows[0], executable_path='/opt/example/worker-' + str(i), ownership={}) for i in range(35)]
rows += [dict(rows[0], executable_path='/opt/hidden', hidden=True),
         dict(rows[0], executable_path='/usr/bin/service-only', sources=['services'])]
atomic_write(path, json.dumps({'version': 1, 'apps': rows}).encode())
original = path.read_bytes()
failures = []
started = time.monotonic()


class TestApplication(GtkApplication):
    def do_activate(self):
        self.window = AppsWindow(self)
        self.backend = InventoryBackend(root)
        self.backend.subscribe(self.window.apply_snapshot)
        self.window.present()


app = TestApplication('light')


def capture(name):
    window = app.window
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    texture = window.get_renderer().render_texture(snapshot.to_node(), None)
    assert texture.save_to_png('/tmp/opensnitch-gtk-inventory-' + name + '.png')


def fail():
    import traceback
    traceback.print_exc()
    failures.append(True)
    app.quit()


def check_loaded():
    try:
        if not app.window.records:
            assert time.monotonic() - started < 15, app.backend.snapshot.message
            return True
        window = app.window
        assert len(window.records) == 37
        assert window.nodes.get_visible()
        window.theme_buttons['dark'].emit('clicked')
        assert app.appearance == 'dark'
        window.theme_buttons['light'].emit('clicked')
        assert app.appearance == 'light'
        assert not app.backend.snapshot.connected and not app.backend.snapshot.authoritative
        assert all(r.policy == 'Unavailable' for r in window.records)
        assert path.read_bytes() == original
        assert not app.backend.request_policy(('unix:/local', '/usr/lib/firefox/firefox'), 'deny')
        assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in sys.modules)
        remote = ('ipv4:192.0.2.4', '/usr/lib/firefox/firefox')
        assert not app.backend.snapshot.applications[0].pending
        remote_identity = next(r.identity for r in app.backend.snapshot.applications if r.key == remote)
        assert not remote_identity.exact and not remote_identity.icon_reference
        window.lists['Unavailable'].select_row(window.rows[remote])
        window.search.set_text('/usr/lib/firefox')
        window.filter_rows()
        assert sum(row.get_visible() for row in window.rows.values()) == 2
        assert window.selected_id == remote
        window.search.set_text('no-such-application')
        window.filter_rows()
        assert window.selected_id is None
        assert window.body_stack.get_visible_child_name() == 'empty'
        window.search.set_text('')
        window.filter_rows()
        window.lists['Unavailable'].select_row(window.rows[remote])
        window.scroll.get_vadjustment().set_value(200)
        window.groups['Unavailable'][0].set_expanded(False)
        modified = replace(app.backend.snapshot, applications=tuple(
            replace(r, last_seen=456) for r in app.backend.snapshot.applications))
        previous_row = window.rows[remote]
        window.apply_snapshot(modified)
        assert window.rows[remote] is previous_row
        assert window.selected_id == remote
        assert not window.groups['Unavailable'][0].get_expanded()
        window.groups['Unavailable'][0].set_expanded(True)
        window.split.set_show_content(False)
        GLib.timeout_add(400, prepare_scroll)
    except Exception:
        fail()
    return False


def prepare_scroll():
    try:
        window = app.window
        window.scroll.get_vadjustment().set_value(200)
        window.apply_snapshot(replace(app.backend.snapshot, applications=tuple(
            replace(r, last_seen=789) for r in app.backend.snapshot.applications)))
        GLib.timeout_add(400, check_scroll)
    except Exception:
        fail()
    return False


def check_scroll():
    try:
        assert abs(app.window.scroll.get_vadjustment().get_value() - 200) < 2, (app.window.scroll.get_vadjustment().get_value(), app.window.scroll.get_vadjustment().get_upper(), app.window.scroll.get_vadjustment().get_page_size())
        capture('light')
        app.activate_action('appearance', GLib.Variant('s', 'dark'))
        GLib.timeout_add(350, check_dark)
    except Exception:
        fail()
    return False


def check_dark():
    try:
        capture('dark')
        rows.append(dict(rows[0], executable_path='/opt/added-externally', ownership={}))
        atomic_write(path, json.dumps({'version': 1, 'apps': rows}).encode())
        GLib.timeout_add(300, check_refresh)
    except Exception:
        fail()
    return False


def check_refresh():
    try:
        assert time.monotonic() - started < 20
        if len(app.window.records) != 38:
            return True
        assert app.window.selected_id == ('ipv4:192.0.2.4', '/usr/lib/firefox/firefox')
        app.window.split.set_collapsed(True)
        app.window.row_activated(app.window.lists['Unavailable'], app.window.rows[app.window.selected_id])
        assert app.window.split.get_show_content()
        print('PASS: real backend isolation, unavailable policy, node provenance, hidden/service-only filtering, search, selection, stable rows/scroll/groups, atomic inventory refresh, details, light/dark, no Qt imports')
        app.quit()
    except Exception:
        fail()
    return False


GLib.timeout_add(200, check_loaded)
app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
