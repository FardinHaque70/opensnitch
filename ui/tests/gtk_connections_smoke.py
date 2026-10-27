"""Native Connections workflow over a fake daemon and isolated state."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue, Empty
from dataclasses import replace
import json
import tempfile
import time
from gi.repository import Gio
from opensnitch.gtkui.apps import GtkApplication, AppsWindow, GLib, Gtk
from opensnitch.gtkui.live import LiveBackend
from opensnitch.gtkui.connections import ConnectionsView
from opensnitch.core.transport import pb
from opensnitch.core.sockets import SocketSnapshot, parse_sockets
from tests.test_transport import FakeDaemon
from tests.test_sockets import payload

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-gtk-connections-')
root = Path(fixture.name)
rules = root / 'rules'; rules.mkdir()
executable = root / 'fixture-browser'; executable.write_text('')
failures = []; started = time.monotonic()


class TestApplication(GtkApplication):
    def do_activate(self):
        self.window = AppsWindow(self)
        from opensnitch.gtkui.desktop import APPLICATION_ID
        from gi.repository import Gdk, GdkPixbuf, GioUnix
        desktop = GioUnix.DesktopAppInfo.new(APPLICATION_ID + '.desktop')
        assert desktop is not None and isinstance(desktop.get_icon(), Gio.FileIcon)
        assert GdkPixbuf.Pixbuf.new_from_file(desktop.get_icon().get_file().get_path()).get_width() == 64
        assert Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).has_icon(APPLICATION_ID)
        assert Gtk.Window.get_default_icon_name() == APPLICATION_ID
        self.backend = LiveBackend(self, self.live_socket, root)
        self.backend.metadata.close()
        self.backend.session.accept_index(({str(executable): ('Fixture Browser', '', '', '')}, set(), (), {}))
        self.backend.subscribe(self.window.apply_snapshot)
        self.view = ConnectionsView(self.backend, self.window)
        self.window.connections = self.view
        self.window.pages.add_titled(self.view.widget, 'connections', 'Connections')
        self.window.pages.connect('notify::visible-child-name', lambda *args: self.view.set_active(self.window.pages.get_visible_child_name() == 'connections'))
        self.daemon = FakeDaemon(self.live_socket)
        self.configuration = pb.ClientConfig(isFirewallRunning=True, config=json.dumps({'Rules': {'Path': str(rules)}}))
        self.daemon.stub.Subscribe(self.configuration, timeout=2)
        stream = self.daemon.open_stream()
        self.requests = Queue(); self.readers = ThreadPoolExecutor(max_workers=1)
        def receive():
            try:
                for notification in stream:
                    self.requests.put(notification)
            except Exception:
                pass
        self.readers.submit(receive)
        self.window.present()
        GLib.timeout_add(100, begin)
    def do_shutdown(self):
        self.daemon.close()
        GtkApplication.do_shutdown(self)
        self.readers.shutdown(wait=False, cancel_futures=True)


app = TestApplication(live_socket='unix://' + str(root / 'ui.sock'))


def fail():
    import traceback
    traceback.print_exc(); failures.append(True); app.quit()


def begin():
    try:
        assert time.monotonic() - started < 15
        if not app.backend.snapshot.connected:
            return True
        app.window.pages.set_visible_child_name('connections')
        GLib.timeout_add(100, supply)
    except Exception:
        fail()
    return False


def supply():
    try:
        try:
            task = app.requests.get_nowait()
        except Empty:
            return True
        assert task.type == pb.TASK_START
        app.task_id = task.id
        raw = json.loads(payload(str(executable)))
        app.raw = raw
        template = raw['Table'][1]
        raw['Table'] = [json.loads(json.dumps(template)) for _ in range(100)]
        for index, row in enumerate(raw['Table']):
            row['Socket']['INode'] = 1000 + index
            row['Socket']['ID']['DestinationPort'] = 1000 + index
        app.daemon.replies.put(pb.NotificationReply(id=task.id, code=pb.OK, data=json.dumps(raw)))
        GLib.timeout_add(150, initial)
    except Exception:
        fail()
    return False


def initial():
    try:
        if app.view.store.get_n_items() != 100:
            return True
        view = app.view
        assert app.backend.session.sockets.snapshot.status == 'live'
        view.selection.set_selected(4)
        selected = view.selection.get_selected_item()
        assert selected.identity.display_name == 'Fixture Browser'
        app.selected_key = selected.record.key
        assert str(executable) in view.csv_text()
        assert 'Fixture Browser' not in view.csv_text()  # Export raw operation identity.
        view.table.sort_by_column(view.columns['pid'], Gtk.SortType.DESCENDING)
        assert view.selected_key == app.selected_key
        GLib.timeout_add(200, scroll_down)
    except Exception:
        fail()
    return False


def scroll_down():
    app.view.scroll.get_vadjustment().set_value(700)
    GLib.timeout_add(200, update_stream)
    return False


def update_stream():
    try:
        app.scroll = app.view.scroll.get_vadjustment().get_value()
        assert app.scroll > 0
        app.raw['Table'][4]['Socket']['WQueue'] = 123
        app.daemon.replies.put(pb.NotificationReply(id=app.task_id, code=pb.OK, data=json.dumps(app.raw)))
        GLib.timeout_add(200, verify_update)
    except Exception:
        fail()
    return False


def verify_update():
    try:
        view = app.view
        assert view.selected_key == app.selected_key
        assert view.selection.get_selected_item().record.send_queue == 123
        assert abs(view.scroll.get_vadjustment().get_value() - app.scroll) < 2
        view.search.set_text('203.0.113.10'); view.update()
        assert view.store.get_n_items() == 100
        view.search.set_text('no-such-address'); view.update()
        assert view.store.get_n_items() == 0
        view.search.set_text('fixture-browser'); view.update()
        assert view.store.get_n_items() == 100
        view.column_toggles['uid'].set_active(False)
        view.columns['pid'].set_fixed_width(130)
        view.save_layout()
        assert app.backend.preferences.get('connections')['sort'] == 'pid'
        view.interval.set_selected(0)
        assert view.monitor.snapshot.status == 'paused'
        view.monitor.refresh(once=True)
        GLib.timeout_add(100, one_snapshot)
    except Exception:
        fail()
    return False


def one_snapshot():
    try:
        task = None
        while True:
            try:
                message = app.requests.get_nowait()
            except Empty:
                break
            assert message.type in (pb.TASK_START, pb.TASK_STOP)
            if message.type == pb.TASK_START:
                task = message
        if task is None:
            return True
        app.daemon.replies.put(pb.NotificationReply(id=task.id, code=pb.OK, data=json.dumps(app.raw)))
        GLib.timeout_add(150, verify_pause)
    except Exception:
        fail()
    return False


def verify_pause():
    try:
        view = app.view
        if view.monitor.snapshot.status != 'paused':
            return True
        assert view.store.get_n_items() == 100
        view.selection.set_selected(4)
        obj = view.selection.get_selected_item()
        view.open_application(obj.record)
        assert app.window.pages.get_visible_child_name() == 'apps'
        assert app.window.selected_id == ('unix:/local', str(executable))
        app.window.pages.set_visible_child_name('connections')
        # Same executable on an explicitly remote snapshot cannot borrow local metadata.
        remote = 'ipv4:192.0.2.44'
        app.backend.session.identities.register_node(remote, False)
        remote_rows = parse_sockets(remote, payload(str(executable), address='2001:db8::44'))
        view.monitor.snapshot = SocketSnapshot(remote, remote_rows, time.time(), 'paused', 'Remote fixture snapshot · Paused')
        view.monitor.changed.emit(view.monitor.snapshot)
        view.selection.set_selected(0)
        obj = view.selection.get_selected_item()
        assert obj.identity.display_name == 'fixture-browser' and not obj.identity.exact
        assert obj.record.node == remote
        assert '[2001:db8::44]:443' in view.csv_text() or '2001:db8::44' in view.csv_text()
        app.window.theme_buttons['dark'].emit('clicked')
        GLib.timeout_add(300, capture)
    except Exception:
        fail()
    return False


def capture():
    try:
        window = app.window
        paintable = Gtk.WidgetPaintable.new(window); snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        window.get_renderer().render_texture(snapshot.to_node(), None).save_to_png('/tmp/opensnitch-gtk-connections-' + app.appearance + '.png')
        if app.appearance == 'dark':
            app.window.theme_buttons['light'].emit('clicked'); GLib.timeout_add(250, capture)
        else:
            assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in __import__('sys').modules)
            print('PASS: native Connections, repeated task replies, stable selection/scroll, filtering/sorting/layout, raw CSV, pause/one-shot refresh, Apps navigation, remote provenance, light/dark, no Qt imports')
            app.quit()
    except Exception:
        fail()
    return False


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
