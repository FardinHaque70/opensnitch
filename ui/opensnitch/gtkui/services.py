"""Native Services presentation over shared discovery and application policies."""
import csv
import io
from .apps import Adw, GLib, Gtk, AppRow, app_image, label
from .widgets import ExecutableControls, detail_row, details_header
from opensnitch.core.services import shared_services


class ServicesView:
    def __init__(self, backend, window):
        self.backend, self.window = backend, window
        self.provider = backend.services
        self.rows, self.visible_records = {}, ()
        self.selected = None
        self._updating = False
        self._details_state = None
        self.widget = Adw.NavigationSplitView()
        self.widget.set_min_sidebar_width(420)
        self.widget.set_max_sidebar_width(760)
        self.widget.set_sidebar_width_fraction(.62)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=12, margin_bottom=12)
        tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
            margin_start=18, margin_end=18)
        self.search = Gtk.SearchEntry(placeholder_text='Search services, unit names or executable paths')
        self.search.connect('search-changed', lambda *args: self.update())
        tools.append(self.search)
        filters = Gtk.Box(spacing=8)
        self.scope = Gtk.DropDown.new_from_strings(['All services', 'System services', 'My services'])
        self.scope.set_tooltip_text('Service manager')
        self.sort = Gtk.DropDown.new_from_strings(['Name', 'Running state', 'Access policy'])
        self.sort.set_tooltip_text('Sort services by')
        self.nodes = Gtk.DropDown.new_from_strings(['This computer'])
        self.nodes.set_tooltip_text('Computer')
        self.node_ids = ['unix:/local']
        self.nodes.set_visible(False)
        for control in (self.scope, self.sort, self.nodes):
            control.connect('notify::selected', lambda *args: self.update())
            filters.append(control)
        tools.append(filters)
        heading = Gtk.Box(spacing=8)
        self.count = label('Services', 'title-2', xalign=0, hexpand=True)
        heading.append(self.count)
        export = Gtk.Button(label='Export…')
        export.set_tooltip_text('Export the filtered service list as CSV')
        export.connect('clicked', self.export)
        heading.append(export)
        tools.append(heading)
        side.append(tools)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True,
            margin_start=18, margin_end=18)
        self.list.add_css_class('boxed-list')
        self.list.set_sort_func(lambda a, b: (a.sort_key > b.sort_key) - (a.sort_key < b.sort_key))
        self.list.connect('row-selected', self.select)
        self.list.connect('row-activated', lambda box, row: self.widget.set_show_content(True))
        self.scroll = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.scroll, 'list')
        self.empty = Adw.StatusPage(title='Discovering services', icon_name='system-run-symbolic',
            description='Reading system and current-user service managers.')
        self.stack.add_named(self.empty, 'empty')
        side.append(self.stack)
        self.note = label('', 'dim-label', 'caption', wrap=True, margin_start=18, margin_end=18)
        side.append(self.note)
        self.widget.set_sidebar(Adw.NavigationPage.new(side, 'Services'))
        detail = Adw.ToolbarView()
        header = details_header('Service details')
        detail.add_top_bar(header)
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
            margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        self.detail_scroll = Gtk.ScrolledWindow(child=self.details, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.toasts = Adw.ToastOverlay(child=self.detail_scroll)
        detail.set_content(self.toasts)
        self.controls = ExecutableControls(self.request)
        detail.add_bottom_bar(self.controls)
        self.widget.set_content(Adw.NavigationPage.new(detail, 'Service details'))
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 860sp'))
        breakpoint.add_setter(self.widget, 'collapsed', True)
        window.add_breakpoint(breakpoint)
        backend.subscribe(lambda snapshot: self.update())
        self.provider.changed.connect(self.update)
        self.update()

    @property
    def node(self):
        index = self.nodes.get_selected()
        return self.node_ids[index] if index < len(self.node_ids) else 'unix:/local'

    def application(self, record):
        return self.backend.session.application_snapshots.get((self.node, record.executable_path))

    def update(self):
        if self._updating:
            return
        self._updating = True
        previous_node = self.node
        nodes = ['unix:/local'] + sorted(node for node in self.backend.session.nodes if node != 'unix:/local')
        if nodes != self.node_ids:
            self.node_ids = nodes
            self.nodes.set_model(Gtk.StringList.new(['This computer'] + nodes[1:]))
            self.nodes.set_selected(nodes.index(previous_node) if previous_node in nodes else 0)
        self.nodes.set_visible(len(nodes) > 1)
        query = self.search.get_text().strip().casefold()
        scope = ('', 'system', 'user')[self.scope.get_selected()]
        records = tuple(record for record in self.provider.records.values()
            if self.node == 'unix:/local' and (not scope or record.scope == scope)
            and (not query or query in '\n'.join((record.description, record.unit_name,
                record.configured_executable, record.executable_path)).casefold()))
        ids = {record.key for record in records}
        position = self.scroll.get_vadjustment().get_value()
        for key in tuple(self.rows):
            if key not in self.provider.records:
                self.list.remove(self.rows.pop(key))
        for record in records:
            application = self.application(record)
            policy = 'Unavailable' if application is None else application.policy
            row = self.rows.get(record.key)
            if row is None:
                row = Adw.ActionRow(use_markup=False)
                row.entry_key = record.key
                row.sort_key = ('', record.description.casefold(), record.unit_name, record.scope)
                row.set_selectable(True)
                row.set_activatable(True)
                row.add_prefix(Gtk.Image.new_from_icon_name('system-run-symbolic'))
                row.state = label('', 'caption')
                row.add_suffix(row.state)
                self.list.append(row)
                self.rows[record.key] = row
            row.set_title(record.description or record.unit_name)
            manager = 'System' if record.scope == 'system' else 'My services'
            row.set_subtitle(record.unit_name + ' · ' + manager + ' · ' + record.active_state)
            row.set_tooltip_text(record.configured_executable or record.reason)
            row.state.set_text('Applying…' if application and application.pending else policy)
            row.sort_key = ((record.active_state if self.sort.get_selected() == 1 else policy if self.sort.get_selected() == 2 else ''),
                            record.description.casefold(), record.unit_name, record.scope)
        for key, row in self.rows.items():
            row.set_visible(key in ids)
        self.list.invalidate_sort()
        self.visible_records = tuple(sorted(records, key=lambda item: self.rows[item.key].sort_key))
        self.count.set_text('Services · ' + str(len(records)))
        self.stack.set_visible_child_name('list' if records else 'empty')
        if self.node != 'unix:/local':
            self.empty.set_title('Local service discovery only')
            self.empty.set_description('Remote service integration is unavailable. Application policies remain separate for each computer.')
        elif self.provider.generation == 0:
            self.empty.set_title('Discovering services')
        else:
            self.empty.set_title('No matching services' if not self.provider.errors else 'Service discovery unavailable')
            self.empty.set_description('Try another name, executable path or manager.' if not self.provider.errors else '\n'.join(self.provider.errors.values()))
        errors = '\n'.join(scope.capitalize() + ': ' + error for scope, error in self.provider.errors.items())
        self.note.set_text(self.backend.snapshot.message + ('\n' + errors if errors else ''))
        if self.selected not in ids:
            self.selected = self.visible_records[0].key if self.visible_records else None
        self.list.select_row(self.rows[self.selected] if self.selected else None)
        self._updating = False
        self.show_details()
        def restore():
            adjustment = self.scroll.get_vadjustment()
            adjustment.set_value(min(position, max(0, adjustment.get_upper() - adjustment.get_page_size())))
            return False
        GLib.idle_add(restore)

    def select(self, box, row):
        if row is not None and not self._updating:
            self.selected = row.entry_key
            self.show_details()

    def show_details(self):
        record = self.provider.records.get(self.selected)
        application = self.application(record) if record else None
        shared = shared_services(self.provider.records, record.executable_path) if record else ()
        verifying = self.backend.service_controls.pending is not None
        reason = (record.reason or self.provider.errors.get(record.scope, '') or
                  (application.error if application else 'No authoritative application policy is available.')) if record else 'Select a service.'
        self.controls.update(bool(record and record.executable_path), reason,
            verifying or bool(application and application.pending), bool(application and application.related_rules))
        if verifying:
            self.controls.reason.set_text('Verifying service executables…')
        state = record, application, shared
        if state == self._details_state:
            return
        self._details_state = state
        position = self.detail_scroll.get_vadjustment().get_value()
        child = self.details.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.details.remove(child)
            child = following
        if record is None:
            self.details.append(Adw.StatusPage(title='Select a service', icon_name='system-run-symbolic'))
            return
        if application:
            self.details.append(app_image(AppRow.from_snapshot(application), 64))
        else:
            icon = Gtk.Image.new_from_icon_name('system-run-symbolic'); icon.set_pixel_size(64)
            self.details.append(icon)
        self.details.append(label(record.description or record.unit_name, 'title-1', wrap=True, justify=Gtk.Justification.CENTER))
        info = Adw.PreferencesGroup(title='Service')
        for title, value in (('Unit', record.unit_name), ('Manager', 'System' if record.scope == 'system' else 'Current user'),
                ('State', record.active_state + (' · ' + record.sub_state if record.sub_state else '')),
                ('Unit file', record.file_state or 'Not listed'), ('Configured executable', record.configured_executable or 'Unresolved')):
            detail_row(info, title, value)
        if record.aliases:
            detail_row(info, 'Aliases', '\n'.join(record.aliases))
        self.details.append(info)
        access = Adw.PreferencesGroup(title='Executable access',
            description='Applies to every process using this executable, including other services that share it.')
        detail_row(access, 'Executable', record.executable_path or 'Cannot be verified')
        if application:
            detail_row(access, 'Configured policy', application.policy)
            if application.related_rules:
                detail_row(access, 'Related rules', '\n'.join(application.related_rules))
            if application.global_rules:
                other = Adw.ExpanderRow(title='Other network rules', subtitle='Additional conditions may match individual connections', use_markup=False)
                for name, description in application.other_rule_details:
                    detail_row(other, name, description)
                access.add(other)
        if reason:
            detail_row(access, 'Access controls unavailable', reason)
        self.details.append(access)
        if shared:
            sharing = Adw.PreferencesGroup(title='Known services using this executable',
                description='An access change affects all of these services. Other programs may use the same executable.')
            for scope, unit in shared:
                detail_row(sharing, unit, 'System service' if scope == 'system' else 'Current-user service')
            self.details.append(sharing)
        copy = Gtk.Button(label='Copy executable path', sensitive=bool(record.executable_path))
        copy.connect('clicked', lambda *args: self.window.get_clipboard().set(record.executable_path))
        self.details.append(copy)
        GLib.idle_add(lambda: (self.detail_scroll.get_vadjustment().set_value(position), False)[1])

    def request(self, action, duration):
        if self.selected:
            self.backend.service_controls.request(self.selected, self.node, action, duration)

    def csv_text(self):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(('Manager', 'Unit', 'Description', 'State', 'Executable', 'Configured policy'))
        for record in self.visible_records:
            application = self.application(record)
            writer.writerow((record.scope, record.unit_name, record.description, record.active_state,
                             record.executable_path or record.configured_executable,
                             application.policy if application else 'Unavailable'))
        return output.getvalue()

    def export(self, *args):
        from .export import save_text
        save_text(self.window, 'opensnitch-services.csv', self.csv_text())
