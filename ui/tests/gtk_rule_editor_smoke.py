"""Native rule CRUD with a fake daemon, normalization, failures and conflicts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from queue import Queue, Empty
import json
import tempfile
import time
from opensnitch.gtkui.apps import GtkApplication, AppsWindow, GLib, Gtk
from opensnitch.gtkui.live import LiveBackend
from opensnitch.gtkui.advanced import AdvancedView
from opensnitch.core.transport import pb

fixture = tempfile.TemporaryDirectory(prefix='opensnitch-editor-')
root = Path(fixture.name); rules_path = root / 'rules'; rules_path.mkdir()
executable = root / 'fixture-app'; executable.write_text('')
initial = pb.Rule(name='existing', action='allow', duration='always', enabled=True, created=int(time.time()), nolog=True,
    description='Original description', operator=pb.Operator(type='simple', operand='process.path', data=str(executable), sensitive=True))
(rules_path / 'existing.json').write_text(initial.SerializeToString().hex())
started = time.monotonic(); failures = []


class TestApplication(GtkApplication):
    def do_activate(self):
        from tests.test_transport import FakeDaemon
        self.window = AppsWindow(self)
        self.backend = LiveBackend(self, self.live_socket, root)
        self.backend.metadata.close()
        self.backend.subscribe(self.window.apply_snapshot)
        self.window.advanced = AdvancedView(self.backend, self.window)
        self.window.pages.add_titled(self.window.advanced.widget, 'advanced', 'Advanced')
        self.view = self.window.advanced.rules
        self.daemon = FakeDaemon(self.live_socket)
        self.configuration = pb.ClientConfig(isFirewallRunning=True,
            config=json.dumps({'Rules': {'Path': str(rules_path)}}), rules=[initial])
        self.requests = Queue(); self.readers = ThreadPoolExecutor(max_workers=2)
        self.notifications = []; self.fail_next = False
        self.reconnecting = False; self.phase = 0
        self.connect_daemon()
        self.window.advanced.open_rules(); self.window.present()
        GLib.timeout_add(75, step)
    def connect_daemon(self):
        self.reconnecting = True
        self.daemon.stub.Subscribe(self.configuration, timeout=2)
        stream = self.daemon.open_stream()
        def receive():
            try:
                for notification in stream: self.requests.put(notification)
            except Exception: pass
        self.readers.submit(receive)
    def do_shutdown(self):
        self.daemon.close(); GtkApplication.do_shutdown(self)
        self.readers.shutdown(wait=False, cancel_futures=True)


app = TestApplication(live_socket='unix://' + str(root / 'ui.sock'))


def select(name):
    for index in range(app.view.sorted.get_n_items()):
        if app.view.sorted.get_item(index).record.key == ('unix:/local', name):
            app.view.selection.set_selected(index); return app.view.selection.get_selected_item().record
    raise AssertionError('Missing rule ' + name)


def current(name='existing'):
    return app.backend.session.policies.rule_by_name('unix:/local', name)


def ready():
    return app.backend.snapshot.connected and not app.backend.session.verify_node('unix:/local') and not app.backend.session.controller.pending


def acknowledge(message):
    app.notifications.append(message)
    if app.fail_next:
        app.fail_next = False
        app.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.ERROR, data='Fixture rejected this update'))
        return
    rules = {rule.name: rule for rule in app.configuration.rules}
    for raw in message.rules:
        path = rules_path / (raw.name + '.json')
        if message.type == pb.DELETE_RULE:
            assert raw.operator.type == ''  # Delete sends only the identifier.
            rules.pop(raw.name, None); path.unlink(missing_ok=True)
        else:
            rule = pb.Rule(); rule.CopyFrom(raw); rule.created = int(time.time())
            if message.type == pb.ENABLE_RULE: rule.enabled = True
            if message.type == pb.DISABLE_RULE: rule.enabled = False
            if rule.operator.type == 'list': rule.operator.data = ''; rule.operator.operand = 'list'
            rules[rule.name] = rule
            if rule.duration == 'always': path.write_text(rule.SerializeToString().hex())
            else: path.unlink(missing_ok=True)
    del app.configuration.rules[:]; app.configuration.rules.extend(rules.values())
    app.daemon.replies.put(pb.NotificationReply(id=message.id, code=pb.OK))


def step():
    try:
        assert time.monotonic() - started < 40, ('Editor fixture timeout', app.phase)
        try: acknowledge(app.requests.get_nowait())
        except Empty: pass
        if app.backend.snapshot.connected:
            app.reconnecting = False
        elif not app.reconnecting and 'unix:/local' not in app.backend.session.transport.sessions:
            app.connect_daemon()
        if not ready(): return True
        if app.phase == 0:
            record = select('existing')
            key = ('unix:/local', str(executable))
            app.backend.session.catalog.set_ownership(key, app.backend.session.controller._ownership(key, current()))
            app.view.edit_rule(); editor = app.view.editor
            assert editor.get_modal() and editor.get_transient_for() is app.window
            assert not editor.name.get_editable()
            editor.action.set_selected(1)
            editor.duration.set_selected(len(__import__('opensnitch.gtkui.rule_editor', fromlist=['DURATIONS']).DURATIONS) - 1)
            editor.custom_duration.set_text('1h30m')
            editor.description.get_buffer().set_text('Edited description\nSecond line')
            editor.add_condition(dict(type='simple', operand='dest.port', data='443', sensitive=False, list=[]))
            editor.apply()
            assert editor._pending and current().action == 'allow'
            app.phase = 1
        elif app.phase == 1:
            rule = current()
            assert rule.action == 'deny' and rule.duration == '1h30m' and rule.nolog
            assert rule.description == 'Edited description\nSecond line'
            assert len(rule.operator['list']) == 2
            assert app.view.editor._closed
            assert not app.backend.session.catalog.entries[('unix:/local', str(executable))].ownership
            select('existing'); app.view.toggle_rule(); app.phase = 2
        elif app.phase == 2:
            assert not current().enabled and app.notifications[-1].type == pb.DISABLE_RULE
            select('existing'); app.view.toggle_rule(); app.phase = 3
        elif app.phase == 3:
            assert current().enabled and app.notifications[-1].type == pb.ENABLE_RULE
            select('existing'); app.view.duplicate_rule(); editor = app.view.editor
            assert editor.name.get_editable()
            editor.name.set_text('copy-rule'); editor.apply(); app.phase = 4
        elif app.phase == 4:
            assert current('copy-rule') and current()
            assert current('copy-rule').operator == current().operator
            select('copy-rule'); app.view.confirm_delete()
            dialog = app.window.get_visible_dialog(); assert dialog is not None
            dialog.emit('response', 'delete'); dialog.close()
            app.phase = 5
        elif app.phase == 5:
            assert current('copy-rule') is None and current() is not None
            select('existing'); app.view.edit_rule(); editor = app.view.editor
            editor.action.set_selected(0); app.fail_next = True; editor.apply(); app.phase = 6
        elif app.phase == 6:
            editor = app.view.editor
            assert not editor._closed and not editor._pending and current().action == 'deny'
            assert editor.action.get_selected() == 0  # Failed draft retained.
            assert 'Fixture rejected' in editor.status.get_text()
            editor.close(); app.view.new_rule(); editor = app.view.editor
            before = len(app.notifications); editor.apply()
            assert not editor._pending and len(app.notifications) == before
            editor.name.set_text('brand-new'); editor.conditions[0].value.set_text('/opt/example/new-app')
            editor.apply(); app.phase = 7
        elif app.phase == 7:
            assert current('brand-new') is not None
            assert ('unix:/local', '/opt/example/new-app') in app.backend.session.catalog.entries
            select('existing'); app.view.edit_rule(); editor = app.view.editor
            editor.description.get_buffer().set_text('Unsaved draft')
            for rule in app.configuration.rules:
                if rule.name == 'existing': rule.description = 'External change'
            app.connect_daemon(); app.phase = 8
        elif app.phase == 8:
            editor = app.view.editor
            if current().description != 'External change': return True
            assert not editor.save.get_sensitive()
            assert 'changed' in editor.status.get_text()
            before = len(app.notifications); editor.apply()
            assert len(app.notifications) == before and not editor._pending
            assert current().description == 'External change'
            editor.reload_rule(); app.phase = 9
        elif app.phase == 9:
            editor = app.view.editor
            if editor._reload_requested: return True
            assert editor.expected.description == 'External change'
            buffer = editor.description.get_buffer()
            assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True) == 'External change'
            assert editor.save.get_sensitive()
            assert not any(name.startswith(('PyQt5', 'PyQt6')) for name in __import__('sys').modules)
            app.window.theme_buttons['dark'].emit('clicked')
            GLib.timeout_add(250, capture)
            return False
    except Exception:
        import traceback
        traceback.print_exc(); failures.append(True); app.quit(); return False
    return True


def capture():
    try:
        window = app.view.editor
        paintable = Gtk.WidgetPaintable.new(window); snapshot = Gtk.Snapshot.new()
        paintable.snapshot(snapshot, window.get_width(), window.get_height())
        window.get_renderer().render_texture(snapshot.to_node(), None).save_to_png('/tmp/opensnitch-gtk-rule-editor-' + app.appearance + '.png')
        if app.appearance == 'dark':
            app.window.theme_buttons['light'].emit('clicked'); GLib.timeout_add(250, capture)
        else:
            print('PASS: native rule editor CRUD, flat AND conditions, flags/custom duration, modal/transient, ack-only updates, ownership cleanup, duplicate/toggle/delete, rejected drafts retained, external conflict/reload, light/dark, no Qt imports')
            app.view.editor.close(); app.quit()
    except Exception:
        import traceback
        traceback.print_exc(); failures.append(True); app.quit()
    return False


app.run([])
fixture.cleanup()
raise SystemExit(bool(failures))
