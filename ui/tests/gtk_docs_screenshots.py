"""Render README screenshots from sample snapshots, never the user's live state.

Run through run_gtk_virtual.py --check screenshots. Images go to /tmp; review
before copying to screenshots/. A fake daemon uses a private temporary socket.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from opensnitch.core.backend import ApplicationSnapshot, InterceptionState
from opensnitch.core.identity import ApplicationIdentity
from opensnitch.core.settings import PromptSettings
from opensnitch.core.transport import PromptRequest, pb
from opensnitch.gtkui.apps import GtkApplication, GLib, Gtk
from opensnitch.gtkui.live import LiveBackend
from opensnitch.gtkui.prompts import ConnectionPrompt
from tests.test_transport import FakeDaemon

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-docs-')
root = Path(fixture.name)
samples = json.loads((Path(__file__).parent / 'fixtures/gtk_apps.json').read_text())
failures = []


class DocumentationApplication(GtkApplication):
    def do_activate(self):
        with patch('opensnitch.gtkui.live.LiveBackend', lambda app, socket: LiveBackend(app, socket, root)):
            super().do_activate()
        self.daemon = FakeDaemon(self.live_socket)
        self.daemon.subscribe()
        GLib.timeout_add(700, show_samples)

    def do_shutdown(self):
        self.daemon.close()
        super().do_shutdown()


app = DocumentationApplication('light', live_socket='unix://' + str(root / 'ui.sock'))


def capture(window, name):
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, window.get_width(), window.get_height())
    node = snapshot.to_node()
    assert node is not None
    texture = window.get_renderer().render_texture(node, None)
    assert texture.save_to_png('/tmp/opensnitch-docs-' + name + '.png')


def guarded(callback):
    try:
        callback()
    except Exception:
        import traceback
        traceback.print_exc()
        failures.append(True)
        app.quit()
    return False


def later(callback):
    GLib.timeout_add(650, lambda: guarded(callback))


def show_samples():
    def prepare():
        records = []
        for sample in samples:
            if sample['node'] != 'This computer' or sample['id'] in ('chatgpt', 'unknown'):
                continue
            identity = ApplicationIdentity('unix:/local', sample['path'], sample['name'], sample['icon'], True)
            records.append(ApplicationSnapshot(identity, ('installed', 'observed'), time.time() - 120,
                available=True, policy=sample['policy'], action={'Allow': 'allow', 'Block': 'deny'}.get(sample['action'], ''),
                duration='1h' if sample['policy'] == 'Temporary rules' else 'always',
                related_rules=(sample['rule'],) if sample['rule'] else ()))
        app.backend.session.publish = lambda: None  # Freeze sample presentation during capture.
        app.backend.session.snapshot = replace(app.backend.snapshot, applications=tuple(records), connected=True,
            authoritative=True, message='Sample data · No live firewall changes', interception=InterceptionState(True, True))
        app.window.apply_snapshot(app.backend.snapshot)
        app.window.set_default_size(1180, 880)
        app.window.open_application('unix:/local', '/usr/lib/firefox/firefox')
        app.window.search.set_text('')
        app.window.filter_rows()
        later(app_light)
    return guarded(prepare)


def app_light():
    capture(app.window, 'apps-light')
    app.activate_action('appearance', GLib.Variant('s', 'dark'))
    later(app_dark)


def app_dark():
    capture(app.window, 'apps-dark')
    app.activate_action('appearance', GLib.Variant('s', 'light'))
    connection = pb.Connection(process_path='/usr/lib/firefox/firefox', process_args=['firefox'],
        process_id=1234, user_id=1000, protocol='tcp', dst_ip='203.0.113.10',
        dst_host='api.example.test', dst_port=443)
    request = PromptRequest('unix:/local', connection.SerializeToString())
    identity = ApplicationIdentity('unix:/local', connection.process_path, 'Firefox', 'firefox', True)
    app.sample_prompt = ConnectionPrompt(app, request,
        PromptSettings(action='deny', duration='always', timeout=30, advanced=True), identity,
        lambda *args: None, fixture=True)
    app.sample_prompt.advanced.set_expanded(False)
    app.sample_prompt.present()
    later(prompt_light)


def prompt_light():
    capture(app.sample_prompt, 'prompt-light')
    app.activate_action('appearance', GLib.Variant('s', 'dark'))
    later(prompt_dark)


def prompt_dark():
    capture(app.sample_prompt, 'prompt-dark')
    app.sample_prompt.request.cancelled.set()
    print('PASS: documentation screenshots rendered from isolated sample data')
    app.quit()


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
