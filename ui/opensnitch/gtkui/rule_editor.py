"""Native rule editor over the shared draft validator and policy controller."""
from dataclasses import replace
import json
import time
import uuid
from .apps import Adw, Gtk, label
from opensnitch.core.rule_edit import OPERANDS, KINDS, draft_rule, editable_conditions, rule_from_json, transport_edit_reason

DURATIONS = (('Until changed', 'always'), ('Until daemon restart', 'until restart'), ('Once', 'once'),
    ('30 seconds', '30s'), ('5 minutes', '5m'), ('15 minutes', '15m'), ('30 minutes', '30m'),
    ('1 hour', '1h'), ('12 hours', '12h'), ('Custom duration', ''))
FIELD_LABELS = {'process.path': 'Executable', 'process.parent.path': 'Parent executable', 'process.command': 'Command line',
    'process.id': 'Process ID', 'user.id': 'User ID', 'user.name': 'User name', 'source.ip': 'Source IP',
    'source.port': 'Source port', 'source.network': 'Source network', 'dest.ip': 'Destination IP', 'dest.host': 'Destination hostname',
    'dest.port': 'Destination port', 'dest.network': 'Destination network', 'protocol': 'Protocol', 'iface.in': 'Incoming interface',
    'iface.out': 'Outgoing interface', 'process.hash.md5': 'MD5 checksum', 'process.hash.sha1': 'SHA1 checksum',
    'lists.domains': 'Domain list files', 'lists.domains_regexp': 'Regex domain list files', 'lists.ips': 'IP list files',
    'lists.nets': 'Network list files', 'lists.hash.md5': 'MD5 list files', 'true': 'Any connection'}


class ConditionEditor:
    def __init__(self, condition, remove):
        self._loading = True
        self.row = Adw.ExpanderRow(title='Condition', subtitle=condition.get('data', ''), expanded=True, use_markup=False)
        fields = [FIELD_LABELS[field] + ' · ' + field for field in OPERANDS] + ['Custom field / environment variable']
        self.field = Adw.ComboRow(title='Match field', model=Gtk.StringList.new(fields), use_markup=False)
        operand = condition.get('operand', 'process.path')
        self.field.set_selected(OPERANDS.index(operand) if operand in OPERANDS else len(OPERANDS))
        self.row.add_row(self.field)
        self.custom = Adw.EntryRow(title='Custom field', text=operand if operand not in OPERANDS else '')
        self.custom.set_tooltip_text('For example process.env.NAME')
        self.row.add_row(self.custom)
        self.kind = Adw.ComboRow(title='Comparison', model=Gtk.StringList.new(['Equals', 'Regular expression (RE2)', 'Network / CIDR', 'List files']))
        self.kind.set_selected(KINDS.index(condition.get('type', 'simple')))
        self.row.add_row(self.kind)
        self.value = Adw.EntryRow(title='Value', text=condition.get('data', ''))
        self.row.add_row(self.value)
        self.sensitive = Adw.SwitchRow(title='Case-sensitive', active=bool(condition.get('sensitive', False)))
        self.row.add_row(self.sensitive)
        button = Gtk.Button(label='Remove condition', valign=Gtk.Align.CENTER)
        button.connect('clicked', lambda *args: remove(self))
        self.remove_button = button
        self.row.add_suffix(button)
        self.field.connect('notify::selected', self.field_changed)
        self.value.connect('changed', lambda *args: self.row.set_subtitle(self.value.get_text() or '(empty value)'))
        self._loading = False
        self.custom.set_visible(self.field.get_selected() == len(OPERANDS))
        self.row.set_title(FIELD_LABELS.get(operand, operand))

    def field_changed(self, *args):
        custom = self.field.get_selected() == len(OPERANDS)
        self.custom.set_visible(custom)
        if not custom:
            operand = OPERANDS[self.field.get_selected()]
            self.row.set_title(FIELD_LABELS[operand])
            if not self._loading:
                if operand in ('source.network', 'dest.network'):
                    self.kind.set_selected(KINDS.index('network'))
                elif operand.startswith('lists.'):
                    self.kind.set_selected(KINDS.index('lists'))
                elif KINDS[self.kind.get_selected()] in ('network', 'lists'):
                    self.kind.set_selected(0)

    @property
    def operator(self):
        operand = self.custom.get_text() if self.field.get_selected() == len(OPERANDS) else OPERANDS[self.field.get_selected()]
        return dict(type=KINDS[self.kind.get_selected()], operand=operand, data=self.value.get_text(),
                    sensitive=self.sensitive.get_active(), list=[])


class RuleEditor(Adw.ApplicationWindow):
    def __init__(self, backend, window, node, record=None, duplicate=False):
        super().__init__(application=window.get_application(), title='New rule' if record is None or duplicate else 'Edit rule',
                         default_width=780, default_height=820, transient_for=window, modal=True)
        self.backend, self.parent_window, self.node = backend, window, node
        self.expected = None if record is None or duplicate else rule_from_json(record.json)
        source = rule_from_json(record.json) if record else None
        self._duplicate = duplicate
        self._template = source if duplicate else None
        self._last_failure = ''
        self._pending = self._closed = self._reload_requested = False
        self.conditions = []
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False); header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(title=self.get_title(), subtitle='This computer' if node == 'unix:/local' else node))
        cancel = Gtk.Button(label='Cancel'); cancel.connect('clicked', lambda *args: self.close())
        header.pack_start(cancel)
        self.save = Gtk.Button(label='Save')
        self.save.add_css_class('suggested-action'); self.save.connect('clicked', self.apply)
        header.pack_end(self.save)
        toolbar.add_top_bar(header)
        self.page = Adw.PreferencesPage()
        self.form = Adw.PreferencesGroup(title='Rule')
        self.name = Adw.EntryRow(title='Rule identifier', text=source.name if source and not duplicate else 'ui-rule-' + uuid.uuid4().hex[:12])
        self.name.set_editable(self.expected is None)
        self.name.set_tooltip_text('Identifiers stay fixed while editing. Duplicate creates a separate rule.')
        self.form.add(self.name)
        self.action = Adw.ComboRow(title='Action', model=Gtk.StringList.new(['Allow', 'Block', 'Reject']))
        self.form.add(self.action)
        self.duration = Adw.ComboRow(title='Duration', model=Gtk.StringList.new([title for title, value in DURATIONS]))
        self.form.add(self.duration)
        self.custom_duration = Adw.EntryRow(title='Custom duration')
        self.custom_duration.set_tooltip_text('Positive Go duration, such as 1h30m or 500ms')
        self.form.add(self.custom_duration)
        self.duration.connect('notify::selected', lambda *args: self.custom_duration.set_visible(self.duration.get_selected() == len(DURATIONS) - 1))
        self.enabled = Adw.SwitchRow(title='Enabled')
        self.priority = Adw.SwitchRow(title='Priority rule', subtitle='Priority rules take precedence when their conditions match.')
        self.logging = Adw.SwitchRow(title='Record matching activity')
        for row in (self.enabled, self.priority, self.logging): self.form.add(row)
        self.page.add(self.form)
        description = Adw.PreferencesGroup(title='Description')
        self.description = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=10, right_margin=10, top_margin=10, bottom_margin=10)
        description.add(Gtk.ScrolledWindow(child=self.description, min_content_height=80, max_content_height=160))
        self.condition_group = Adw.PreferencesGroup(title='Conditions', description='All conditions must match. Choose Any connection explicitly for an unrestricted rule.')
        self.page.add(self.condition_group)
        self.add_condition_button = Gtk.Button(label='Add condition')
        self.add_condition_button.connect('clicked', lambda *args: self.add_condition())
        more = Adw.PreferencesGroup(); more.add(self.add_condition_button); self.page.add(more)
        self.page.add(description)
        toolbar.set_content(self.page)
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=18, margin_end=18, margin_top=8, margin_bottom=12)
        self.status = label('', 'caption', xalign=0, wrap=True)
        footer.append(self.status)
        self.reload = Gtk.Button(label='Reload current rule', visible=self.expected is not None, halign=Gtk.Align.START)
        self.reload.set_tooltip_text('Discard this draft and load a fresh daemon snapshot.')
        self.reload.connect('clicked', self.reload_rule); footer.append(self.reload)
        toolbar.add_bottom_bar(footer)
        self.set_content(toolbar)
        self.load_source(source, duplicate)
        self._unsubscribe = backend.subscribe(self.source_changed)
        self.connect('close-request', self.closing)

    def load_source(self, source, duplicate=False):
        for item in self.conditions: self.condition_group.remove(item.row)
        self.conditions = []
        if source:
            reason = transport_edit_reason(source.operator)
            if reason: raise ValueError(reason)
            if not duplicate: self.name.set_text(source.name)
        self.action.set_selected(('allow', 'deny', 'reject').index(source.action) if source else 0)
        duration = source.duration if source else 'always'
        index = next((i for i, pair in enumerate(DURATIONS[:-1]) if pair[1] == duration), len(DURATIONS) - 1)
        self.duration.set_selected(index); self.custom_duration.set_text(duration)
        self.custom_duration.set_visible(index == len(DURATIONS) - 1)
        self.enabled.set_active(source.enabled if source else True)
        self.priority.set_active(source.precedence if source else False)
        self.logging.set_active(not source.nolog if source else True)
        self.description.get_buffer().set_text(source.description if source else '')
        for condition in editable_conditions(source.operator) if source else [dict(type='simple', operand='process.path', data='', sensitive=True, list=[])]:
            self.add_condition(condition)

    def add_condition(self, condition=None):
        item = ConditionEditor(condition or dict(type='simple', operand='process.path', data='', sensitive=True, list=[]), self.remove_condition)
        self.conditions.append(item); self.condition_group.add(item.row)
        self.update_remove_buttons()
        return item

    def remove_condition(self, item):
        if len(self.conditions) > 1:
            self.conditions.remove(item); self.condition_group.remove(item.row); self.update_remove_buttons()

    def update_remove_buttons(self):
        for item in self.conditions: item.remove_button.set_sensitive(len(self.conditions) > 1 and not self._pending)

    def draft(self):
        buffer = self.description.get_buffer()
        description = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        duration = DURATIONS[self.duration.get_selected()][1] or self.custom_duration.get_text()
        return draft_rule(self.name.get_text(), ('allow', 'deny', 'reject')[self.action.get_selected()], duration,
            self.enabled.get_active(), self.priority.get_active(), not self.logging.get_active(), description,
            [item.operator for item in self.conditions], self.expected or (replace(self._template, name=self.name.get_text(), created=int(time.time())) if self._template else None))

    def apply(self, *args):
        if self._pending: return
        try:
            proposed = self.draft()
        except ValueError as error:
            self.status.set_text(str(error)); return
        if self.expected and proposed.policy_fingerprint == self.expected.policy_fingerprint:
            self.status.set_text('No changes to save.'); return
        self._last_failure = ''
        self._pending = True
        self.page.set_sensitive(False); self.save.set_sensitive(False); self.reload.set_sensitive(False)
        self.status.set_text('Applying… Waiting for the daemon acknowledgment.')
        if not self.backend.session.request_rule(self.node, proposed, self.expected, callback=self.applied):
            self._pending = False; self.page.set_sensitive(True); self.reload.set_sensitive(True)
            self.status.set_text(self.backend.session.controller.last_error)
            self.source_changed(self.backend.snapshot)

    def applied(self, success, error):
        if self._closed: return
        self._pending = False
        if success:
            self.parent_window.show_message('Rule saved. Refreshing daemon snapshot.')
            self.close()
        else:
            self.page.set_sensitive(True); self.reload.set_sensitive(True)
            self._last_failure = error
            self.status.set_text(error + ' Reload to reconcile before retrying.')
            self.source_changed(self.backend.snapshot)

    def source_changed(self, snapshot):
        if self._closed or self._pending: return
        reason = self.backend.session.controller.rule_reason(self.node)
        current = self.backend.session.policies.rule_by_name(self.node, self.expected.name) if self.expected else None
        if self._reload_requested and not reason:
            self._reload_requested = False
            if current is None:
                self.status.set_text('The original rule is no longer available. Cancel or create a separate rule.'); self.save.set_sensitive(False); return
            unsupported = transport_edit_reason(current.operator)
            if unsupported:
                self.status.set_text(unsupported); self.save.set_sensitive(False); return
            self._last_failure = ''
            self.expected = rule_from_json(json.dumps(current.__dict__))
            self.load_source(self.expected)
            self.status.set_text('Reloaded the current daemon rule.')
        conflict = self.expected is not None and (current is None or current.policy_fingerprint != self.expected.policy_fingerprint)
        self.save.set_sensitive(not reason and not conflict)
        if conflict:
            self.status.set_text('This rule changed while the editor was open. Reload to review the current rule.')
        elif reason:
            self.status.set_text((self._last_failure + '\n' if self._last_failure else '') + reason)
        elif self._last_failure:
            self.status.set_text(self._last_failure + ' The refreshed daemon state is available for review.')

    def reload_rule(self, *args):
        if self._pending: return
        self._reload_requested = True
        self.status.set_text('Requesting a fresh daemon snapshot…')
        self.backend.session.refresh_policies()

    def closing(self, *args):
        if self._pending:
            self.status.set_text('Wait for the daemon acknowledgment or timeout before closing.'); return True
        if not self._closed:
            self._closed = True
            self._unsubscribe()
        return False
