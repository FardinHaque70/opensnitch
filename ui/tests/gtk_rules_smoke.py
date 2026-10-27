"""Read-only Advanced Rules with fake authoritative subscriptions and isolated state."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from queue import Queue
import csv
import io
import json
import tempfile
import time
from opensnitch.gtkui.apps import GtkApplication, AppsWindow, GLib, Gtk
from opensnitch.gtkui.live import LiveBackend
from opensnitch.gtkui.advanced import AdvancedView
from opensnitch.gtkui.activity import ActivityView
from opensnitch.core.transport import pb
from tests.test_transport import FakeDaemon

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-gtk-rules-')
root = Path(fixture.name)
rules_path = root / 'rules'; rules_path.mkdir()
executable = root / 'fixture-browser'; executable.write_text('')
operator = pb.Operator(type='simple', operand='process.path', data=str(executable), sensitive=True)
records = [pb.Rule(name=f'fixture-{i:03}', action='allow', duration='always', enabled=True, operator=operator) for i in range(120)]
compound = pb.Operator(type='list', operand='list', list=[operator,
    pb.Operator(type='list', operand='list', list=[pb.Operator(type='simple', operand='dest.port', data='443'),
    pb.Operator(type='regexp', operand='dest.host', data=r'^example\.test$', sensitive=True)])])
records += [pb.Rule(name='conditional', action='allow', enabled=True, duration='always', operator=compound, description='Exact app and nested network conditions'),
    pb.Rule(name='pattern', action='deny', enabled=True, duration='always', operator=pb.Operator(type='regexp', operand='process.path', data='^/usr/bin/.*', sensitive=True)),
    pb.Rule(name='incompatible-paths', action='deny', enabled=True, duration='always', operator=pb.Operator(type='list', list=[operator, pb.Operator(type='simple', operand='process.path', data='/another', sensitive=True)])),
    pb.Rule(name='port-only', action='deny', enabled=True, duration='always', operator=pb.Operator(type='simple', operand='dest.port', data='25')),
    pb.Rule(name='disabled', action='reject', enabled=False, duration='1h', precedence=True, nolog=True, operator=operator)]
for rule in records:
    rule.created = int(time.time())
    (rules_path / (rule.name + '.json')).write_text(rule.SerializeToString().hex())
original = {path.name: path.read_bytes() for path in rules_path.iterdir()}
failures = []; started = time.monotonic()


class TestApplication(GtkApplication):
    def do_activate(self):
        self.window = AppsWindow(self)
        self.backend = LiveBackend(self, self.live_socket, root)
        self.backend.metadata.close()
        self.backend.session.accept_index(({str(executable): ('Fixture Browser', '', '', '')}, set(), (), {}))
        self.backend.subscribe(self.window.apply_snapshot)
        self.window.activity = ActivityView(self.backend, self.window)
        self.window.pages.add_titled(self.window.activity.widget, 'activity', 'Activity')
        self.window.advanced = AdvancedView(self.backend, self.window)
        self.window.pages.add_titled(self.window.advanced.widget, 'advanced', 'Advanced')
        self.view = self.window.advanced.rules
        self.daemon = FakeDaemon(self.live_socket)
        self.configuration = pb.ClientConfig(isFirewallRunning=True,
            config=json.dumps({'Rules': {'Path': str(rules_path)}}), rules=records)
        self.incoming = Queue(); self.readers = ThreadPoolExecutor(max_workers=2)
        self.connect_daemon()
        remote = pb.ClientConfig(rules=[records[0]])
        self.backend.session.handle_event('subscribe', 'ipv4:192.0.2.44', remote.SerializeToString())
        self.window.advanced.open_rules()
        self.window.present()
        GLib.timeout_add(150, loaded)
    def connect_daemon(self):
        self.daemon.stub.Subscribe(self.configuration, timeout=2)
        stream = self.daemon.open_stream()
        def receive():
            try:
                for message in stream:
                    self.incoming.put(message)
            except Exception:
                pass
        self.readers.submit(receive)
    def do_shutdown(self):
        self.daemon.close()
        GtkApplication.do_shutdown(self)
        self.readers.shutdown(wait=False, cancel_futures=True)


app = TestApplication(live_socket='unix://' + str(root / 'ui.sock'))


def fail():
    import traceback
    traceback.print_exc(); failures.append(True); app.quit()


def select(key):
    for index in range(app.view.sorted.get_n_items()):
        if app.view.sorted.get_item(index).record.key == key:
            app.view.selection.set_selected(index)
            return app.view.selection.get_selected_item()
    raise AssertionError('Missing key ' + str(key))


def loaded():
    try:
        assert time.monotonic() - started < 18
        view = app.view
        if len(view.visible_items) != 126 or not app.backend.snapshot.connected:
            return True
        assert view.nodes.get_visible()
        local = select(('unix:/local', 'conditional'))
        assert local.identity.display_name == 'Fixture Browser'
        assert len(local.record.condition.children[1].children) == 2
        assert json.loads(local.record.json)['operator']['type'] == 'list'
        remote = select(('ipv4:192.0.2.44', 'fixture-000'))
        assert remote.identity.display_name == 'fixture-browser' and 'Cached' in remote.record.status
        pattern = select(('unix:/local', 'pattern'))
        assert not pattern.record.executable and pattern.record.application_label == 'Custom application match'
        global_rule = select(('unix:/local', 'port-only'))
        assert global_rule.record.application_label == 'Application not specified'
        view.enabled.set_selected(2); view.update()
        assert len(view.visible_items) == 1 and view.visible_items[0].record.nolog
        view.enabled.set_selected(0); view.search.set_text('Fixture Browser'); view.update()
        assert len(view.visible_items) == 122
        export = list(csv.DictReader(io.StringIO(view.csv_text())))
        assert len(export) == 122 and all(row['Exact executable'] == str(executable) for row in export)
        view.search.set_text(''); view.update()
        select(('unix:/local', 'conditional'))
        GLib.timeout_add(200, scroll_down)
    except Exception:
        fail()
    return False


def scroll_down():
    adjustment = app.view.scroll.get_vadjustment()
    if adjustment.get_upper() - adjustment.get_page_size() < 700:
        assert time.monotonic() - started < 18
        return True
    adjustment.set_value(700)
    GLib.timeout_add(150, update_subscription)
    return False


def update_subscription():
    try:
        app.position = app.view.scroll.get_vadjustment().get_value()
        assert app.position > 0
        app.configuration.rules[120].description = 'Updated by authoritative fixture subscription'
        app.connect_daemon()
        GLib.timeout_add(200, stable)
    except Exception:
        fail()
    return False


def stable():
    try:
        view = app.view
        record = view.selection.get_selected_item().record
        if record.description != 'Updated by authoritative fixture subscription' or not app.backend.snapshot.connected:
            return True
        assert record.key == ('unix:/local', 'conditional')
        assert abs(view.scroll.get_vadjustment().get_value() - app.position) < 2
        app.window.open_rules('unix:/local', str(executable))
        assert len(view.visible_items) == 123
        assert any(item.record.name == 'incompatible-paths' for item in view.visible_items)
        app.window.open_rule('unix:/local', 'conditional')
        assert len(view.visible_items) == 1
        select(('unix:/local', 'conditional'))
        view.open_activity(record)
        assert app.window.pages.get_visible_child_name() == 'activity'
        assert app.window.activity.browser.query.rule == 'conditional'
        app.window.open_rule('unix:/local', 'conditional')
        view.columns['name'].set_fixed_width(320)
        app.window.advanced.save_layout()
        assert app.backend.preferences.get('rules')['sort'] == 'name'
        assert app.incoming.empty(), 'Inspection must not send daemon mutation or task notifications'
        assert {path.name: path.read_bytes() for path in rules_path.iterdir()} == original
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
        window.get_renderer().render_texture(snapshot.to_node(), None).save_to_png('/tmp/opensnitch-gtk-rules-' + app.appearance + '.png')
        if app.appearance == 'dark':
            app.window.theme_buttons['light'].emit('clicked'); GLib.timeout_add(250, capture)
        else:
            assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in __import__('sys').modules)
            print('PASS: native Advanced Rules, nested conditions/raw JSON, exact/neutral identities, cached remote provenance, filters/raw export, stable subscription refresh, exact Apps/Activity navigation, layouts, light/dark, no daemon writes or Qt imports')
            app.quit()
    except Exception:
        fail()
    return False


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
