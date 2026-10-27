"""Recorded firewall decisions; all database work stays on the shared worker."""
import csv
import io
from datetime import datetime, timedelta
from .apps import Adw, GLib, Gtk, app_image, label
from .widgets import detail_row, details_header
from .table import RecordTable
from opensnitch.core.activity import FIELDS
from opensnitch.core.sockets import endpoint

COLUMNS = (('Time', 200, 'time'), ('Application', 220, 'executable'), ('Decision', 90, 'action'),
    ('Remote endpoint', 185, 'remote'), ('Hostname', 190, 'host'), ('Protocol', 85, 'protocol'),
    ('Rule', 220, 'rule'), ('PID', 80, 'pid'), ('Local endpoint', 185, 'local'), ('User ID', 80, 'uid'),
    ('Arguments', 260, 'arguments'), ('Working directory', 240, 'cwd'), ('Computer', 180, 'node'))
ACTIONS = ('', 'allow', 'deny', 'reject')
PROTOCOLS = ('', 'tcp', 'udp', 'icmp', 'icmpv6', 'sctp', 'dccp', 'igmp')


def value(record, field):
    if field == 'remote':
        return endpoint(record.destination, record.destination_port)
    if field == 'local':
        return endpoint(record.source, record.source_port)
    return getattr(record, field)


class ActivityView(RecordTable):
    def __init__(self, backend, window):
        self.backend, self.window = backend, window
        self.browser = backend.session.activity
        self._updating = self._ready = False
        self._detail_state = object()
        self._search_timer = 0
        self.node_ids = ['']
        self.widget = Adw.NavigationSplitView()
        self.widget.set_min_sidebar_width(580)
        self.widget.set_max_sidebar_width(1100)
        self.widget.set_sidebar_width_fraction(.70)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12)
        tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=18, margin_end=18)
        self.search = Gtk.SearchEntry(placeholder_text='Search names, paths, addresses, hostnames or rules')
        self.search.connect('notify::text', self.search_changed)
        tools.append(self.search)
        filters = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, min_children_per_line=1,
            column_spacing=8, row_spacing=8, homogeneous=False)
        self.nodes = Gtk.DropDown.new_from_strings(['All computers'])
        self.nodes.set_tooltip_text('Filter recorded activity by computer')
        self.action = Gtk.DropDown.new_from_strings(['All decisions', 'Allowed', 'Blocked', 'Rejected'])
        self.action.set_tooltip_text('Recorded decision')
        self.protocol = Gtk.DropDown.new_from_strings(['All protocols'] + [value.upper() for value in PROTOCOLS[1:]])
        self.protocol.set_tooltip_text('Recorded protocol')
        self.period = Gtk.DropDown.new_from_strings(['All time', 'Last hour', 'Today'])
        self.period.set_tooltip_text('Time range')
        for control in (self.nodes, self.action, self.protocol, self.period):
            control.connect('notify::selected', self.configure)
            filters.append(control)
        self.live = Gtk.ToggleButton(label='Live', active=True)
        self.live.set_tooltip_text('Follow new decisions; scrolling into older entries pauses updates')
        self.live.connect('toggled', self.toggle_live)
        filters.append(self.live)
        tools.append(filters)
        self.focus = Gtk.Box(spacing=8)
        self.focus_label = label('', 'caption', ellipsize=3, xalign=0, hexpand=True)
        self.focus.append(self.focus_label)
        clear_focus = Gtk.Button(label='Clear app/rule filter')
        clear_focus.connect('clicked', lambda *args: self.clear_focus())
        self.focus.append(clear_focus)
        self.focus.set_visible(False)
        tools.append(self.focus)
        heading = Gtk.Box(spacing=8)
        self.count = label('Activity', 'title-2', xalign=0, hexpand=True)
        heading.append(self.count)
        self.copy_button = Gtk.Button(label='Copy', sensitive=False)
        self.copy_button.connect('clicked', self.copy_selected)
        heading.append(self.copy_button)
        self.export_button = Gtk.Button(label='Export…')
        self.export_button.set_tooltip_text('Export all matching recorded decisions, including other pages')
        self.export_button.connect('clicked', self.export)
        heading.append(self.export_button)
        clear = Gtk.Button(label='Clear history…')
        clear.connect('clicked', self.confirm_clear)
        heading.append(clear)
        self.column_menu = Gtk.MenuButton(icon_name='view-list-symbolic')
        self.column_menu.set_tooltip_text('Visible columns')
        heading.append(self.column_menu)
        tools.append(heading)
        side.append(tools)
        self.initialize_table(COLUMNS, 'activity', value, 'time', Gtk.SortType.DESCENDING)
        self.scroll.get_vadjustment().connect('value-changed', self.browsing)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.scroll, 'table')
        self.empty = Adw.StatusPage(title='No recorded activity', description='Decisions sent by the daemon appear here.', icon_name='document-open-recent-symbolic')
        self.stack.add_named(self.empty, 'empty')
        side.append(self.stack)
        pages = Gtk.Box(spacing=8, margin_start=18, margin_end=18)
        self.previous = Gtk.Button(label='Previous')
        self.previous.connect('clicked', lambda *args: self.browser.page(-1))
        pages.append(self.previous)
        self.page_label = label('', 'caption', hexpand=True)
        pages.append(self.page_label)
        self.next = Gtk.Button(label='Next')
        self.next.connect('clicked', lambda *args: self.browser.page(1))
        pages.append(self.next)
        self.limit = Gtk.DropDown.new_from_strings(['100 rows', '250 rows', '500 rows'])
        self.limit.set_selected(1)
        self.limit.set_tooltip_text('Rows per history page')
        self.limit.connect('notify::selected', self.configure)
        pages.append(self.limit)
        side.append(pages)
        self.note = label('', 'dim-label', 'caption', wrap=True, margin_start=18, margin_end=18)
        side.append(self.note)
        self.widget.set_sidebar(Adw.NavigationPage.new(side, 'Activity'))
        detail = Adw.ToolbarView()
        detail.add_top_bar(details_header('Decision details'))
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
            margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        self.toasts = Adw.ToastOverlay(child=Gtk.ScrolledWindow(child=self.details, hscrollbar_policy=Gtk.PolicyType.NEVER))
        detail.set_content(self.toasts)
        self.widget.set_content(Adw.NavigationPage.new(detail, 'Decision details'))
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 1000sp'))
        breakpoint.add_setter(self.widget, 'collapsed', True)
        window.add_breakpoint(breakpoint)
        self._updating = True
        self.restore_layout()
        self._updating = False
        self._ready = True
        self.browser.changed.connect(self.apply_page)
        backend.subscribe(lambda snapshot: self.update_identities())
        self.apply_page(self.browser.snapshot)
        self.configure()

    def search_changed(self, *args):
        if self._updating:
            return
        if self._search_timer:
            GLib.source_remove(self._search_timer)
        def run():
            self._search_timer = 0
            self.configure()
            return False
        self._search_timer = GLib.timeout_add(250, run)

    def set_active(self, active):
        self.browser.set_active(active)

    def display_value(self, record, field):
        if field == 'action':
            return {'allow': 'Allowed', 'deny': 'Blocked', 'reject': 'Rejected'}.get(record.action, record.action)
        return value(record, field)

    def _sort_changed(self):
        was_updating = self._updating
        super()._sort_changed()
        if self._ready and not was_updating:
            self.configure()

    def configure(self, *args):
        if self._updating or not self._ready:
            return
        sorter = self.table.get_sorter()
        field = next((field for field, column in self.columns.items() if column is sorter.get_primary_sort_column()), 'time')
        now = datetime.now()
        since = '' if self.period.get_selected() == 0 else (now - timedelta(hours=1) if self.period.get_selected() == 1 else now.replace(hour=0, minute=0, second=0, microsecond=0)).strftime('%Y-%m-%d %H:%M:%S')
        self.browser.configure(text=self.search.get_text(), node=self.node_ids[self.nodes.get_selected()],
            action=ACTIONS[self.action.get_selected()], protocol=PROTOCOLS[self.protocol.get_selected()], since=since,
            limit=(100, 250, 500)[self.limit.get_selected()], sort=field,
            descending=sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING)

    def toggle_live(self, *args):
        if not self._updating:
            self.browser.set_live(self.live.get_active())

    def browsing(self, adjustment):
        if self.browser.active and self.live.get_active() and adjustment.get_value() > 0 and not self._updating and not self._restoring_scroll:
            self.live.set_active(False)

    def update_identities(self):
        if self._ready:
            self.apply_page(self.browser.snapshot)

    def apply_page(self, page):
        if self._updating:
            return
        self._updating = True
        selected_node = self.browser.query.node
        nodes = [''] + sorted(set(self.backend.session.nodes) | set(page.nodes))
        if nodes != self.node_ids:
            self.node_ids = nodes
            self.nodes.set_model(Gtk.StringList.new(['All computers'] + ['This computer' if node == 'unix:/local' else node for node in nodes[1:]]))
        self.nodes.set_selected(nodes.index(selected_node) if selected_node in nodes else 0)
        self.nodes.set_visible(len(nodes) > 2)
        self.live.set_active(self.browser.live)
        self.live.set_label('Live' if self.browser.live else 'Paused')
        self.focus.set_visible(bool(self.browser.query.executable or self.browser.query.rule))
        self.focus_label.set_text(self.browser.query.executable or self.browser.query.rule)
        self.focus_label.set_tooltip_text(self.browser.query.executable or self.browser.query.rule)
        self.replace_rows(page.rows, self.backend.session.identities.resolve)
        self._updating = False
        self.count.set_text('Activity · ' + str(page.total))
        self.stack.set_visible_child_name('table' if page.rows else 'empty')
        self.empty.set_title('History unavailable' if page.error else 'No matching activity' if page.total == 0 and any((self.browser.query.text, self.browser.query.executable, self.browser.query.action)) else 'No recorded activity')
        self.empty.set_description(page.error or 'Decisions sent by the daemon appear here. Clearing activity keeps the application inventory and rules.')
        self.previous.set_sensitive(page.offset > 0)
        self.next.set_sensitive(page.offset + len(page.rows) < page.total)
        self.export_button.set_sensitive(page.total > 0)
        self.page_label.set_text('{}–{} of {}'.format(page.offset + 1 if page.rows else 0, page.offset + len(page.rows), page.total))
        self.note.set_text(page.error or page.message + ' · Recorded decisions do not describe current permissions or live sockets.')
        self.show_details()

    def show_details(self):
        obj = self.selection.get_selected_item()
        self.copy_button.set_sensitive(obj is not None)
        state = (obj.record, obj.identity) if obj else None
        if state == self._detail_state:
            return
        self._detail_state = state
        child = self.details.get_first_child()
        while child:
            following = child.get_next_sibling(); self.details.remove(child); child = following
        if not obj:
            self.details.append(Adw.StatusPage(title='Select a recorded decision', icon_name='document-open-recent-symbolic'))
            return
        r, identity = obj.record, obj.identity
        self.details.append(app_image(identity, 64))
        self.details.append(label(identity.display_name, 'title-1', wrap=True, justify=Gtk.Justification.CENTER))
        decision = Adw.PreferencesGroup(title='Recorded decision', description='This rule may have changed since the event was recorded.')
        for title, text in (('Time', r.time), ('Decision', self.display_value(r, 'action')), ('Rule identifier', r.rule or 'Not recorded'),
                ('Computer', 'This computer' if r.node == 'unix:/local' else r.node), ('Protocol', r.protocol),
                ('Remote endpoint', endpoint(r.destination, r.destination_port)), ('Hostname', r.host or 'Not recorded'),
                ('Local endpoint', endpoint(r.source, r.source_port))):
            detail_row(decision, title, text)
        self.details.append(decision)
        process = Adw.PreferencesGroup(title='Process at the time of the event')
        for title, text in (('Executable', r.executable or 'Unknown'), ('PID', r.pid or 'Unknown'), ('User ID', r.uid),
                ('Arguments', r.arguments or 'Not recorded'), ('Working directory', r.cwd or 'Not recorded')):
            detail_row(process, title, text)
        self.details.append(process)
        app = Gtk.Button(label='Show in Apps', sensitive=bool(r.executable))
        app.connect('clicked', lambda *args: self.window.open_application(r.node, r.executable))
        self.details.append(app)
        if r.rule:
            rule = Gtk.Button(label='View rule')
            rule.connect('clicked', lambda *args: self.window.open_rule(r.node, r.rule))
            self.details.append(rule)
        copy = Gtk.Button(label='Copy executable path', sensitive=bool(r.executable))
        copy.connect('clicked', lambda *args: self.window.get_clipboard().set(r.executable))
        self.details.append(copy)

    def focus_application(self, node, executable):
        self._focus(node, executable=executable)

    def focus_rule(self, node, name):
        self._focus(node, rule=name)

    def _focus(self, node, executable='', rule=''):
        if self._search_timer:
            GLib.source_remove(self._search_timer); self._search_timer = 0
        self._updating = True
        self.search.set_text('')
        self.action.set_selected(0); self.protocol.set_selected(0); self.period.set_selected(0)
        self._updating = False
        self.browser.configure(node=node, executable=executable, text='', action='', protocol='', rule=rule, since='')

    def clear_focus(self):
        self.browser.configure(executable='', rule='')

    def copy_selected(self, *args):
        obj = self.selection.get_selected_item()
        if obj:
            r = obj.record
            output = io.StringIO(); writer = csv.writer(output); writer.writerow(FIELDS)
            writer.writerow((r.time, r.node, r.action, r.protocol, r.source, r.source_port, r.destination, r.host,
                r.destination_port, r.uid, r.pid, r.executable, r.arguments, r.cwd, r.rule))
            self.window.get_clipboard().set(output.getvalue())

    def export(self, *args):
        from .export import choose_file
        def chosen(file):
            path = file.get_path()
            if not path:
                self.window.show_message('Choose a local destination for the history export.')
                return
            self.browser.export(path, lambda count, error: self.window.show_message('Export failed: ' + error if error else 'Exported {} recorded decisions.'.format(count)))
        choose_file(self.window, 'opensnitch-activity.csv', chosen)

    def confirm_clear(self, *args):
        dialog = Adw.AlertDialog(heading='Clear recorded activity?',
            body='Remove the currently recorded history for all computers. Application entries, rules and new events are kept.')
        dialog.add_response('cancel', 'Cancel'); dialog.add_response('clear', 'Clear history')
        dialog.set_response_appearance('clear', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel'); dialog.set_close_response('cancel')
        dialog.connect('response', lambda dialog, response: self.clear_history() if response == 'clear' else None)
        dialog.present(self.window)

    def clear_history(self):
        def cleared(count, error):
            if not error:
                self.selected_key = None
            self.window.show_message('Could not clear activity: ' + error if error else 'Cleared {} recorded decisions.'.format(count))
        self.browser.clear(cleared)
