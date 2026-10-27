"""Native rule inspection and actions over the shared policy controller."""
from dataclasses import replace
import csv
from datetime import datetime
import io
from .apps import Adw, Gtk, app_image, label
from .widgets import detail_row, details_header
from .table import RecordTable
from .export import save_text
from opensnitch.core.rule_edit import rule_from_json, transport_edit_reason

COLUMNS = (('Application', 230, 'executable'), ('Rule identifier', 280, 'name'), ('State', 95, 'enabled'),
    ('Action', 85, 'action'), ('Duration', 120, 'duration'), ('Priority', 85, 'precedence'),
    ('Logging', 95, 'nolog'), ('Conditions', 320, 'conditions'), ('Computer', 180, 'node'))
ACTIONS = ('', 'allow', 'deny', 'reject')


def created_label(timestamp):
    if not timestamp:
        return 'Not recorded'
    try:
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
    except (OverflowError, OSError, ValueError):
        return 'Epoch seconds: ' + str(timestamp)


class RulesView(RecordTable):
    def __init__(self, backend, window):
        self.backend, self.window = backend, window
        self._updating = False
        self._detail_state = object()
        self._snapshot = backend.snapshot
        self.node_ids = ['']
        self.editor = None
        self.focus_node = self.focus_executable = self.focus_name = ''
        self.widget = Adw.NavigationSplitView()
        self.widget.set_min_sidebar_width(580)
        self.widget.set_max_sidebar_width(1100)
        self.widget.set_sidebar_width_fraction(.70)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12)
        tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=18, margin_end=18)
        self.search = Gtk.SearchEntry(placeholder_text='Search apps, rule identifiers, conditions or descriptions')
        self.search.connect('search-changed', lambda *args: self.update())
        tools.append(self.search)
        filters = Gtk.Box(spacing=8)
        self.nodes = Gtk.DropDown.new_from_strings(['All computers'])
        self.nodes.set_tooltip_text('Rule source computer')
        self.enabled = Gtk.DropDown.new_from_strings(['All rules', 'Enabled', 'Disabled'])
        self.enabled.set_tooltip_text('Configured enabled state')
        self.action = Gtk.DropDown.new_from_strings(['All actions', 'Allow', 'Block', 'Reject'])
        self.action.set_tooltip_text('Configured rule action')
        self.duration = Gtk.DropDown.new_from_strings(['All lifetimes', 'Until changed', 'Temporary'])
        self.duration.set_tooltip_text('Configured rule duration')
        for control in (self.nodes, self.enabled, self.action, self.duration):
            control.connect('notify::selected', lambda *args: self.update())
            filters.append(control)
        tools.append(filters)
        actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5,
            min_children_per_line=1, column_spacing=6, row_spacing=6, homogeneous=False)
        self.action_buttons = {}
        for title, name, callback in (('New rule', 'new', self.new_rule), ('Edit', 'edit', self.edit_rule),
                ('Duplicate', 'duplicate', self.duplicate_rule), ('Disable', 'toggle', self.toggle_rule), ('Delete', 'delete', self.confirm_delete)):
            button = Gtk.Button(label=title)
            button.connect('clicked', callback)
            self.action_buttons[name] = button
            actions.append(button)
        tools.append(actions)
        self.focus = Gtk.Box(spacing=8)
        self.focus_label = label('', 'caption', xalign=0, hexpand=True, ellipsize=3)
        self.focus.append(self.focus_label)
        clear_focus = Gtk.Button(label='Show all rules')
        clear_focus.connect('clicked', self.clear_focus)
        self.focus.append(clear_focus)
        self.focus.set_visible(False)
        tools.append(self.focus)
        heading = Gtk.Box(spacing=8)
        self.count = label('Rules', 'title-2', xalign=0, hexpand=True)
        heading.append(self.count)
        self.copy = Gtk.Button(label='Copy rule JSON', sensitive=False)
        self.copy.connect('clicked', self.copy_rule)
        heading.append(self.copy)
        export = Gtk.Button(label='Export list…')
        export.set_tooltip_text('Export the filtered rule list as CSV with raw identifiers and conditions')
        export.connect('clicked', lambda *args: save_text(window, 'opensnitch-rule-list.csv', self.csv_text()))
        heading.append(export)
        self.column_menu = Gtk.MenuButton(icon_name='view-list-symbolic')
        self.column_menu.set_tooltip_text('Visible columns')
        heading.append(self.column_menu)
        tools.append(heading)
        side.append(tools)
        self.initialize_table(COLUMNS, 'rules', lambda record, field: getattr(record, field), 'name')
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.scroll, 'table')
        self.empty = Adw.StatusPage(title='Waiting for rules', icon_name='view-list-symbolic', description='Rules are read from the daemon subscription.')
        self.stack.add_named(self.empty, 'empty')
        side.append(self.stack)
        self.note = label('', 'dim-label', 'caption', wrap=True, margin_start=18, margin_end=18)
        side.append(self.note)
        self.widget.set_sidebar(Adw.NavigationPage.new(side, 'Rules'))
        detail = Adw.ToolbarView()
        detail.add_top_bar(details_header('Rule details'))
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
            margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        self.toasts = Adw.ToastOverlay(child=Gtk.ScrolledWindow(child=self.details, hscrollbar_policy=Gtk.PolicyType.NEVER))
        detail.set_content(self.toasts)
        self.widget.set_content(Adw.NavigationPage.new(detail, 'Rule details'))
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 1000sp'))
        breakpoint.add_setter(self.widget, 'collapsed', True)
        window.add_breakpoint(breakpoint)
        self.restore_layout()
        backend.subscribe(self.apply_snapshot)

    def application_label(self, record, identity):
        return identity.display_name if record.executable else record.application_label

    def application_tooltip(self, record):
        return record.executable or record.application_label

    def display_value(self, record, field):
        if field == 'enabled':
            if self.backend.session.controller.rule_pending(record.node, record.name):
                return 'Applying…'
            return 'Enabled' if record.enabled else 'Disabled'
        if field == 'action':
            return {'allow': 'Allow', 'deny': 'Block', 'reject': 'Reject'}.get(record.action, record.action)
        if field == 'duration':
            return {'always': 'Until changed', 'until restart': 'Until reboot', 'once': 'Once'}.get(record.duration, record.duration)
        if field == 'precedence':
            return 'Priority' if record.precedence else 'Normal'
        if field == 'nolog':
            return 'Off' if record.nolog else 'On'
        return getattr(record, field)

    def apply_snapshot(self, snapshot):
        self._snapshot = snapshot
        self.update()

    def update(self):
        if self._updating:
            return
        self._updating = True
        selected_node = self.node_ids[self.nodes.get_selected()]
        nodes = [''] + sorted({record.node for record in self._snapshot.rules})
        if nodes != self.node_ids:
            self.node_ids = nodes
            self.nodes.set_model(Gtk.StringList.new(['All computers'] + ['This computer' if node == 'unix:/local' else node for node in nodes[1:]]))
            self.nodes.set_selected(nodes.index(selected_node) if selected_node in nodes else 0)
        self.nodes.set_visible(len(nodes) > 2)
        node = self.node_ids[self.nodes.get_selected()]
        query = self.search.get_text().strip().casefold()
        related_names = {rule.name for rule in self.backend.session.policies.application_rules(self.focus_node, self.focus_executable)} if self.focus_executable else set()
        def matches(record, identity):
            if node and record.node != node:
                return False
            if self.enabled.get_selected() and record.enabled != (self.enabled.get_selected() == 1):
                return False
            if self.action.get_selected() and record.action != ACTIONS[self.action.get_selected()]:
                return False
            if self.duration.get_selected() and (record.duration == 'always') != (self.duration.get_selected() == 1):
                return False
            if self.focus_name and (record.node != self.focus_node or record.name != self.focus_name):
                return False
            if self.focus_executable:
                # The policy service supplies related pattern/compound rules too;
                # this filter does not invent a friendly identity for them.
                if record.node != self.focus_node or record.name not in related_names:
                    return False
            return not query or query in '\n'.join((record.name, record.executable, self.application_label(record, identity),
                record.description, record.conditions, record.node)).casefold()
        count = self.replace_rows(self._snapshot.rules, self.backend.session.identities.resolve, matches)
        self._updating = False
        self.count.set_text('Rules · ' + str(count))
        self.stack.set_visible_child_name('table' if count else 'empty')
        self.empty.set_title('No matching rules' if self._snapshot.rules else 'No rule snapshot')
        self.empty.set_description('This identifier is not in the current rule snapshot. It may have expired or been removed.' if self.focus_name and not count else 'Try another filter.' if self._snapshot.rules else self._snapshot.message)
        self.focus.set_visible(bool(self.focus_executable or self.focus_name))
        self.focus_label.set_text(self.focus_executable or self.focus_name)
        self.focus_label.set_tooltip_text(self.focus_executable or self.focus_name)
        self.note.set_text('Rule changes are confirmed by the daemon. ' +
            ('Some snapshots are cached or unverified; inspect the selected rule’s source status.' if any(item.record.status != 'Current daemon snapshot' for item in self.visible_items) else 'Source: current daemon subscription.'))
        self.update_actions()
        self.show_details()

    def update_actions(self):
        obj = self.selection.get_selected_item()
        node = obj.record.node if obj else self.node_ids[self.nodes.get_selected()] or 'unix:/local'
        reason = self.backend.session.controller.rule_reason(node)
        self.action_buttons['new'].set_sensitive(not reason)
        self.action_buttons['new'].set_tooltip_text(reason or 'Create a separate rule')
        unsupported = transport_edit_reason(rule_from_json(obj.record.json).operator) if obj else 'Select a rule.'
        if obj and obj.record.action not in ('allow', 'deny', 'reject'):
            unsupported = 'Unknown rule action; inspect or delete this rule using its identifier.'
        for name in ('edit', 'duplicate', 'toggle', 'delete'):
            blocked = reason or (unsupported if name != 'delete' else '')
            self.action_buttons[name].set_sensitive(bool(obj) and not blocked)
            self.action_buttons[name].set_tooltip_text(blocked or 'Use the selected raw rule identifier')
        if obj:
            self.action_buttons['toggle'].set_label('Disable' if obj.record.enabled else 'Enable')

    def show_details(self):
        self.update_actions()
        obj = self.selection.get_selected_item()
        self.copy.set_sensitive(obj is not None)
        state = (obj.record, obj.identity) if obj else None
        if state == self._detail_state:
            return
        self._detail_state = state
        child = self.details.get_first_child()
        while child:
            following = child.get_next_sibling(); self.details.remove(child); child = following
        if not obj:
            self.details.append(Adw.StatusPage(title='Select a rule', icon_name='view-list-symbolic'))
            return
        record, identity = obj.record, obj.identity
        self.details.append(app_image(identity, 64))
        self.details.append(label(self.application_label(record, identity), 'title-1', wrap=True, justify=Gtk.Justification.CENTER))
        self.details.append(label(record.name, 'dim-label', wrap=True, selectable=True, justify=Gtk.Justification.CENTER))
        group = Adw.PreferencesGroup(title='Configured rule')
        for title, value in (('Identifier', record.name), ('Computer', 'This computer' if record.node == 'unix:/local' else record.node),
                ('Snapshot status', record.status), ('State', self.display_value(record, 'enabled')), ('Action', self.display_value(record, 'action')),
                ('Duration', self.display_value(record, 'duration')), ('Priority', self.display_value(record, 'precedence')),
                ('Logging', self.display_value(record, 'nolog')), ('Created', created_label(record.created)),
                ('Description', record.description or 'None')):
            detail_row(group, title, value)
        if record.executable:
            detail_row(group, 'Exact executable', record.executable)
        unsupported = transport_edit_reason(rule_from_json(record.json).operator)
        if unsupported:
            detail_row(group, 'Editing unavailable', unsupported)
        self.details.append(group)
        conditions = Adw.PreferencesGroup(title='Conditions')
        self.condition_row(conditions, record.condition)
        self.details.append(conditions)
        raw = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title='Raw rule JSON', subtitle='Read-only daemon fields', use_markup=False)
        text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, left_margin=8, right_margin=8, top_margin=8, bottom_margin=8)
        text.get_buffer().set_text(record.json)
        expander.add_row(Gtk.ScrolledWindow(child=text, min_content_height=240, max_content_height=360))
        raw.add(expander); self.details.append(raw)
        copy = Gtk.Button(label='Copy rule JSON')
        copy.connect('clicked', self.copy_rule)
        self.details.append(copy)
        if record.executable:
            app = Gtk.Button(label='Show in Apps')
            app.connect('clicked', lambda *args: self.window.open_application(record.node, record.executable))
            self.details.append(app)
        activity = Gtk.Button(label='View recorded activity')
        activity.connect('clicked', lambda *args: self.open_activity(record))
        self.details.append(activity)

    def condition_row(self, parent, condition):
        title = 'All conditions must match' if condition.kind == 'list' else condition.operand or 'Condition'
        subtitle = condition.data if condition.kind != 'list' else '{} conditions'.format(len(condition.children))
        row = Adw.ExpanderRow(title=title, subtitle=subtitle or '(empty value)', use_markup=False,
            expanded=condition.kind == 'list')
        parent.add_row(row) if isinstance(parent, Adw.ExpanderRow) else parent.add(row)
        detail_row(row, 'Operator type', condition.kind or 'Not specified')
        if condition.kind != 'list':
            detail_row(row, 'Operand', condition.operand or 'Not specified')
            detail_row(row, 'Value', condition.data)
            detail_row(row, 'Case-sensitive', 'Yes' if condition.sensitive else 'No')
        elif condition.operand or condition.data:
            detail_row(row, 'Operand', condition.operand)
            detail_row(row, 'Data', condition.data)
        for child in condition.children:
            self.condition_row(row, child)

    def copy_rule(self, *args):
        obj = self.selection.get_selected_item()
        if obj:
            self.window.get_clipboard().set(obj.record.json)

    def csv_text(self):
        output = io.StringIO(); writer = csv.writer(output)
        writer.writerow(('Computer', 'Rule identifier', 'Exact executable', 'Enabled', 'Action', 'Duration', 'Priority', 'Logging disabled', 'Created', 'Description', 'Rule JSON'))
        for index in range(self.sorted.get_n_items()):
            record = self.sorted.get_item(index).record
            writer.writerow((record.node, record.name, record.executable, record.enabled, record.action, record.duration,
                record.precedence, record.nolog, record.created, record.description, record.json))
        return output.getvalue()

    def focus_application(self, node, executable):
        self._focus(node, executable=executable)

    def focus_identifier(self, node, name):
        self._focus(node, name=name)

    def _focus(self, node, executable='', name=''):
        self.focus_node, self.focus_executable, self.focus_name = node, executable, name
        self._updating = True
        self.search.set_text(''); self.enabled.set_selected(0); self.action.set_selected(0); self.duration.set_selected(0)
        self.nodes.set_selected(0)
        self._updating = False
        self.update()

    def clear_focus(self, *args):
        self.focus_node = self.focus_executable = self.focus_name = ''
        self.update()

    def open_activity(self, record):
        if hasattr(self.window, 'activity'):
            self.window.activity.focus_rule(record.node, record.name)
            self.window.pages.set_visible_child_name('activity')

    def selected_record(self):
        obj = self.selection.get_selected_item()
        return obj.record if obj else None

    def new_rule(self, *args):
        node = self.node_ids[self.nodes.get_selected()] or 'unix:/local'
        self.open_editor(node)

    def edit_rule(self, *args):
        record = self.selected_record()
        if record: self.open_editor(record.node, record)

    def duplicate_rule(self, *args):
        record = self.selected_record()
        if record: self.open_editor(record.node, record, duplicate=True)

    def open_editor(self, node, record=None, duplicate=False):
        from .rule_editor import RuleEditor
        reason = self.backend.session.controller.rule_reason(node)
        if record:
            reason = reason or transport_edit_reason(rule_from_json(record.json).operator)
            if record.action not in ('allow', 'deny', 'reject'):
                reason = 'Unknown rule action; this rule is preserved read-only.'
        if reason:
            self.window.show_message(reason); return
        if self.editor and not self.editor._closed:
            self.editor.present(); return
        self.editor = RuleEditor(self.backend, self.window, node, record, duplicate)
        self.editor.present()

    def toggle_rule(self, *args):
        record = self.selected_record()
        if not record: return
        expected = rule_from_json(record.json)
        proposed = replace(expected, enabled=not expected.enabled)
        operation = 'enable' if proposed.enabled else 'disable'
        self.apply_rule_operation(record.node, proposed, expected, operation)

    def confirm_delete(self, *args):
        record = self.selected_record()
        if not record: return
        expected = rule_from_json(record.json)
        dialog = Adw.AlertDialog(heading='Delete this rule?', body=record.name + '\n' + record.node)
        dialog.add_response('cancel', 'Cancel'); dialog.add_response('delete', 'Delete')
        dialog.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel'); dialog.set_close_response('cancel')
        dialog.connect('response', lambda dialog, response: self.apply_rule_operation(record.node, expected, expected, 'delete') if response == 'delete' else None)
        dialog.present(self.window)

    def apply_rule_operation(self, node, proposed, expected, operation):
        def completed(success, error):
            self.window.show_message('Rule updated. Refreshing daemon snapshot.' if success else error)
        if not self.backend.session.request_rule(node, proposed, expected, operation, completed):
            self.window.show_message(self.backend.session.controller.last_error)
