"""Native prompt integration with a fake daemon on a private socket.

Use --interactive for a harmless visual check. No installed daemon or application
inventory is accessed. All returned rules are discarded by the fake daemon.
"""
from dataclasses import replace
import sys
import tempfile
import time

from opensnitch.gtkui.apps import GtkApplication, Adw, GLib, Gtk, Gdk
from opensnitch.gtkui.prompts import PromptQueue
from opensnitch.core.identity import ApplicationIdentity
from opensnitch.core.settings import PromptSettings
from opensnitch.core.transport import UITransport, pb
from tests.test_transport import FakeDaemon

interactive = '--interactive' in sys.argv
fixture = tempfile.TemporaryDirectory(prefix='opensnitch-prompt-test-')
failures = []
started = time.monotonic()
settings = PromptSettings(action='deny', duration='once', timeout=30, advanced=interactive)


def dispatch(callback, *args):
    def run():
        callback(*args)
        return False
    GLib.idle_add(run)


class PromptApplication(GtkApplication):
    def do_activate(self):
        self.hold()
        self.window = Adw.ApplicationWindow(application=self, title='OpenSnitch — isolated test host', default_width=800, default_height=650)
        self.window.set_content(Adw.StatusPage(title='Isolated prompt test', description='This window is the parent of the test dialog. No firewall rules are changed.', icon_name='dialog-information-symbolic'))
        self.window.present()
        self.prompts = PromptQueue(self, lambda: settings,
            lambda node, path: ApplicationIdentity(node, path, 'Example application', '', False), fixture=True)
        address = 'unix://' + fixture.name + '/ui.sock'
        self.service = UITransport(address, dispatch, lambda *args: None, self.prompts.submit, lambda: settings)
        self.service.start()
        self.daemon = FakeDaemon(address)
        self.daemon.subscribe()
        connection = pb.Connection(process_path='/example/test-application', process_args=['test-application', '--example'],
            process_id=1234, user_id=1000, protocol='tcp', dst_ip='203.0.113.10', dst_host='api.example.test', dst_port=443)
        self.first = self.daemon.stub.AskRule.future(connection, timeout=3600 if interactive else 15)
        if interactive:
            GLib.timeout_add(300, check_interactive)
        else:
            self.second = self.daemon.stub.AskRule.future(connection, timeout=15)
            GLib.timeout_add(150, check_queue)

    def do_shutdown(self):
        if hasattr(self, 'prompts'):
            self.prompts.close()
            self.daemon.close()
            self.service.stop()
        GtkApplication.do_shutdown(self)


app = PromptApplication('light')


def fail():
    import traceback
    traceback.print_exc()
    failures.append(True)
    app.quit()


def capture(name):
    window = app.prompts.active
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    window.get_renderer().render_texture(snapshot.to_node(), None).save_to_png('/tmp/opensnitch-gtk-prompt-' + name + '.png')


def check_queue():
    try:
        assert time.monotonic() - started < 10
        if not app.prompts.active or len(app.prompts.pending) != 1:
            return True
        prompt = app.prompts.active
        assert prompt.get_modal()
        assert prompt.get_transient_for() is app.window
        assert prompt.key_pressed(None, Gdk.KEY_Escape, 0, 0)
        prompt.advanced.set_expanded(True)
        prompt.duration.set_selected(2)
        prompt.target.set_selected(1)
        assert prompt._paused
        prompt._deadline = time.monotonic() - 1
        prompt.tick()
        assert not prompt.request.done.is_set()
        GLib.timeout_add(500, check_light)
    except Exception:
        fail()
    return False


def check_light():
    try:
        capture('light')
        app.activate_action('appearance', GLib.Variant('s', 'dark'))
        GLib.timeout_add(350, check_dark)
    except Exception:
        fail()
    return False


def check_dark():
    try:
        capture('dark')
        app.prompts.active.decide('deny')
        GLib.timeout_add(250, check_response)
    except Exception:
        fail()
    return False


def check_response():
    try:
        completed = app.first if app.first.done() else app.second
        pending = app.second if app.first.done() else app.first
        rule = completed.result(timeout=1)
        assert rule.action == 'deny' and rule.duration == '5m'
        assert rule.operator.type == 'list'
        assert app.prompts.active is not None
        pending.cancel()
        GLib.timeout_add(250, check_cancel)
    except Exception:
        fail()
    return False


def check_cancel():
    global settings
    try:
        assert app.prompts.active is None
        settings = replace(settings, timeout=0, advanced=True)
        app.zero = app.daemon.stub.AskRule.future(pb.Connection(process_path='/example/zero', dst_ip='203.0.113.1'), timeout=3)
        GLib.timeout_add(150, check_zero)
    except Exception:
        fail()
    return False


def check_zero():
    global settings
    try:
        assert time.monotonic() - started < 12
        if not app.zero.done():
            return True
        assert app.zero.result().action == 'deny'
        assert app.prompts.active is None
        settings = replace(settings, timeout=1, advanced=False, action='reject')
        app.timed = app.daemon.stub.AskRule.future(pb.Connection(process_path='/example/timed', dst_ip='203.0.113.1'), timeout=3)
        GLib.timeout_add(200, check_timeout)
    except Exception:
        fail()
    return False


def check_timeout():
    try:
        assert time.monotonic() - started < 15
        if not app.timed.done():
            return True
        assert app.timed.result().action == 'reject'
        assert app.prompts.active is None
        assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in sys.modules)
        print('PASS: native prompt queue, real fixture gRPC round trip, compound choice, cancellation, Escape, paused/zero/default timeout, light/dark, no Qt imports')
        app.quit()
    except Exception:
        fail()
    return False


def check_interactive():
    if app.first.done():
        try:
            print('Test choice returned to fake daemon:', app.first.result().action, '(discarded)')
        except Exception:
            pass
        app.quit()
        return False
    return True


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
