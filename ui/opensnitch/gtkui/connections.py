"""Native socket table. Presentation never changes raw operation identifiers."""
import csv
import io
from datetime import datetime
from .apps import Adw, Gtk, app_image, label
from .widgets import detail_row, details_header
from .table import RecordTable
from .export import save_text
from opensnitch.core.sockets import endpoint

PROTOCOLS = (('All protocols', 0), ('TCP', 6), ('UDP', 17), ('SCTP', 132), ('DCCP', 33), ('ICMP', 1), ('ICMPv6', 58), ('IGMP', 2), ('RAW', 255))
FAMILIES = (('All families', 0), ('IPv4', 2), ('IPv6', 10), ('Packet', 17), ('XDP', 44))
STATES = (('All states', 0), ('Established', 1), ('SYN sent', 2), ('SYN received', 3), ('FIN wait 1', 4), ('FIN wait 2', 5), ('Time wait', 6), ('Closed', 7), ('Close wait', 8), ('Last ack', 9), ('Listening', 10), ('Closing', 11), ('New SYN received', 12))
COLUMNS = (('Application', 220, 'executable'), ('Remote endpoint', 190, 'remote'), ('State', 130, 'state_name'),
    ('Protocol', 85, 'protocol_name'), ('PID', 65, 'pid'), ('Local endpoint', 190, 'local'), ('User ID', 80, 'uid'),
    ('Family', 100, 'family_name'), ('Interface', 90, 'interface'), ('Inode', 110, 'inode'),
    ('Receive queue', 115, 'receive_queue'), ('Send queue', 105, 'send_queue'))


def value(record, field):
    if field == 'remote':
        return endpoint(record.destination, record.destination_port)
    if field == 'local':
        return endpoint(record.source, record.source_port)
    return getattr(record, field)


class ConnectionsView(RecordTable):
    def __init__(self, backend, window):
        self.backend, self.window = backend, window
        self.monitor = backend.session.sockets
        self.widget = Adw.NavigationSplitView()
        self.widget.set_min_sidebar_width(580)
        self.widget.set_max_sidebar_width(1100)
        self.widget.set_sidebar_width_fraction(.70)
        self._updating = False
        self._objects = {}
        self.selected_key = None
        self._detail_state = object()
        self.node_ids = ['unix:/local']
        self.visible_items = ()
        self.columns = {}
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=12, margin_bottom=12)
        tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=18, margin_end=18)
        self.search = Gtk.SearchEntry(placeholder_text='Search applications, paths, addresses or process IDs')
        self.search.connect('search-changed', lambda *args: self.update())
        tools.append(self.search)
        filters = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, min_children_per_line=1,
            column_spacing=8, row_spacing=8, homogeneous=False)
        self.nodes = Gtk.DropDown.new_from_strings(['This computer'])
        self.nodes.set_tooltip_text('Computer whose sockets are shown')
        self.nodes.connect('notify::selected', self.configure)
        filters.append(self.nodes)
        self.interval = Gtk.DropDown.new_from_strings(['Paused', 'Every second', 'Every 5 seconds', 'Every 10 seconds', 'Every 30 seconds'])
        self.interval.set_selected(2)
        self.interval.set_tooltip_text('Socket refresh interval')
        self.interval.connect('notify::selected', self.configure)
        filters.append(self.interval)
        for name, choices, hint in (('protocol', PROTOCOLS, 'Protocol filter'), ('family', FAMILIES, 'Address family filter'), ('state', STATES, 'Socket state filter')):
            control = Gtk.DropDown.new_from_strings([text for text, number in choices])
            control.set_tooltip_text(hint)
            control.connect('notify::selected', self.configure)
            setattr(self, name, control)
            filters.append(control)
        tools.append(filters)
        heading = Gtk.Box(spacing=8)
        self.count = label('Connections', 'title-2', xalign=0, hexpand=True)
        heading.append(self.count)
        self.copy_button = Gtk.Button(label='Copy')
        self.copy_button.set_tooltip_text('Copy selected connection with its raw executable path')
        self.copy_button.connect('clicked', self.copy_selected)
        heading.append(self.copy_button)
        export = Gtk.Button(label='Export…')
        export.set_tooltip_text('Export the filtered socket snapshot as CSV')
        export.connect('clicked', lambda *args: save_text(window, 'opensnitch-connections.csv', self.csv_text()))
        heading.append(export)
        self.column_menu = Gtk.MenuButton(icon_name='view-list-symbolic')
        self.column_menu.set_tooltip_text('Visible columns')
        heading.append(self.column_menu)
        tools.append(heading)
        side.append(tools)
        self.initialize_table(COLUMNS, 'connections', value)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.scroll, 'table')
        self.empty = Adw.StatusPage(title='No socket snapshot', icon_name='network-workgroup-symbolic')
        self.stack.add_named(self.empty, 'empty')
        side.append(self.stack)
        self.note = label('', 'dim-label', 'caption', wrap=True, margin_start=18, margin_end=18)
        side.append(self.note)
        self.widget.set_sidebar(Adw.NavigationPage.new(side, 'Connections'))
        detail = Adw.ToolbarView()
        header = details_header('Connection details')
        detail.add_top_bar(header)
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
            margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        self.toasts = Adw.ToastOverlay(child=Gtk.ScrolledWindow(child=self.details, hscrollbar_policy=Gtk.PolicyType.NEVER))
        detail.set_content(self.toasts)
        self.widget.set_content(Adw.NavigationPage.new(detail, 'Connection details'))
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 1000sp'))
        breakpoint.add_setter(self.widget, 'collapsed', True)
        window.add_breakpoint(breakpoint)
        self.restore_layout()
        self.monitor.changed.connect(lambda snapshot: self.update())
        backend.subscribe(lambda snapshot: self.update())
        self.update()

    def configure(self, *args):
        if self._updating:
            return
        selected = self.nodes.get_selected()
        self.monitor.configure(node=self.node_ids[selected] if selected < len(self.node_ids) else 'unix:/local',
            interval=self.monitor.INTERVALS[self.interval.get_selected()],
            protocol=PROTOCOLS[self.protocol.get_selected()][1], family=FAMILIES[self.family.get_selected()][1],
            state=STATES[self.state.get_selected()][1])

    def set_active(self, active):
        self.monitor.configure(active=active)

    def update(self):
        if self._updating:
            return
        self._updating = True
        nodes = ['unix:/local'] + sorted(node for node in self.backend.session.nodes if node != 'unix:/local')
        if nodes != self.node_ids:
            self.node_ids = nodes
            self.nodes.set_model(Gtk.StringList.new(['This computer'] + nodes[1:]))
            self.nodes.set_selected(nodes.index(self.monitor.node) if self.monitor.node in nodes else 0)
        self.nodes.set_visible(len(nodes) > 1)
        snapshot = self.monitor.snapshot
        query = self.search.get_text().casefold().strip()
        def matches(record, identity):
            haystack = '\n'.join((identity.display_name, record.executable, record.command, record.source, record.destination,
                str(record.pid), str(record.uid), record.protocol_name, record.state_name, str(record.source_port), str(record.destination_port))).casefold()
            return not query or query in haystack
        count = self.replace_rows(snapshot.rows, self.backend.session.identities.resolve, matches)
        self._updating = False
        self.count.set_text('Connections · ' + str(count))
        self.stack.set_visible_child_name('table' if count else 'empty')
        self.empty.set_title('No matching connections' if snapshot.rows else 'No socket snapshot' if not snapshot.timestamp else 'No sockets found')
        self.empty.set_description(snapshot.message)
        when = datetime.fromtimestamp(snapshot.timestamp).strftime('%H:%M:%S') if snapshot.timestamp else ''
        self.note.set_text(snapshot.message + (' · Last received ' + when if when else '') + '\nSocket state describes activity, not application permission.')
        self.show_details()

    def show_details(self):
        obj = self.selection.get_selected_item()
        self.copy_button.set_sensitive(obj is not None)
        state = (obj.record, obj.identity, self.monitor.snapshot.status) if obj else None
        if state == self._detail_state:
            return
        self._detail_state = state
        child = self.details.get_first_child()
        while child:
            following = child.get_next_sibling(); self.details.remove(child); child = following
        if not obj:
            self.details.append(Adw.StatusPage(title='Select a connection', icon_name='network-workgroup-symbolic'))
            return
        record, identity = obj.record, obj.identity
        self.details.append(app_image(identity, 64))
        self.details.append(label(identity.display_name if record.executable else record.command or 'Unknown process', 'title-1', wrap=True, justify=Gtk.Justification.CENTER))
        group = Adw.PreferencesGroup(title='Connection', description='Snapshot data; socket state does not prove internet access.')
        for title, text in (('Computer', 'This computer' if record.node == 'unix:/local' else record.node),
                ('Remote endpoint', endpoint(record.destination, record.destination_port)), ('Local endpoint', endpoint(record.source, record.source_port)),
                ('Protocol', record.protocol_name), ('State', record.state_name), ('Family', record.family_name), ('Interface', record.interface or 'Unknown')):
            detail_row(group, title, text)
        self.details.append(group)
        process = Adw.PreferencesGroup(title='Process')
        for title, text in (('Executable', record.executable or 'Unavailable'), ('Command', record.command or 'Unknown'),
                ('PID', record.pid if record.pid >= 0 else 'Unknown'), ('User ID', record.uid)):
            detail_row(process, title, text)
        self.details.append(process)
        diagnostic = Adw.PreferencesGroup(title='Socket details')
        for title, text in (('Inode', record.inode), ('Receive queue', record.receive_queue), ('Send queue', record.send_queue),
                ('Cookies', str(list(record.cookies))), ('Expires', record.expires), ('Retransmits', record.retransmits),
                ('Timer', record.timer), ('Mark', record.mark)):
            detail_row(diagnostic, title, str(text))
        self.details.append(diagnostic)
        application = Gtk.Button(label='Show in Apps', sensitive=bool(record.executable))
        application.connect('clicked', lambda *args: self.open_application(record))
        self.details.append(application)
        copy = Gtk.Button(label='Copy executable path', sensitive=bool(record.executable))
        copy.connect('clicked', lambda *args: self.window.get_clipboard().set(record.executable))
        self.details.append(copy)

    def open_application(self, record):
        self.window.open_application(record.node, record.executable)

    def copy_selected(self, *args):
        obj = self.selection.get_selected_item()
        if obj:
            self.window.get_clipboard().set(self.csv_text((obj,)))

    def csv_text(self, items=None):
        output = io.StringIO(); writer = csv.writer(output)
        writer.writerow(('Computer', 'Executable', 'Command', 'PID', 'UID', 'Local address', 'Local port', 'Remote address', 'Remote port',
                         'Protocol', 'Family', 'State', 'Interface', 'Inode', 'Receive queue', 'Send queue', 'Cookies', 'Expires', 'Retransmits', 'Timer', 'Mark'))
        if items is None:
            items = tuple(self.sorted.get_item(index) for index in range(self.sorted.get_n_items()))
        for obj in items:
            r = obj.record
            writer.writerow((r.node, r.executable, r.command, r.pid, r.uid, r.source, r.source_port, r.destination, r.destination_port,
                r.protocol, r.family, r.state, r.interface, r.inode, r.receive_queue, r.send_queue, str(list(r.cookies)), r.expires, r.retransmits, r.timer, r.mark))
        return output.getvalue()
