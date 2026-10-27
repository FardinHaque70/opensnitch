"""Native live Apps workflow over a fake daemon, isolated catalog and history."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue, Empty
import json
import tempfile
import time

from opensnitch.gtkui.apps import GtkApplication, AppsWindow, GLib, Gtk
from opensnitch.gtkui.live import LiveBackend
from opensnitch.core.transport import pb
from tests.test_transport import FakeDaemon

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-gtk-live-')
root = Path(fixture.name)
rules = root / 'rules'; rules.mkdir()
executable = root / 'fixture-app'; executable.write_text('')
key = ('unix:/local', str(executable))
(root / 'settings.conf').write_text('[global]\ndefault_action=0\ndefault_duration=8\ndefault_timeout=30\n[database]\ntype=1\nfile=' + str(root / 'history.db') + '\n')
initial = pb.Rule(name='fixture-existing', action='allow', duration='always', enabled=True,
    operator=pb.Operator(type='simple', operand='process.path', data=str(executable), sensitive=True))
(rules / 'fixture-existing.json').write_text('initial fixture')
failures = []
started = time.monotonic()


class TestApplication(GtkApplication):
    def do_activate(self):
        self.window = AppsWindow(self)
        self.backend = LiveBackend(self, self.live_socket, root)
        self.backend.subscribe(self.window.apply_snapshot)
        self.daemon = FakeDaemon(self.live_socket)
        self.configuration = pb.ClientConfig(name='Fixture computer', isFirewallRunning=True,
            config=json.dumps({'Rules': {'Path': str(rules)}}), rules=[initial])
        self.requests = Queue()
        self.readers = ThreadPoolExecutor(max_workers=1)
        self.connect_daemon()
        self.window.present()
        GLib.timeout_add(150, check_initial)

    def connect_daemon(self):
        self.daemon.stub.Subscribe(self.configuration, timeout=2)
        stream = self.daemon.open_stream()
        def receive():
            try:
                for notification in stream:
                    self.requests.put(notification)
            except Exception:
                pass
        self.readers.submit(receive)

    def do_shutdown(self):
        self.daemon.close()
        GtkApplication.do_shutdown(self)
        self.readers.shutdown(wait=False, cancel_futures=True)


app = TestApplication(live_socket='unix://' + str(root / 'ui.sock'))


def row():
    return next(record for record in app.backend.snapshot.applications if record.key == key)


def fail():
    import traceback
    traceback.print_exc()
    failures.append(True); app.quit()


def acknowledge(success=True):
    try:
        message = app.requests.get_nowait()
    except Empty:
        return False
    if success:
        existing = {rule.name: rule for rule in app.configuration.rules}
        for rule in message.rules:
            path = rules / (rule.name + '.json')
            if message.type == pb.DELETE_RULE:
                path.unlink(missing_ok=True); existing.pop(rule.name, None)
            else:
                path.write_text(rule.SerializeToString().hex()); existing[rule.name] = rule
        del app.configuration.rules[:]
        app.configuration.rules.extend(existing.values())
    app.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.OK if success else pb.ERROR, data='' if success else 'Fixture rejection'))
    return True


def check_initial():
    try:
        assert time.monotonic() - started < 15
        if not app.backend.snapshot.connected or key not in app.window.rows:
            return True
        app.window.search.set_text(str(executable)); app.window.filter_rows()
        app.window.lists['Allowed'].select_row(app.window.rows[key])
        assert row().policy == 'Allowed'
        assert not app.window.nodes.get_visible()
        app.window.theme_buttons['dark'].emit('clicked')
        assert app.appearance == 'dark'
        app.original_row = app.window.rows[key]
        assert all(button.get_sensitive() for button in app.window.access_buttons)
        app.window.access_buttons[1].emit('clicked')
        assert row().pending and row().policy == 'Allowed'
        assert app.window.rows[key].badge.get_label() == 'Applying…'
        GLib.timeout_add(100, check_block)
    except Exception:
        fail()
    return False


def check_block():
    try:
        assert time.monotonic() - started < 15
        acknowledge()
        if row().pending:
            return True
        assert row().policy == 'Blocked'
        assert app.window.selected_id == key and app.window.rows[key] is app.original_row
        assert app.window.rows[key].get_parent() is app.window.lists['Blocked']
        app.window.access_buttons[0].emit('clicked')
        GLib.timeout_add(100, check_allow)
    except Exception:
        fail()
    return False


def check_allow():
    try:
        acknowledge()
        if row().pending:
            return True
        assert row().policy == 'Allowed'
        app.window.access_buttons[2].emit('clicked')
        GLib.timeout_add(100, check_defaults)
    except Exception:
        fail()
    return False


def check_defaults():
    try:
        acknowledge()
        if row().pending:
            return True
        assert row().policy == 'Not configured'
        assert not app.window.access_buttons[2].get_sensitive()
        app.window.access_buttons[1].emit('clicked')
        GLib.timeout_add(100, check_rejection)
    except Exception:
        fail()
    return False


def check_rejection():
    try:
        acknowledge(False)
        if row().pending:
            return True
        assert row().policy == 'Unavailable'
        assert not any(button.get_sensitive() for button in app.window.access_buttons)
        app.window.refresh_button.emit('clicked')
        GLib.timeout_add(150, reconnect)
    except Exception:
        fail()
    return False


def reconnect():
    try:
        app.connect_daemon()
        GLib.timeout_add(150, check_prompt)
    except Exception:
        fail()
    return False


def check_prompt():
    try:
        assert time.monotonic() - started < 20
        if not app.backend.snapshot.connected:
            return True
        assert row().policy == 'Not configured'
        connection = pb.Connection(process_path=str(executable), process_args=[str(executable)],
            process_id=1234, user_id=1000, protocol='tcp', dst_ip='203.0.113.10', dst_port=443)
        app.prompt = app.daemon.stub.AskRule.future(connection, timeout=10)
        GLib.timeout_add(150, answer_prompt)
    except Exception:
        fail()
    return False


def answer_prompt():
    try:
        assert time.monotonic() - started < 20
        if not app.backend.prompts.active:
            return True
        prompt = app.backend.prompts.active
        assert prompt.get_modal() and prompt.get_transient_for() is app.window
        prompt.decide('deny')
        GLib.timeout_add(150, confirm_prompt)
    except Exception:
        fail()
    return False


def confirm_prompt():
    try:
        if not app.prompt.done():
            return True
        rule = app.prompt.result()
        assert rule.action == 'deny'
        app.configuration.rules.append(rule)
        (rules / (rule.name + '.json')).write_text(rule.SerializeToString().hex())
        GLib.timeout_add(1500, resubscribe_prompt)
    except Exception:
        fail()
    return False


def resubscribe_prompt():
    try:
        if app.backend.snapshot.connected:
            return True
        app.connect_daemon()
        GLib.timeout_add(200, finish)
    except Exception:
        fail()
    return False


def finish():
    try:
        assert time.monotonic() - started < 25
        if not app.backend.snapshot.connected:
            return True
        assert row().policy == 'Blocked'
        assert app.window.selected_id == key
        assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in __import__('sys').modules)
        window = app.window
        paintable = Gtk.WidgetPaintable.new(window); snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        window.get_renderer().render_texture(snapshot.to_node(), None).save_to_png('/tmp/opensnitch-gtk-live.png')
        print('PASS: native live Apps, pending acknowledgments, Allow/Block/defaults, rejected replies, row/group/selection continuity, reconnect, real prompt round trip and authoritative refresh, no Qt imports')
        app.quit()
    except Exception:
        fail()
    return False


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
