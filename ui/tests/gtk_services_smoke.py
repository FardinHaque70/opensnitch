"""Native Services integration: fake discovery, daemon and isolated state only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue, Empty
import json
import tempfile
import time
from dataclasses import replace
from opensnitch.gtkui.apps import GtkApplication, AppsWindow, GLib, Gtk
from opensnitch.gtkui.live import LiveBackend
from opensnitch.gtkui.services import ServicesView
from opensnitch.core.events import Event
from opensnitch.core.services import ServiceIdentity, ServiceAccessController
from opensnitch.core.transport import pb
from tests.test_transport import FakeDaemon

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-gtk-services-')
root = Path(fixture.name)
rules = root / 'rules'; rules.mkdir()
executable = root / 'service-daemon'; executable.write_bytes(b'\x7fELFfixture'); executable.chmod(0o755)
key = ('unix:/local', str(executable))
a = ServiceIdentity('system', 'alpha.service', 'Alpha service', 'active', 'running',
    configured_executable=str(executable), executable_path=str(executable))
b = replace(a, scope='user', unit_name='beta.service', description='Beta service', active_state='inactive')
wrapper = replace(a, unit_name='wrapper.service', description='Wrapper service', executable_path='', reason='A shared interpreter cannot identify this service executable.')
records = (a, b, wrapper) + tuple(replace(a, unit_name=f'fixture-{i:03}.service', description=f'Fixture {i:03}',
    executable_path=str(root / f'fixture-{i:03}')) for i in range(80))
initial = pb.Rule(name='fixture-service', action='allow', duration='always', enabled=True,
    operator=pb.Operator(type='simple', operand='process.path', data=str(executable), sensitive=True))
(rules / 'fixture-service.json').write_text('initial')
failures = []
started = time.monotonic()


class Provider:
    def __init__(self):
        self.records = {record.key: record for record in records}
        self.errors = {}; self.generation = 1; self.changed = Event()
        self.mutate = False
    def refresh(self, callback=None):
        def ready():
            if self.mutate:
                self.records.pop(b.key, None)
                self.mutate = False
            self.generation += 1
            self.changed.emit()
            if callback:
                callback()
            return False
        GLib.timeout_add(50, ready)
    def set_active(self, active):
        pass
    def close(self):
        pass


class TestApplication(GtkApplication):
    def do_activate(self):
        self.window = AppsWindow(self)
        self.backend = LiveBackend(self, self.live_socket, root)
        self.backend.services.close()
        self.backend.services = Provider()
        self.backend.services.changed.connect(self.backend._services_ready)
        self.backend.service_controls = ServiceAccessController(self.backend.services, self.backend.request_policy,
            self.backend._service_action_changed, self.backend._message)
        self.backend.subscribe(self.window.apply_snapshot)
        self.window.services = ServicesView(self.backend, self.window)
        self.window.pages.add_titled(self.window.services.widget, 'services', 'Services')
        self.window.pages.set_visible_child_name('services')
        self.daemon = FakeDaemon(self.live_socket)
        self.configuration = pb.ClientConfig(isFirewallRunning=True, config=json.dumps({'Rules': {'Path': str(rules)}}), rules=[initial])
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
        self.backend._services_ready()
        self.window.present()
        GLib.timeout_add(150, begin)
    def do_shutdown(self):
        self.daemon.close()
        GtkApplication.do_shutdown(self)
        self.readers.shutdown(wait=False, cancel_futures=True)


app = TestApplication(live_socket='unix://' + str(root / 'ui.sock'))


def fail():
    import traceback
    traceback.print_exc(); failures.append(True); app.quit()


def acknowledge():
    try:
        message = app.requests.get_nowait()
    except Empty:
        return
    for rule in message.rules:
        path = rules / (rule.name + '.json')
        if message.type == pb.DELETE_RULE:
            path.unlink(missing_ok=True)
        else:
            path.write_text(rule.SerializeToString().hex())
    app.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.OK))


def begin():
    try:
        assert time.monotonic() - started < 15
        if not app.backend.snapshot.connected:
            return True
        view = app.window.services
        assert len(view.visible_records) == 83
        assert ('unix:/local', str(root / 'fixture-000')) not in app.window.rows
        view.list.select_row(view.rows[a.key])
        assert all(button.get_sensitive() for button in view.controls.buttons)
        app.stable_row = view.rows[a.key]
        view.controls.buttons[1].emit('clicked')
        assert app.backend.service_controls.pending == a.key
        GLib.timeout_add(100, blocked)
    except Exception:
        fail()
    return False


def blocked():
    try:
        assert time.monotonic() - started < 15
        acknowledge()
        snapshot = app.backend.session.application_snapshots[key]
        if app.backend.service_controls.pending or snapshot.pending or snapshot.policy != 'Blocked':
            return True
        view = app.window.services
        assert view.selected == a.key and view.rows[a.key] is app.stable_row
        assert view.rows[b.key].state.get_label() == 'Blocked'
        view.controls.buttons[0].emit('clicked')
        GLib.timeout_add(100, allowed)
    except Exception:
        fail()
    return False


def allowed():
    try:
        acknowledge()
        snapshot = app.backend.session.application_snapshots[key]
        if app.backend.service_controls.pending or snapshot.pending or snapshot.policy != 'Allowed':
            return True
        app.window.services.controls.buttons[2].emit('clicked')
        GLib.timeout_add(100, defaults)
    except Exception:
        fail()
    return False


def defaults():
    try:
        acknowledge()
        snapshot = app.backend.session.application_snapshots[key]
        if app.backend.service_controls.pending or snapshot.pending or snapshot.policy != 'Not configured':
            return True
        view = app.window.services
        assert not view.controls.buttons[2].get_sensitive()
        app.backend.services.mutate = True
        view.controls.buttons[1].emit('clicked')
        GLib.timeout_add(200, changed_association)
    except Exception:
        fail()
    return False


def changed_association():
    try:
        assert app.requests.empty(), 'Changed sharing must not send any policy'
        view = app.window.services
        assert not app.backend.service_controls.pending
        view.list.select_row(view.rows[wrapper.key])
        assert not any(button.get_sensitive() for button in view.controls.buttons)
        assert 'interpreter' in view.controls.reason.get_label()
        view.search.set_text('alpha.service'); view.update()
        assert len(view.visible_records) == 1
        assert 'alpha.service' in view.csv_text() and 'wrapper.service' not in view.csv_text()
        view.search.set_text(''); view.scope.set_selected(2); view.update()
        assert not view.visible_records  # User-sharing entry was removed above.
        view.scope.set_selected(0); view.sort.set_selected(1); view.update()
        GLib.timeout_add(150, scroll_down)
    except Exception:
        fail()
    return False


def scroll_down():
    app.window.services.scroll.get_vadjustment().set_value(600)
    GLib.timeout_add(150, stable_scroll)
    return False


def stable_scroll():
    try:
        view = app.window.services
        app.position = view.scroll.get_vadjustment().get_value()
        assert app.position > 0
        app.backend.services.changed.emit()
        GLib.timeout_add(150, finish)
    except Exception:
        fail()
    return False


def finish():
    try:
        view = app.window.services
        assert abs(view.scroll.get_vadjustment().get_value() - app.position) < 2
        app.window.theme_buttons['dark'].emit('clicked')
        assert app.appearance == 'dark'
        assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in __import__('sys').modules)
        view.list.select_row(view.rows[a.key])
        view.scroll.get_vadjustment().set_value(0)
        GLib.timeout_add(250, capture)
    except Exception:
        fail()
    return False


def capture():
    try:
        window = app.window
        paintable = Gtk.WidgetPaintable.new(window); snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        window.get_renderer().render_texture(snapshot.to_node(), None).save_to_png('/tmp/opensnitch-gtk-services-' + app.appearance + '.png')
        if app.appearance == 'dark':
            app.window.theme_buttons['light'].emit('clicked')
            GLib.timeout_add(250, capture)
        else:
            print('PASS: native Services, shared-executable controls and acknowledgments, stale-association rejection, read-only wrappers, inactive scopes, search/sort/CSV, stable selection/scroll, light/dark, no Qt imports')
            app.quit()
    except Exception:
        fail()
    return False


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
