"""Advanced workflows appear only as their native pages become available."""
from .apps import Adw, Gtk
from .rules import RulesView


class AdvancedView:
    def __init__(self, backend, window):
        self.backend, self.window = backend, window
        self.toolbar = Adw.ToolbarView()
        self.widget = Adw.ToastOverlay(child=self.toolbar)
        self.toasts = self.widget
        self.stack = Gtk.Stack(vexpand=True)
        self.context = Gtk.Box(spacing=10, margin_start=18, margin_end=18, margin_top=6, margin_bottom=6)
        back_content = Gtk.Box(spacing=6)
        back_content.append(Gtk.Image.new_from_icon_name('go-previous-symbolic'))
        back_content.append(Gtk.Label(label='Advanced'))
        back = Gtk.Button(child=back_content)
        back.set_tooltip_text('Back to Advanced tools')
        back.connect('clicked', lambda *args: self.stack.set_visible_child_name('tools'))
        self.context.append(back)
        self.context.append(Gtk.Label(label='Rules', xalign=0))
        self.toolbar.add_top_bar(self.context)
        page = Adw.PreferencesPage(title='Advanced')
        group = Adw.PreferencesGroup(title='Advanced', description='Detailed firewall configuration and diagnostics are being migrated in verified stages.')
        row = Adw.ActionRow(title='Rules', subtitle='Manage rule identifiers, conditions and configured actions',
            activatable=True, use_markup=False)
        row.add_prefix(Gtk.Image.new_from_icon_name('view-list-symbolic'))
        row.add_suffix(Gtk.Image.new_from_icon_name('go-next-symbolic'))
        row.connect('activated', lambda *args: self.stack.set_visible_child_name('rules'))
        group.add(row)
        page.add(group)
        note = Adw.PreferencesGroup(description='Remaining Advanced workflows are available through “Return to Qt interface” in the window menu during migration.')
        page.add(note)
        self.stack.add_named(page, 'tools')
        self.rules = RulesView(backend, window)
        self.stack.add_named(self.rules.widget, 'rules')
        self.stack.connect('notify::visible-child-name', lambda *args: self.context.set_visible(self.stack.get_visible_child_name() != 'tools'))
        self.stack.set_visible_child_name('tools')
        self.context.set_visible(False)
        self.toolbar.set_content(self.stack)

    def open_rules(self, node='', executable=''):
        if executable:
            self.rules.focus_application(node, executable)
        else:
            self.rules.clear_focus()
        self.stack.set_visible_child_name('rules')
        self.window.pages.set_visible_child_name('advanced')

    def open_rule(self, node, name):
        self.rules.focus_identifier(node, name)
        self.stack.set_visible_child_name('rules')
        self.window.pages.set_visible_child_name('advanced')

    def refresh(self):
        self.backend.session.refresh_policies()

    def save_layout(self):
        self.rules.save_layout()
