"""Native Activity, real worker/history format, fake daemon and isolated state."""
from pathlib import Path
import csv
import json
import tempfile
import time
from opensnitch.gtkui.apps import GtkApplication, AppsWindow, GLib, Gtk, Adw
from opensnitch.gtkui.live import LiveBackend
from opensnitch.gtkui.activity import ActivityView
from opensnitch.core.transport import pb
from tests.test_transport import FakeDaemon

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-gtk-activity-')
root = Path(fixture.name)
rules = root / 'rules'; rules.mkdir()
executable = root / 'fixture-browser'; executable.write_text('')
(rules / 'fixture-rule.json').write_text('rule fixture')
initial = pb.Rule(name='fixture-rule', action='allow', enabled=True, duration='always',
    operator=pb.Operator(type='simple', operand='process.path', data=str(executable)))
failures = []; started = time.monotonic()
base_time = int(time.time())


def statistics(start, count):
    return pb.Statistics(events=[pb.Event(unixnano=(base_time + i) * 10**9,
        connection=pb.Connection(process_path=str(executable), process_id=1000 + i, user_id=1000,
            process_args=[str(executable), '--fixture'], process_cwd='/a long working directory',
            src_ip='192.0.2.1', src_port=5000 + i, dst_ip='203.0.113.20', dst_host='example.test', dst_port=443, protocol='tcp'),
        rule=pb.Rule(name='fixture-rule', action='allow' if i % 2 == 0 else 'deny')) for i in range(start, start + count)])


class TestApplication(GtkApplication):
    def do_activate(self):
        self.window = AppsWindow(self)
        self.backend = LiveBackend(self, self.live_socket, root)
        self.backend.metadata.close()
        self.backend.session.accept_index(({str(executable): ('Fixture Browser', '', '', '')}, set(), (), {}))
        self.backend.subscribe(self.window.apply_snapshot)
        self.view = ActivityView(self.backend, self.window)
        self.window.activity = self.view
        self.window.pages.add_titled(self.view.widget, 'activity', 'Activity')
        self.window.pages.connect('notify::visible-child-name', lambda *args: self.view.set_active(self.window.pages.get_visible_child_name() == 'activity'))
        self.daemon = FakeDaemon(self.live_socket)
        self.daemon.stub.Subscribe(pb.ClientConfig(isFirewallRunning=True,
            config=json.dumps({'Rules': {'Path': str(rules)}}), rules=[initial]), timeout=2)
        self.daemon.open_stream()
        self.window.pages.set_visible_child_name('activity')
        self.window.present()
        self.daemon.stub.Ping(pb.PingRequest(stats=statistics(0, 300)), timeout=2)
        self.backend.session.history.submit('statistics', 'ipv4:192.0.2.44', statistics(0, 3).SerializeToString())
        GLib.timeout_add(100, initial_page)
    def do_shutdown(self):
        self.daemon.close()
        GtkApplication.do_shutdown(self)


app = TestApplication(live_socket='unix://' + str(root / 'ui.sock'))


def fail():
    import traceback
    traceback.print_exc(); failures.append(True); app.quit()


def initial_page():
    try:
        assert time.monotonic() - started < 18
        view = app.view
        if view.browser.snapshot.total != 303:
            view.browser.refresh()
            return True
        assert view.store.get_n_items() == 250
        assert view.nodes.get_visible()
        view.selection.set_selected(4)
        assert view.selection.get_selected_item().identity.display_name == 'Fixture Browser'
        app.selected = view.selected_key
        # Root header has controls; all detail headers explicitly suppress them.
        headers = []
        def walk(widget):
            if isinstance(widget, Adw.HeaderBar): headers.append(widget)
            child = widget.get_first_child()
            while child:
                walk(child); child = child.get_next_sibling()
        walk(app.window)
        assert sum(header.get_show_end_title_buttons() for header in headers) == 1
        assert sum(not header.get_show_start_title_buttons() and not header.get_show_end_title_buttons() for header in headers) >= 2
        view.action.set_selected(2)
        GLib.timeout_add(100, filtered)
    except Exception:
        fail()
    return False


def filtered():
    try:
        view = app.view
        if view.browser.snapshot.total != 151:
            return True
        assert all(item.record.action == 'deny' for item in view.visible_items)
        view.action.set_selected(0)
        view.search.set_text('Fixture Browser')
        GLib.timeout_add(350, named)
    except Exception:
        fail()
    return False


def named():
    try:
        view = app.view
        if view.browser.snapshot.total != 300:
            return True
        assert all(item.record.node == 'unix:/local' for item in view.visible_items)
        view.search.set_text('')
        GLib.timeout_add(350, ready_scroll)
    except Exception:
        fail()
    return False


def ready_scroll():
    try:
        view = app.view
        if view.browser.snapshot.total != 303:
            return True
        view.selection.set_selected(4)
        app.selected = view.selected_key
        view.scroll.get_vadjustment().set_value(700)
        GLib.timeout_add(150, incoming_while_browsing)
    except Exception:
        fail()
    return False


def incoming_while_browsing():
    try:
        view = app.view
        assert not view.browser.live
        app.position = view.scroll.get_vadjustment().get_value()
        assert app.position > 0
        app.daemon.stub.Ping(pb.PingRequest(stats=statistics(300, 20)), timeout=2)
        GLib.timeout_add(200, stable_page)
    except Exception:
        fail()
    return False


def stable_page():
    try:
        view = app.view
        assert view.browser.snapshot.total == 303
        assert abs(view.scroll.get_vadjustment().get_value() - app.position) < 2
        assert view.selected_key == app.selected
        view.next.emit('clicked')
        GLib.timeout_add(100, second_page)
    except Exception:
        fail()
    return False


def second_page():
    try:
        view = app.view
        if view.browser.snapshot.offset != 250:
            return True
        assert view.store.get_n_items() == 53
        remote = next(item for item in view.visible_items if item.record.node == 'ipv4:192.0.2.44')
        assert remote.identity.display_name == 'fixture-browser' and not remote.identity.exact
        view.browser.export(root / 'activity.csv', exported)
    except Exception:
        fail()
    return False


def exported(count, error):
    try:
        assert not error and count == 303
        with (root / 'activity.csv').open() as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 303 and rows[0]['process'] == str(executable)
        assert 'fixture-rule' in rows[0]['rule']
        app.backend.session.catalog.flush()
        app.inventory = (root / 'applications.json').read_bytes()
        app.view.clear_history()
        GLib.timeout_add(100, cleared)
    except Exception:
        fail()


def cleared():
    try:
        view = app.view
        if view.browser.snapshot.total != 20:
            return True
        assert (root / 'applications.json').read_bytes() == app.inventory
        assert (rules / 'fixture-rule.json').read_text() == 'rule fixture'
        assert app.backend.session.policies.application_rules('unix:/local', str(executable))
        view.selection.set_selected(0)
        record = view.selection.get_selected_item().record
        app.window.open_application(record.node, record.executable)
        assert app.window.selected_id == ('unix:/local', str(executable))
        app.window.open_activity(record.node, record.executable)
        GLib.timeout_add(150, navigation)
    except Exception:
        fail()
    return False


def navigation():
    try:
        assert app.window.pages.get_visible_child_name() == 'activity'
        assert app.view.browser.query.executable == str(executable)
        app.view.selection.set_selected(0)
        app.view.columns['executable'].set_fixed_width(260)
        app.view.save_layout()
        assert app.backend.preferences.get('activity')['sort'] == 'time'
        assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in __import__('sys').modules)
        app.window.theme_buttons['dark'].emit('clicked')
        GLib.timeout_add(250, capture)
    except Exception:
        fail()
    return False


def capture():
    try:
        window = app.window
        paintable = Gtk.WidgetPaintable.new(window); snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        window.get_renderer().render_texture(snapshot.to_node(), None).save_to_png('/tmp/opensnitch-gtk-activity-' + app.appearance + '.png')
        if app.appearance == 'dark':
            app.window.theme_buttons['light'].emit('clicked'); GLib.timeout_add(250, capture)
        else:
            print('PASS: native Activity, worker-backed pagination, friendly-name and decision filters, stable browsing during live events, remote provenance, all-page raw export, scoped clear preserving new events/inventory/rules, Apps navigation, layout, light/dark, single window-control row, no Qt imports')
            app.quit()
    except Exception:
        fail()
    return False


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
