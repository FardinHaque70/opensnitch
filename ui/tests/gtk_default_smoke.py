"""Default session closes to background and reactivates without a second backend."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import tempfile
import time
from opensnitch.gtkui.apps import GtkApplication, GLib, Gio
from opensnitch.gtkui.live import LiveBackend
from opensnitch.gtkui.desktop import APPLICATION_ID
from opensnitch.core.transport import pb
from tests.test_transport import FakeDaemon
from tests.test_tray import FakeWatcher
from opensnitch.gtkui.tray import ITEM, ITEM_PATH, MENU, MENU_PATH

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-gtk-default-')
root = Path(fixture.name); rules = root / 'rules'; rules.mkdir()
failures = []; started = time.monotonic()


class TestApplication(GtkApplication):
    def do_activate(self):
        from unittest.mock import patch
        with patch('opensnitch.gtkui.live.LiveBackend', lambda app, socket: LiveBackend(app, socket, root)):
            GtkApplication.do_activate(self)
        if hasattr(self, 'daemon'):
            return
        assert not self.window.get_visible(), 'Login must never map the main window'
        assert self.window.get_modal() and self.window.get_resizable()
        assert self.window.get_transient_for() is None
        self.daemon = FakeDaemon(self.live_socket)
        self.daemon.stub.Subscribe(pb.ClientConfig(config=json.dumps({'Rules': {'Path': str(rules)}})), timeout=2)
        self.daemon.open_stream()
        GLib.timeout_add(100, check_close)
    def do_shutdown(self):
        self.daemon.close(); GtkApplication.do_shutdown(self)


watcher = FakeWatcher()
exit_reason = root / 'exit-reason'
app = TestApplication(live_socket='unix://' + str(root / 'ui.sock'), background_on_close=True, start_hidden=True, exit_reason_file=exit_reason)
executor = ThreadPoolExecutor(max_workers=1)


def fail():
    import traceback
    traceback.print_exc(); failures.append(True); app.quit()


def check_close():
    try:
        assert time.monotonic() - started < 15
        if not app.backend.snapshot.connected or not app.tray.registered: return True
        assert not app.window.get_visible()
        assert app._tray_fallback == 0
        app.original_backend = app.backend
        app.window.present()
        app.window.close()
        assert not app.window.get_visible()
        assert app.daemon.stub.Ping(pb.PingRequest(id=77), timeout=2).id == 77
        connection = pb.Connection(process_path='/tmp/fixture-default-app', dst_ip='203.0.113.1', dst_port=443, protocol='tcp')
        app.prompt = app.daemon.stub.AskRule.future(connection, timeout=8)
        GLib.timeout_add(100, hidden_prompt)
    except Exception: fail()
    return False


def hidden_prompt():
    try:
        if not app.backend.prompts.active: return True
        prompt = app.backend.prompts.active
        assert prompt.get_modal() and prompt.get_transient_for() is app.window
        prompt.decide('deny')
        def activate():
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(app.tray.connection.get_unique_name(), ITEM_PATH, ITEM, 'Activate', GLib.Variant('(ii)', (0, 0)), None, 0, 2000, None)
            bus.call_sync(APPLICATION_ID, '/' + APPLICATION_ID.replace('.', '/'), 'org.freedesktop.Application', 'Activate',
                GLib.Variant('(a{sv})', ({},)), None, Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        app.activation = executor.submit(activate)
        GLib.timeout_add(100, finish)
    except Exception: fail()
    return False


def finish():
    try:
        assert time.monotonic() - started < 15
        if not app.activation.done() or not app.prompt.done(): return True
        app.activation.result()
        assert app.prompt.result().action == 'deny'
        assert app.window.get_visible() and app.backend is app.original_backend
        assert len(app.backend.session.transport.sessions) == 1
        assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in __import__('sys').modules)
        app.window.set_visible(False)
        watcher.release()
        GLib.timeout_add(100, check_fallback)
    except Exception: fail()
    return False


def check_fallback():
    try:
        assert time.monotonic() - started < 20
        if not app.window.get_visible(): return True
        assert not app.tray.registered
        watcher.acquire()
        GLib.timeout_add(100, quit_from_tray)
    except Exception: fail()
    return False


def quit_from_tray():
    try:
        assert time.monotonic() - started < 20
        if not app.tray.registered: return True
        assert app._tray_fallback == 0
        def request_quit():
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync(app.tray.connection.get_unique_name(), MENU_PATH, MENU, 'Event',
                GLib.Variant('(isvu)', (4, 'clicked', GLib.Variant('i', 0), 0)), None, 0, 2000, None)
        executor.submit(request_quit)
    except Exception: fail()
    return False


app.run([])
assert exit_reason.read_text().strip() == 'quit'
watcher.close()
print('PASS: default GTK session, hidden tray startup, native Open/status/Quit, prompts while hidden, one backend, watcher restart and no-tray fallback, no Qt imports')
executor.shutdown(wait=False, cancel_futures=True)
fixture.cleanup()
raise SystemExit(bool(failures))
