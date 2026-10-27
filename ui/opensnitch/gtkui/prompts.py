"""Native connection prompts. Queues and timers belong to the GLib main thread."""
from collections import deque
from dataclasses import replace
import math
import time

from .apps import Adw, AppRow, Gdk, GLib, Gtk, app_image, label
from opensnitch.core.prompts import Target, build_rule, checksum_mismatches, targets
from opensnitch.core.protocol import to_proto
from opensnitch.core.settings import DURATIONS
from opensnitch.proto import import_

pb, _ = import_()
DURATION_LABELS = ('Once', '30 seconds', '5 minutes', '15 minutes', '30 minutes',
                   '1 hour', '12 hours', 'Until reboot', 'Until changed')


class ConnectionPrompt(Adw.ApplicationWindow):
    def __init__(self, application, request, settings, identity, finish, rules=(), fixture=False, update_checksums=None):
        super().__init__(application=application, title='Connection request', default_width=560, default_height=650)
        self.set_size_request(360, 360)
        parent = getattr(application, 'window', None)
        if isinstance(parent, Gtk.Window):
            self.set_transient_for(parent)
        self.set_modal(True)
        self.request, self.settings, self.finish = request, settings, finish
        self.connection = pb.Connection.FromString(request.connection)
        self._closed = False
        self._paused = settings.advanced
        self._deadline = time.monotonic() + settings.timeout
        self._timer = 0
        self.target_options = targets(self.connection)
        self.fixture = fixture
        self.connect('close-request', self.close_requested)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='Connection request', subtitle='Isolated test request' if fixture else request.node))
        view.add_top_bar(header)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                       margin_start=24, margin_end=24, margin_top=18, margin_bottom=24)
        row = AppRow(request.node, identity.display_name, identity.executable_path, request.node,
                     'Unavailable', '', '', '', '', identity.icon_reference, artwork=identity.icon_candidates)
        body.append(app_image(row, 64))
        identity_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        identity_box.append(label(identity.display_name, 'title-1', wrap=True, justify=Gtk.Justification.CENTER))
        identity_box.append(label(identity.executable_path or 'Unknown executable', 'dim-label', wrap=True, selectable=True))
        if identity.icon_owner:
            identity_box.append(label('Related app: ' + identity.icon_owner, 'dim-label', 'caption'))
        body.append(identity_box)
        destination = self.connection.dst_host or self.connection.dst_ip
        description = 'Connecting to ' + destination + ' · ' + self.connection.protocol.upper()
        if self.connection.dst_port:
            description += ' port ' + str(self.connection.dst_port)
        body.append(label(description, wrap=True, justify=Gtk.Justification.CENTER))
        if fixture:
            body.append(label('Test only · no firewall rules will be changed', 'warning', wrap=True))
        self.status = label('', 'dim-label', wrap=True)
        body.append(self.status)
        choices = Adw.PreferencesGroup()
        self.target = Adw.ComboRow(title='Apply to', model=Gtk.StringList.new([t.label for t in self.target_options]))
        index = settings.target if 0 <= settings.target < 6 else 0
        if self.connection.process_path.startswith('/tmp/.mount_'):
            index = next((i for i, t in enumerate(self.target_options) if t.label.startswith('This AppImage')), index)
        elif not self.connection.process_path:
            index = 2 if self.connection.dst_port else 3
        self.target.set_selected(index)
        self.duration = Adw.ComboRow(title='Duration', model=Gtk.StringList.new(DURATION_LABELS))
        self.duration.set_selected(DURATIONS.index(settings.duration))
        choices.add(self.target); choices.add(self.duration)
        self.target.connect('notify::selected', self.pause)
        self.duration.connect('notify::selected', self.pause)
        body.append(choices)
        self.advanced = Adw.ExpanderRow(title='Advanced conditions', subtitle='Restrict this decision further')
        self.extras = []
        destination_options = tuple(t for t in self.target_options if t.operand in ('dest.ip', 'dest.host', 'dest.network'))
        self.destination = Adw.ComboRow(title='Destination', model=Gtk.StringList.new([t.label for t in destination_options]))
        self.destination_options = destination_options
        self.check_destination = Adw.SwitchRow(title='Match destination', active=settings.destination)
        self.advanced.add_row(self.check_destination)
        self.advanced.add_row(self.destination)
        for title, target, active in (
                ('Match destination port', Target('', 'simple', 'dest.port', str(self.connection.dst_port)), settings.port),
                ('Match user', Target('', 'simple', 'user.id', str(self.connection.user_id)), settings.user),
                ('Match executable checksum (MD5)', Target('', 'simple', 'process.hash.md5', self.connection.process_checksums.get('process.hash.md5', '')), settings.checksum)):
            switch = Adw.SwitchRow(title=title, active=active and bool(target.data))
            switch.set_sensitive(bool(target.data) and not (target.operand == 'dest.port' and not self.connection.dst_port))
            switch.connect('notify::active', self.pause)
            self.advanced.add_row(switch)
            self.extras.append((switch, target))
        self.check_destination.connect('notify::active', self.pause)
        self.destination.connect('notify::selected', self.pause)
        self.advanced.set_expanded(settings.advanced)
        self.advanced.connect('notify::expanded', self.pause)
        advanced_group = Adw.PreferencesGroup()
        advanced_group.add(self.advanced)
        body.append(advanced_group)
        details = Adw.ExpanderRow(title='Connection details')
        for title, text in (
            ('Executable', self.connection.process_path or 'Unknown executable'),
            ('Command line', ' '.join(self.connection.process_args)), ('Process ID', str(self.connection.process_id)),
            ('User ID', str(self.connection.user_id)), ('Source', self.connection.src_ip + ':' + str(self.connection.src_port)),
            ('Destination', self.connection.dst_ip + ':' + str(self.connection.dst_port)),
            ('Working directory', self.connection.process_cwd), ('Node', request.node),
            ('Checksums', '\n'.join(key + ': ' + value for key, value in self.connection.process_checksums.items())),
            ('Process tree', '\n'.join(str(item.key) + ': ' + str(item.value) for item in self.connection.process_tree))):
            if text:
                entry = Adw.ActionRow(title=title, subtitle=text, use_markup=False)
                entry.set_subtitle_selectable(True); entry.set_subtitle_lines(0)
                details.add_row(entry)
        details.connect('notify::expanded', self.pause)
        information = Adw.PreferencesGroup(); information.add(details); body.append(information)
        mismatches = checksum_mismatches(self.connection, rules)
        if mismatches:
            warning = Adw.PreferencesGroup(title='Executable checksum changed', description='The executable differs from these rules. Review it before trusting the current checksum.')
            for name, expected in mismatches:
                warning.add(Adw.ActionRow(title=name, subtitle='Expected MD5: ' + expected, use_markup=False))
            if update_checksums is not None:
                choices = Adw.ComboRow(title='Rule to update', model=Gtk.StringList.new([name for name, expected in mismatches]))
                warning.add(choices)
                checksum_status = label('', wrap=True)
                actions = Gtk.Box(spacing=8, homogeneous=True)
                def update(all_rules):
                    names = [name for name, expected in mismatches] if all_rules else [mismatches[choices.get_selected()][0]]
                    checksum_status.set_text('Updating…')
                    actions.set_sensitive(False)
                    def confirmed(success, message):
                        checksum_status.set_text(message)
                        actions.set_sensitive(not success)
                    update_checksums(request.node, names, self.connection, confirmed)
                for title, all_rules in (('Trust current checksum', False), ('Update all listed rules', True)):
                    button = Gtk.Button(label=title)
                    button.connect('clicked', lambda button, all_rules=all_rules: update(all_rules))
                    actions.append(button)
                warning.add(actions)
                warning.add(checksum_status)
            body.append(warning)
            self._paused = True
        self.buttons = {}
        buttons = Gtk.Box(spacing=10, homogeneous=True)
        for action, title in (('deny', '_Block'), ('reject', '_Reject'), ('allow', '_Allow')):
            button = Gtk.Button(label=title, use_underline=True)
            button.add_css_class('suggested-action' if action == settings.action else 'flat')
            button.connect('clicked', lambda button, action=action: self.decide(action))
            self.buttons[action] = button
            buttons.append(button)
        buttons.set_margin_start(18); buttons.set_margin_end(18)
        buttons.set_margin_top(12); buttons.set_margin_bottom(18)
        view.add_bottom_bar(buttons)
        view.set_vexpand(True)
        view.set_hexpand(True)
        self.set_content(view)
        view.set_content(Gtk.ScrolledWindow(child=body, hscrollbar_policy=Gtk.PolicyType.NEVER))
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.key_pressed)
        self.add_controller(keys)
        self._timer = GLib.timeout_add(200, self.tick)
        self.tick()

    def key_pressed(self, controller, key, keycode, state):
        # Escape never applies an unintended choice. Alt+B/R/A are button mnemonics.
        return key == Gdk.KEY_Escape

    def pause(self, *args):
        self._paused = True
        self.tick()

    def tick(self):
        if self._closed:
            return False
        if self.request.cancelled.is_set():
            self.dismiss()
            return False
        remaining = max(0, math.ceil(self._deadline - time.monotonic()))
        if self._paused:
            self.status.set_text('Countdown paused while you review the request')
        else:
            self.status.set_text(self.settings.action.capitalize() + ' by default in ' + str(remaining) + ' seconds')
            if remaining == 0:
                self.decide(self.settings.action)
                return False
        return True

    def decide(self, action):
        if self._closed or self.request.cancelled.is_set():
            return
        try:
            extras = [target for switch, target in self.extras if switch.get_active() and switch.get_sensitive()]
            if self.check_destination.get_active() and self.destination_options:
                extras.append(self.destination_options[self.destination.get_selected()])
            rule = build_rule(self.connection, action, DURATIONS[self.duration.get_selected()],
                              self.target_options[self.target.get_selected()], extras)
            result = to_proto(rule, pb.Rule)
        except ValueError as error:
            self.status.set_text(str(error)); self._paused = True; return
        import logging
        logging.info('Prompt decision: node=%s executable=%s action=%s duration=%s target=%s', self.request.node, self.connection.process_path, action, result.duration, result.operator.operand)
        self.request.answer(result)
        self.dismiss()

    def close_requested(self, *args):
        self.decide(self.settings.action)
        return True

    def dismiss(self):
        if self._closed:
            return
        self._closed = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.destroy()
        self.finish(self.request)


class PromptQueue:
    def __init__(self, application, settings, resolve, rules=lambda node, path: (), fixture=False, update_checksums=None):
        self.application, self.settings, self.resolve, self.rules = application, settings, resolve, rules
        self.fixture = fixture
        self.update_checksums = update_checksums
        self.pending = deque()
        self.active = None
        self.closed = False

    def submit(self, request):
        if self.closed:
            request.cancelled.set(); request.done.set(); return
        if request.cancelled.is_set():
            if self.active and self.active.request is request:
                self.active.dismiss()
            else:
                self.pending = deque(item for item in self.pending if item is not request)
            return
        self.pending.append(request)
        self._next()

    def _next(self):
        if self.active is not None or self.closed:
            return
        while self.pending:
            request = self.pending.popleft()
            if request.cancelled.is_set():
                continue
            settings = self.settings()
            if settings.disabled or settings.timeout == 0:
                settings = replace(settings, timeout=0, advanced=False)
            connection = pb.Connection.FromString(request.connection)
            identity = self.resolve(request.node, connection.process_path)
            self.active = ConnectionPrompt(self.application, request, settings, identity, self.finished,
                self.rules(request.node, connection.process_path), self.fixture, self.update_checksums)
            if not self.active._closed:
                self.active.present()
            else:
                self.active = None
                continue
            break

    def finished(self, request):
        if self.active and self.active.request is request:
            self.active = None
        GLib.idle_add(self._next)

    def close(self):
        self.closed = True
        for request in self.pending:
            request.cancelled.set(); request.done.set()
        self.pending.clear()
        if self.active:
            self.active.request.cancelled.set(); self.active.request.done.set()
            self.active.dismiss()
