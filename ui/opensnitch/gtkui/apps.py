#!/usr/bin/python3
"""Native Adwaita shell and Apps view over shared immutable snapshots."""

from dataclasses import dataclass
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from .widgets import ExecutableControls, detail_row, details_header
from .desktop import APPLICATION_ID, register_application


GROUPS = ('Allowed', 'Blocked', 'Temporary rules', 'Custom rules', 'Not configured', 'Unavailable')
STYLES = {'Allowed': 'success', 'Blocked': 'error', 'Temporary rules': 'warning',
          'Custom rules': 'accent', 'Not configured': 'dim-label', 'Unavailable': 'dim-label'}
SCHEMES = {'system': Adw.ColorScheme.DEFAULT, 'light': Adw.ColorScheme.FORCE_LIGHT,
           'dark': Adw.ColorScheme.FORCE_DARK}


@dataclass(frozen=True)
class AppRow:
    id: tuple
    name: str
    path: str
    node: str
    policy: str
    action: str
    duration: str
    activity: str
    source: str
    icon: str
    rule: str = ''
    condition: str = ''
    artwork: tuple[str, ...] = ()
    pending: bool = False
    error: str = ''
    global_rules: tuple[str, ...] = ()
    other_rule_details: tuple[tuple[str, str], ...] = ()
    artwork_owner: str = ''

    @classmethod
    def from_snapshot(cls, record):
        identity = record.identity
        from datetime import datetime
        activity = datetime.fromtimestamp(record.last_seen).strftime('%Y-%m-%d %H:%M') if record.last_seen else 'No recorded activity'
        if record.available is False:
            activity += ' · Executable unavailable'
        node = 'This computer' if identity.node_id == 'unix:/local' else identity.node_id
        return cls(record.key, identity.display_name, identity.executable_path, node,
            record.policy, {'allow': 'Allowed', 'deny': 'Blocked', 'reject': 'Rejected'}.get(record.action, record.action) or ('Uses defaults' if record.policy == 'Not configured' else record.policy),
            {'always': 'Until changed', 'until restart': 'Until reboot', 'once': 'Once'}.get(record.duration, record.duration) or ('No application rule' if record.policy == 'Not configured' else 'No confirmed application rule'), activity,
            ', '.join(record.sources), identity.icon_reference, rule='\n'.join(record.related_rules),
            artwork=identity.icon_candidates, pending=record.pending, error=record.error, global_rules=record.global_rules, other_rule_details=record.other_rule_details, artwork_owner=identity.icon_owner)


def matches(record, query, node='All computers'):
    query = query.casefold().strip()
    return (node == 'All computers' or record.node == node) and (
        not query or query in (record.name + '\n' + record.path).casefold())


def label(text='', *classes, **kwargs):
    widget = Gtk.Label(label=text, **kwargs)
    for css_class in classes:
        widget.add_css_class(css_class)
    return widget


def policy_badge(record):
    text = 'Applying…' if record.pending else (record.action.capitalize() if record.policy == 'Temporary rules' else record.policy)
    if record.policy == 'Not configured' and not record.pending:
        text = 'Uses defaults'
    widget = label(text, 'status-pill', STYLES[record.policy], valign=Gtk.Align.CENTER)
    widget.set_tooltip_text(record.duration)
    return widget


def app_image(record, size):
    # Called on the GTK thread. Decode actual artwork before using it.
    from gi.repository import GdkPixbuf
    image = Gtk.Image()
    reference = getattr(record, 'icon', getattr(record, 'icon_reference', ''))
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    candidates = []
    if reference:
        if Path(reference).is_absolute():
            candidates.append(reference)
        elif theme.has_icon(reference):
            paintable = theme.lookup_icon(reference, None, size, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags(0))
            file = paintable.get_file()
            if file and file.get_path():
                candidates.append(file.get_path())
    candidates.extend(getattr(record, 'artwork', getattr(record, 'icon_candidates', ())))
    fallback = Path(__file__).resolve().parent.parent / 'res/icons/fallback/light/system-run.png'
    candidates.append(str(fallback))
    for candidate in candidates:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(candidate, size, size, True)
            image.set_from_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
            break
        except GLib.Error:
            continue
    else:
        image.set_from_icon_name('application-x-executable-symbolic')
    image.set_pixel_size(size)
    image.set_valign(Gtk.Align.CENTER)
    return image


class AppsWindow(Adw.ApplicationWindow):
    def __init__(self, app, records=None):
        super().__init__(application=app, title='OpenSnitch — Development', default_width=1120, default_height=820)
        # Like connection prompts, request native floating treatment from KWin.
        # No transient parent: this remains the independent main application window.
        self.set_modal(True)
        self.set_size_request(360, 360)
        self.records = records if records is not None else ()
        self.by_id = {record.id: record for record in self.records}
        self.selected_id = None
        self.rows, self.groups, self.lists = {}, {}, {}
        self._filtering = False
        self.live = bool(getattr(app, 'live_socket', None))
        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(420)
        self.split.set_max_sidebar_width(760)
        self.split.set_sidebar_width_fraction(0.62)
        self.sidebar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='OpenSnitch', subtitle='Application access' if self.live else 'Read-only application inventory'))
        self.root_view = Adw.ToolbarView()
        self.root_view.add_top_bar(header)
        actions = Gtk.Box(spacing=10, margin_start=18, margin_end=18, margin_top=8, margin_bottom=4)
        refresh_content = Gtk.Box(spacing=6)
        refresh_content.append(Gtk.Image.new_from_icon_name('view-refresh-symbolic'))
        refresh_content.append(label('Refresh'))
        self.refresh_button = Gtk.Button(child=refresh_content, action_name='app.refresh')
        self.refresh_button.set_tooltip_text('Reload applications and current rules')
        actions.append(self.refresh_button)
        self.filtering_buttons = {}
        self.filtering_status = None
        self._updating_filtering = False
        if self.live:
            filtering = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER, margin_start=18, margin_end=18, margin_top=6, margin_bottom=4)
            filtering.append(label('App filtering', xalign=0, hexpand=True))
            self.filtering_status = label('Unavailable', 'dim-label')
            choices = Gtk.Box(spacing=0)
            choices.add_css_class('linked')
            for title, enabled in (('On', True), ('Off', False)):
                button = Gtk.ToggleButton(label=title, sensitive=False)
                if self.filtering_buttons:
                    button.set_group(next(iter(self.filtering_buttons.values())))
                button.set_tooltip_text(('Enable' if enabled else 'Pause') + ' app filtering on this computer; saved rules stay in place.')
                button.connect('clicked', self.change_filtering, enabled)
                self.filtering_buttons[enabled] = button
                choices.append(button)
            filtering.append(choices)
            filtering.append(self.filtering_status)
            self.root_view.add_top_bar(filtering)
        if self.live:
            cleanup = Gtk.Button(label='Clean up…', action_name='app.cleanup-stale')
            cleanup.set_tooltip_text('Review unavailable temporary entries and matching rules')
            actions.append(cleanup)
        themes = Gtk.Box(spacing=0, hexpand=True, halign=Gtk.Align.END)
        themes.add_css_class('linked')
        self.theme_buttons = {}
        for title, value in (('System', 'system'), ('Light', 'light'), ('Dark', 'dark')):
            button = Gtk.ToggleButton(label=title)
            button.set_tooltip_text('Follow desktop appearance' if value == 'system' else title + ' appearance')
            if self.theme_buttons:
                button.set_group(next(iter(self.theme_buttons.values())))
            button.set_active(app.appearance == value)
            button.connect('clicked', lambda button, value=value: app.activate_action('appearance', GLib.Variant('s', value)))
            self.theme_buttons[value] = button
            themes.append(button)
        actions.append(themes)
        self.root_view.add_top_bar(actions)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        toolbar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
            margin_start=18, margin_end=18, margin_top=12, margin_bottom=12)
        self.search = Gtk.SearchEntry(placeholder_text='Search applications or executable paths', hexpand=True)
        self.search.connect('search-changed', self.filter_rows)
        self.nodes = Gtk.DropDown.new_from_strings(['All computers'])
        self.nodes.set_visible(False)
        self.nodes.set_tooltip_text('Filter by computer')
        self.nodes.connect('notify::selected', self.filter_rows)
        toolbar.append(self.search)
        toolbar.append(self.nodes)
        side.append(toolbar)
        self.body_stack = Gtk.Stack(vexpand=True)
        self.app_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_start=18, margin_end=18, margin_top=4, margin_bottom=18)
        top = Gtk.Box(spacing=8)
        top.append(label('Apps', 'title-2', xalign=0, hexpand=True))
        self.count = label('', 'dim-label', 'caption')
        top.append(self.count)
        self.app_list.append(top)
        for group_name in GROUPS:
            heading = Gtk.Box(spacing=8, hexpand=True)
            heading.append(label(group_name, 'heading', xalign=0, hexpand=True))
            count = label('', 'dim-label', 'caption')
            heading.append(count)
            expander = Gtk.Expander(expanded=True)
            expander.set_label_widget(heading)
            group_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True)
            group_box.add_css_class('boxed-list')
            group_box.set_margin_top(8)
            group_box.connect('row-selected', self.row_selected)
            group_box.connect('row-activated', self.row_activated)
            expander.set_child(group_box)
            self.groups[group_name] = (expander, count)
            group_box.set_sort_func(lambda one, two: (one.sort_key > two.sort_key) - (one.sort_key < two.sort_key))
            self.lists[group_name] = group_box
            self.app_list.append(expander)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.app_list)
        self.body_stack.add_named(self.scroll, 'apps')
        self.empty = Adw.StatusPage(title='No matching applications', description='Try another name, path or computer.',
                                    icon_name='system-search-symbolic')
        self.body_stack.add_named(self.empty, 'empty')
        side.append(self.body_stack)
        self.note = label('Loading inventory…', 'dim-label', 'caption', wrap=True,
                     margin_top=8, margin_bottom=12)
        side.append(self.note)
        self.sidebar.set_content(side)
        self.details_view = Adw.ToolbarView()
        detail_header = details_header('Application details')
        menu = Gio.Menu()
        if self.live:
            menu.append('Clean up unavailable temporary apps…', 'app.cleanup-stale')
        menu.append('Quit OpenSnitch', 'app.quit')
        if getattr(app, 'supervised', False):
            menu.append('Return to Qt interface', 'app.rollback')
        menu_button = Gtk.MenuButton(icon_name='view-more-symbolic', menu_model=menu)
        menu_button.set_tooltip_text('Appearance and window options')
        header.pack_end(menu_button)
        self.details_view.add_top_bar(detail_header)
        self.details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
            margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        detail_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.details_box)
        self.toasts = Adw.ToastOverlay(child=detail_scroll)
        self.details_view.set_content(self.toasts)
        if self.live:
            controls = ExecutableControls(lambda action, duration: self.request_access(action, duration))
            self.duration_choice = controls.duration
            self.access_buttons = controls.buttons
            self.control_reason = controls.reason
            self.details_view.add_bottom_bar(controls)
        self.split.set_sidebar(Adw.NavigationPage.new(self.sidebar, 'Apps'))
        self.split.set_content(Adw.NavigationPage.new(self.details_view, 'Application details'))
        self.pages = Gtk.Stack(vexpand=True)
        self.pages.add_titled(self.split, 'apps', 'Apps')
        switcher = Gtk.StackSwitcher(stack=self.pages, halign=Gtk.Align.CENTER,
            margin_top=6, margin_bottom=6)
        self.root_view.add_top_bar(switcher)
        self.root_view.set_content(self.pages)
        self.set_content(self.root_view)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 860sp'))
        breakpoint.add_setter(self.split, 'collapsed', True)
        self.add_breakpoint(breakpoint)
        self.filter_rows()

    def apply_snapshot(self, snapshot):
        self.update_filtering(snapshot.interception)
        records = tuple(AppRow.from_snapshot(record) for record in snapshot.applications)
        if records == self.records:
            self.note.set_text(snapshot.message)
            return
        position = self.scroll.get_vadjustment().get_value()
        self._filtering = True
        ids = {record.id for record in records}
        for key in tuple(self.rows):
            if key not in ids:
                row = self.rows.pop(key)
                row.get_parent().remove(row)
        previous = self.by_id
        names = {}
        for record in records:
            name_key = (record.node, record.name.casefold())
            names[name_key] = names.get(name_key, 0) + 1
        for record in records:
            row = self.rows.get(record.id)
            if row is None:
                row = Adw.ActionRow(use_markup=False)
                row.entry_key = record.id
                row.sort_key = (record.name.casefold(), record.path, record.node)
                row.set_selectable(True)
                row.set_activatable(True)
                row.artwork = app_image(record, 32)
                row.add_prefix(row.artwork)
                row.badge = policy_badge(record)
                row.add_suffix(row.badge)
                self.lists[record.policy].append(row)
                self.rows[record.id] = row
            elif previous.get(record.id) != record:
                row.remove(row.artwork)
                row.artwork = app_image(record, 32)
                row.add_prefix(row.artwork)
                row.remove(row.badge)
                row.badge = policy_badge(record)
                row.add_suffix(row.badge)
                if row.get_parent() is not self.lists[record.policy]:
                    row.get_parent().remove(row)
                    self.lists[record.policy].append(row)
            row.sort_key = (record.name.casefold(), record.path, record.node)
            row.set_title(record.name)
            subtitle = record.activity if record.node == 'This computer' else record.node + ' · ' + record.activity
            if record.artwork_owner or names[record.node, record.name.casefold()] > 1:
                subtitle = record.path + ' · ' + subtitle
            row.set_subtitle(subtitle)
            row.set_tooltip_text(record.path + '\n' + record.node)
        for box in self.lists.values():
            box.invalidate_sort()
        self.records = records
        self.by_id = {record.id: record for record in records}
        current = self.nodes.get_selected_item().get_string()
        computers = ['All computers'] + sorted({record.node for record in records})
        self.nodes.set_model(Gtk.StringList.new(computers))
        self.nodes.set_selected(computers.index(current) if current in computers else 0)
        self.nodes.set_visible(len(computers) > 2)
        self._filtering = False
        self.note.set_text(snapshot.message)
        self.filter_rows()
        if self.selected_id in self.by_id:
            self.show_details(self.by_id[self.selected_id])
        def restore():
            adjustment = self.scroll.get_vadjustment()
            adjustment.set_value(min(position, max(0, adjustment.get_upper() - adjustment.get_page_size())))
            return False
        GLib.idle_add(restore)

    def update_filtering(self, state):
        if not self.filtering_buttons:
            return
        self._updating_filtering = True
        for enabled, button in self.filtering_buttons.items():
            button.set_active(state.enabled is enabled)
            button.set_sensitive(state.available)
            button.set_tooltip_text(state.error or ('Enable' if enabled else 'Pause') + ' app filtering on this computer; saved rules stay in place.')
        text = ('Resuming…' if state.requested else 'Pausing…') if state.pending else ('On' if state.enabled else 'Off') if state.enabled is not None else 'Not confirmed'
        self.filtering_status.set_text(text)
        self._updating_filtering = False

    def change_filtering(self, button, enabled):
        if not self._updating_filtering:
            backend = getattr(self.get_application(), 'backend', None)
            if backend:
                backend.request_interception(enabled)

    def filter_rows(self, *args):
        if self._filtering:
            return
        self._filtering = True
        node = self.nodes.get_selected_item().get_string()
        visible = [record for record in self.records if matches(record, self.search.get_text(), node)]
        ids = {record.id for record in visible}
        for record_id, row in self.rows.items():
            row.set_visible(record_id in ids)
        for name, (expander, count) in self.groups.items():
            size = sum(record.policy == name for record in visible)
            count.set_text(str(size))
            expander.set_visible(size > 0)
        self.count.set_text('{} {}'.format(len(visible), 'application' if len(visible) == 1 else 'applications'))
        self.body_stack.set_visible_child_name('apps' if visible else 'empty')
        self._filtering = False
        if visible:
            record = self.by_id[self.selected_id] if self.selected_id in ids else visible[0]
            box = self.lists[record.policy]
            box.select_row(self.rows[record.id])
            if self.selected_id != record.id:
                self.row_selected(box, self.rows[record.id])
        else:
            self.selected_id = None
            self.show_details(None)

    def row_selected(self, box, row):
        if row is None or self._filtering:
            return
        for other in self.lists.values():
            if other is not box:
                other.unselect_all()
        self.selected_id = row.entry_key
        self.show_details(self.by_id[self.selected_id])

    def row_activated(self, box, row):
        box.select_row(row)
        if self.selected_id != row.entry_key:
            self.row_selected(box, row)
        self.split.set_show_content(True)

    def detail_row(self, group, title, value):
        return detail_row(group, title, value)

    def show_message(self, text):
        page = self.pages.get_visible_child_name()
        view = getattr(self, page, None) if page != 'apps' else None
        overlay = view.toasts if view is not None else self.toasts
        overlay.add_toast(Adw.Toast.new(text))

    def request_access(self, action, duration=None):
        backend = getattr(self.get_application(), 'backend', None)
        if backend and self.selected_id:
            duration = duration or ('always', 'until restart', '12h', '1h')[self.duration_choice.get_selected()]
            backend.request_policy(self.selected_id, action, duration)

    def show_details(self, record):
        if self.live:
            reason = record.error if record else 'Select an application.'
            self.control_reason.set_text('Applying…' if record and record.pending else reason)
            for index, button in enumerate(self.access_buttons):
                button.set_sensitive(bool(record) and not reason and not record.pending and (index != 2 or bool(record.rule)))
        child = self.details_box.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.details_box.remove(child)
            child = following
        if record is None:
            self.details_box.append(Adw.StatusPage(title='Select an application', icon_name='application-x-executable-symbolic'))
            return
        self.details_box.append(app_image(record, 64))
        self.details_box.append(label(record.name, 'title-1', wrap=True, justify=Gtk.Justification.CENTER))
        self.details_box.append(label(record.node, 'dim-label'))
        if record.artwork_owner:
            self.details_box.append(label('Related app: ' + record.artwork_owner, 'dim-label', 'caption'))
        badge_box = Gtk.Box(halign=Gtk.Align.CENTER)
        badge_box.append(policy_badge(record))
        self.details_box.append(badge_box)
        policy = Adw.PreferencesGroup(title='Configured access', description='This choice controls only the executable shown below.' if record.policy != 'Unavailable' else 'No current confirmed daemon policy is available.')
        self.detail_row(policy, 'Application policy', record.action)
        self.detail_row(policy, 'Applies to', record.path)
        self.detail_row(policy, 'Duration', record.duration)
        if record.condition:
            self.detail_row(policy, 'Conditions', record.condition)
        if record.error:
            self.detail_row(policy, 'Status', record.error)
        if record.global_rules:
            other_rules = Adw.ExpanderRow(title='Other network rules', subtitle='Not restricted to one executable', use_markup=False)
            for name, description in record.other_rule_details or tuple((name, 'No executable condition') for name in record.global_rules):
                self.detail_row(other_rules, name, description)
            self.detail_row(other_rules, 'How these work', 'Rules for destinations, ports or users can also match a connection. A matching block or priority rule can affect an app-specific choice. This list does not mean every rule matches this app.')
            policy.add(other_rules)
        self.details_box.append(policy)
        information = Adw.PreferencesGroup(title='Application')
        self.detail_row(information, 'Executable', record.path)
        self.detail_row(information, 'Last activity', record.activity)
        self.detail_row(information, 'Discovery', record.source)
        if record.rule:
            rule = Adw.ExpanderRow(title='Related rule', subtitle='Rule identifier', use_markup=False)
            self.detail_row(rule, 'Rule name', record.rule)
            information.add(rule)
        self.details_box.append(information)
        copy = Gtk.Button(label='Copy executable path', halign=Gtk.Align.CENTER)
        copy.connect('clicked', self.copy_path)
        self.details_box.append(copy)
        if self.live:
            activity = Gtk.Button(label='View activity')
            activity.connect('clicked', lambda *args: self.open_activity(*record.id))
            self.details_box.append(activity)
            rules = Gtk.Button(label='View rules')
            rules.connect('clicked', lambda *args: self.open_rules(*record.id))
            self.details_box.append(rules)

    def open_application(self, node, path):
        self.search.set_text(path)
        self.filter_rows()
        key = node, path
        if key in self.by_id:
            self.selected_id = key
            record = self.by_id[key]
            self.lists[record.policy].select_row(self.rows[key])
            self.show_details(record)
        self.pages.set_visible_child_name('apps')
        self.split.set_show_content(True)

    def open_activity(self, node, path):
        if hasattr(self, 'activity'):
            self.activity.focus_application(node, path)
            self.pages.set_visible_child_name('activity')

    def open_rules(self, node, path):
        if hasattr(self, 'advanced'):
            self.advanced.open_rules(node, path)

    def open_rule(self, node, name):
        if hasattr(self, 'advanced'):
            self.advanced.open_rule(node, name)

    def copy_path(self, *args):
        if self.selected_id:
            self.get_clipboard().set(self.by_id[self.selected_id].path)


class GtkApplication(Adw.Application):
    def __init__(self, appearance='system', live_socket=None, ready_file=None, background_on_close=False, start_hidden=False, exit_reason_file=None):
        super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.background_on_close = background_on_close or start_hidden
        self._start_hidden = start_hidden
        self.exit_reason_file = exit_reason_file
        self.tray = None
        self._tray_fallback = 0
        self._held = False
        self.appearance = appearance
        self.live_socket = live_socket
        self.ready_file = ready_file
        self.supervised = bool(ready_file)
        self.window = None
        self.cleanup_window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        if self.background_on_close:
            self.hold(); self._held = True
        try:
            desktop, icon = register_application()
            Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icon.parent))
            Gtk.Window.set_default_icon_name(APPLICATION_ID)
        except (OSError, ValueError):
            import logging
            logging.exception('Could not register development application artwork')
            Gtk.Window.set_default_icon_name('opensnitch-ui')
        self.get_style_manager().set_color_scheme(SCHEMES[self.appearance])
        provider = Gtk.CssProvider()
        provider.load_from_string('.status-pill { border-radius: 999px; padding: 3px 10px; background-color: alpha(currentColor, 0.10); font-size: 0.85em; font-weight: 600; }')
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.appearance_action = Gio.SimpleAction.new_stateful('appearance', GLib.VariantType.new('s'), GLib.Variant('s', self.appearance))
        self.appearance_action.connect('activate', self.change_appearance)
        self.add_action(self.appearance_action)
        quit_action = Gio.SimpleAction.new('quit', None)
        quit_action.connect('activate', self.close_window)
        self.add_action(quit_action)
        self.set_accels_for_action('app.quit', ['<primary>q'])
        rollback = Gio.SimpleAction.new('rollback', None)
        rollback.connect('activate', lambda *args: self.quit())
        self.add_action(rollback)
        refresh = Gio.SimpleAction.new('refresh', None)
        refresh.connect('activate', self.refresh_current)
        self.add_action(refresh)
        cleanup = Gio.SimpleAction.new('cleanup-stale', None)
        cleanup.connect('activate', self.show_cleanup)
        self.add_action(cleanup)

    def do_activate(self):
        if self.window is None:
            self.window = AppsWindow(self)
            if self.live_socket:
                from .live import LiveBackend
                try:
                    self.backend = LiveBackend(self, self.live_socket)
                except Exception:
                    import logging
                    logging.exception('GTK session failed; return to the previous frontend')
                    self.quit()
                    return
                self.window.connect('close-request', self.close_main_window)
            else:
                from .backend import InventoryBackend
                self.backend = InventoryBackend()
            self.backend.subscribe(self.window.apply_snapshot)
            if self.live_socket:
                from .services import ServicesView
                self.window.services = ServicesView(self.backend, self.window)
                self.window.pages.add_titled(self.window.services.widget, 'services', 'Services')
                from .connections import ConnectionsView
                self.window.connections = ConnectionsView(self.backend, self.window)
                self.window.pages.add_titled(self.window.connections.widget, 'connections', 'Connections')
                from .activity import ActivityView
                self.window.activity = ActivityView(self.backend, self.window)
                self.window.pages.add_titled(self.window.activity.widget, 'activity', 'Activity')
                from .advanced import AdvancedView
                self.window.advanced = AdvancedView(self.backend, self.window)
                self.window.pages.add_titled(self.window.advanced.widget, 'advanced', 'Advanced')
                self.window.pages.connect('notify::visible-child-name', self.page_changed)
            if self.live_socket:
                from .tray import TrayIcon
                try:
                    self.tray = TrayIcon(self.present_main, self.close_window, self.tray_available, self.tray_activation_token, self.toggle_filtering)
                    self.tray.start()
                    self.tray_available(False)
                    self.backend.subscribe(self.tray.update)
                except Exception:
                    import logging
                    logging.exception('Could not start system tray integration')
                    if self.tray:
                        self.tray.close()
                    self.tray = None
                    self.tray_available(False)
            if self.live_socket and self.ready_file:
                self.backend.subscribe(self._ready)
        if self._start_hidden:
            self._start_hidden = False
        else:
            self.window.present()

    def tray_activation_token(self, token):
        if token and self.window:
            self.window.set_startup_id(token)

    def toggle_filtering(self, enabled=None):
        if hasattr(self, 'backend'):
            state = self.backend.snapshot.interception
            if state.available:
                self.backend.request_interception(not state.enabled if enabled is None else enabled)

    def show_cleanup(self, *args):
        if not self.live_socket or not hasattr(self, 'backend'):
            return
        from .cleanup import CleanupWindow
        if self.cleanup_window is None or self.cleanup_window.closed:
            self.cleanup_window = CleanupWindow(self)
        self.cleanup_window.present()

    def present_main(self):
        self._start_hidden = False
        self.activate()

    def tray_available(self, available):
        if self._tray_fallback:
            GLib.source_remove(self._tray_fallback)
            self._tray_fallback = 0
        if not available:
            # Allow Plasma time to start at login. Without a tray, keep the UI
            # reachable instead of leaving a background-only frontend.
            self._tray_fallback = GLib.timeout_add_seconds(5, self._show_without_tray)

    def _show_without_tray(self):
        self._tray_fallback = 0
        if not self.tray or not self.tray.registered:
            self.present_main()
        return False

    def close_main_window(self, window):
        if self.background_on_close:
            if self.tray and self.tray.registered:
                window.set_visible(False)
            else:
                window.present()
            return True
        self.quit()
        return False

    def page_changed(self, *args):
        page = self.window.pages.get_visible_child_name()
        self.backend.services.set_active(page == 'services')
        self.window.connections.set_active(page == 'connections')
        self.window.activity.set_active(page == 'activity')

    def refresh_current(self, *args):
        if self.live_socket and self.window.pages.get_visible_child_name() == 'connections':
            self.backend.session.sockets.refresh(once=True)
        elif self.live_socket and self.window.pages.get_visible_child_name() == 'activity':
            self.backend.session.activity.refresh(latest=True)
        elif self.live_socket and self.window.pages.get_visible_child_name() == 'advanced':
            self.window.advanced.refresh()
        elif self.live_socket:
            self.backend.refresh()
        else:
            self.backend.refresh(reindex=True)

    def _ready(self, snapshot):
        if snapshot.connected and self.ready_file:
            Path(self.ready_file).write_text('connected\n')
            self.ready_file = None

    def do_shutdown(self):
        if self._tray_fallback:
            GLib.source_remove(self._tray_fallback); self._tray_fallback = 0
        if self.tray:
            self.tray.close()
        if self.window:
            for name in ('connections', 'activity', 'advanced'):
                view = getattr(self.window, name, None)
                if view:
                    try:
                        view.save_layout()
                    except OSError:
                        import logging
                        logging.exception('Could not save GTK table layout')
        if hasattr(self, 'backend'):
            self.backend.close()
        if self._held:
            self.release(); self._held = False
        Adw.Application.do_shutdown(self)

    def change_appearance(self, action, parameter):
        appearance = parameter.get_string()
        if appearance in SCHEMES:
            self.appearance = appearance
            action.set_state(parameter)
            self.get_style_manager().set_color_scheme(SCHEMES[appearance])
            if self.window and hasattr(self.window, 'theme_buttons'):
                self.window.theme_buttons[appearance].set_active(True)

    def close_window(self, *args):
        if self.exit_reason_file:
            Path(self.exit_reason_file).write_text('quit\n')
        self.quit()
