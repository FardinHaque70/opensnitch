"""Small shared native presentation components; no policy logic."""
from gi.repository import Adw, Gtk

DURATIONS = ('always', 'until restart', '12h', '1h')


def detail_row(group, title, value):
    row = Adw.ActionRow(title=title, subtitle=str(value), use_markup=False)
    row.set_subtitle_selectable(True)
    row.set_subtitle_lines(0)
    if isinstance(group, Adw.ExpanderRow):
        group.add_row(row)
    else:
        group.add(row)
    return row


class ExecutableControls(Gtk.Box):
    def __init__(self, request):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_start=18, margin_end=18, margin_top=12, margin_bottom=18)
        self.duration = Gtk.DropDown.new_from_strings(['Until changed', 'Until reboot', '12 hours', '1 hour'])
        self.duration.set_tooltip_text('Duration for new Allow or Block choices')
        self.append(self.duration)
        buttons = Gtk.Box(spacing=8, homogeneous=True)
        self.buttons = []
        for title, action in (('Allow', 'allow'), ('Block', 'deny'), ('Use defaults', 'defaults')):
            button = Gtk.Button(label=title)
            button.connect('clicked', lambda button, action=action: request(action, DURATIONS[self.duration.get_selected()]))
            buttons.append(button)
            self.buttons.append(button)
        self.append(buttons)
        self.reason = Gtk.Label(wrap=True, xalign=0)
        self.reason.add_css_class('dim-label')
        self.reason.add_css_class('caption')
        self.append(self.reason)
        self.update(False, 'Select an executable.')

    def update(self, selected, reason='', pending=False, has_rules=False):
        self.reason.set_text('Applying…' if pending else reason)
        for index, button in enumerate(self.buttons):
            button.set_sensitive(bool(selected) and not reason and not pending and (index != 2 or has_rules))


def details_header(title):
    """Internal navigation headers must not duplicate the window's controls."""
    header = Adw.HeaderBar()
    header.set_title_widget(Adw.WindowTitle(title=title))
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)
    return header
