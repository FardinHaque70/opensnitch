"""Shared native table rendering, sorting, selection, scroll and layout behavior."""
from gi.repository import GObject, Pango
from .apps import Gio, GLib, Gtk, app_image, label


class RecordItem(GObject.Object):
    version = GObject.Property(type=int, default=0)
    def __init__(self, record, identity):
        super().__init__()
        self.record, self.identity = record, identity


class RecordTable:
    def initialize_table(self, columns, layout_name, raw_value, default_sort='executable', default_order=Gtk.SortType.ASCENDING):
        self.layout_name, self.raw_value, self.default_sort = layout_name, raw_value, default_sort
        self.default_order = default_order
        self._objects = {}
        self.selected_key = None
        self.visible_items = ()
        self.columns = {}
        self._restoring_scroll = False
        self._scroll_restore_source = 0
        self.store = Gio.ListStore.new(RecordItem)
        self.sorted = Gtk.SortListModel.new(self.store, None)
        self.selection = Gtk.SingleSelection.new(self.sorted)
        self.selection.set_autoselect(False)
        self.selection.set_can_unselect(True)
        self.selection.connect('notify::selected-item', self.select)
        self.table = Gtk.ColumnView.new(self.selection)
        self.table.set_reorderable(True)
        self.table.set_show_column_separators(False)
        self.table.set_show_row_separators(True)
        self.table.connect('activate', lambda *args: self.widget.set_show_content(True))
        self.sorted.set_sorter(self.table.get_sorter())
        self.table.get_sorter().connect('changed', lambda *args: self._sort_changed())
        for title, width, field in columns:
            factory = Gtk.SignalListItemFactory()
            factory.connect('setup', self._setup_cell, field)
            factory.connect('bind', self._bind_cell, field)
            factory.connect('unbind', self._unbind_cell)
            column = Gtk.ColumnViewColumn.new(title, factory)
            column.set_resizable(True)
            column.set_fixed_width(width)
            column.set_sorter(Gtk.CustomSorter.new(lambda a, b, _data=None, field=field: self.compare(a, b, field)))
            self.columns[field] = column
            self.table.append_column(column)
        popover = Gtk.Popover()
        toggles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.column_toggles = {}
        for title, width, field in columns:
            button = Gtk.CheckButton(label=title, active=True)
            button.set_sensitive(field != 'executable')
            button.connect('toggled', lambda button, field=field: self.columns[field].set_visible(button.get_active()))
            toggles.append(button)
            self.column_toggles[field] = button
        popover.set_child(toggles)
        self.column_menu.set_popover(popover)
        self.table.sort_by_column(self.columns[default_sort], default_order)
        self.scroll = Gtk.ScrolledWindow(child=self.table, vexpand=True)
        for adjustment in (self.scroll.get_vadjustment(), self.scroll.get_hadjustment()):
            adjustment.connect('value-changed', self._scroll_changed)

    def _scroll_changed(self, adjustment):
        # A queued layout restore must never undo a newer scroll interaction.
        if self._scroll_restore_source and not self._updating and not self._restoring_scroll:
            GLib.source_remove(self._scroll_restore_source)
            self._scroll_restore_source = 0

    def _setup_cell(self, factory, item, field):
        box = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
        text = label('', xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        box.append(text)
        box.text, box.artwork = text, None
        item.set_child(box)

    def _bind_cell(self, factory, item, field):
        obj = item.get_item()
        def render(*args):
            box, record = item.get_child(), obj.record
            text = self.display_value(record, field)
            if field == 'executable':
                text = self.application_label(record, obj.identity)
                if box.artwork:
                    box.remove(box.artwork)
                box.artwork = app_image(obj.identity, 24)
                box.prepend(box.artwork)
            elif field == 'pid' and text in ('', None, -1, '-1'):
                text = '—'
            box.text.set_text(str(text))
            box.set_tooltip_text(self.application_tooltip(record) if field == 'executable' else str(text))
        render()
        item.handler = obj.connect('notify::version', render)

    def _unbind_cell(self, factory, item):
        obj = item.get_item()
        if obj and getattr(item, 'handler', None):
            obj.disconnect(item.handler)
            item.handler = None

    def application_label(self, record, identity):
        return identity.display_name if record.executable else getattr(record, 'command', '') or 'Unknown process'

    def application_tooltip(self, record):
        return record.executable

    def display_value(self, record, field):
        return self.raw_value(record, field)

    def compare(self, a, b, field):
        # Friendly labels never change existing raw executable sort semantics.
        left, right = self.raw_value(a.record, field), self.raw_value(b.record, field)
        return (left > right) - (left < right)

    def _sort_changed(self):
        if not self._updating and hasattr(self, 'details'):
            self._updating = True
            self._restore_selection()
            self._updating = False

    def _restore_selection(self):
        for index in range(self.sorted.get_n_items()):
            if self.sorted.get_item(index).record.key == self.selected_key:
                if self.selection.get_selected() != index:
                    self.selection.set_selected(index)
                return
        if self.selection.get_selected_item() is not None:
            self.selection.unselect_all()

    def replace_rows(self, records, resolve, predicate=lambda record, identity: True):
        items = []
        sorter_dirty = False
        keys = {record.key for record in records}
        for key in tuple(self._objects):
            if key not in keys:
                del self._objects[key]
        for record in records:
            identity = resolve(record.node, record.executable)
            obj = self._objects.get(record.key)
            if obj is None:
                obj = RecordItem(record, identity); self._objects[record.key] = obj
            elif (obj.record, obj.identity) != (record, identity):
                sorter_dirty = sorter_dirty or any(self.raw_value(obj.record, field) != self.raw_value(record, field) for field in self.columns)
                obj.record, obj.identity = record, identity
                obj.version += 1
            if predicate(record, identity):
                items.append(obj)
        vertical, horizontal = self.scroll.get_vadjustment().get_value(), self.scroll.get_hadjustment().get_value()
        old = tuple(self.store.get_item(index) for index in range(self.store.get_n_items()))
        spliced = tuple(items) != old
        if spliced:
            prefix = 0
            while prefix < min(len(old), len(items)) and old[prefix] is items[prefix]:
                prefix += 1
            suffix = 0
            while suffix < min(len(old), len(items)) - prefix and old[len(old) - suffix - 1] is items[len(items) - suffix - 1]:
                suffix += 1
            self.store.splice(prefix, len(old) - prefix - suffix, items[prefix:len(items) - suffix if suffix else len(items)])
        self.visible_items = tuple(items)
        if spliced or sorter_dirty:
            self.table.get_sorter().changed(Gtk.SorterChange.DIFFERENT)
        self._restore_selection()
        def restore():
            self._scroll_restore_source = 0
            self._restoring_scroll = True
            for adjustment, position in ((self.scroll.get_vadjustment(), vertical), (self.scroll.get_hadjustment(), horizontal)):
                adjustment.set_value(min(position, max(0, adjustment.get_upper() - adjustment.get_page_size())))
            self._restoring_scroll = False
            return False
        if spliced:
            if self._scroll_restore_source:
                GLib.source_remove(self._scroll_restore_source)
            self._scroll_restore_source = GLib.idle_add(restore)
        return len(items)

    def select(self, *args):
        if not self._updating:
            obj = self.selection.get_selected_item()
            self.selected_key = obj.record.key if obj else None
            self.show_details()

    def restore_layout(self):
        layout = self.backend.preferences.get(self.layout_name)
        columns = layout.get('columns', [])
        if isinstance(columns, list):
            seen = set()
            for entry in columns:
                if not isinstance(entry, dict) or entry.get('field') not in self.columns or entry['field'] in seen:
                    continue
                field = entry['field']; seen.add(field)
                column = self.columns[field]
                width = entry.get('width', 100)
                if isinstance(width, int) and 50 <= width <= 1500:
                    column.set_fixed_width(width)
                self.column_toggles[field].set_active(field == 'executable' or bool(entry.get('visible', True)))
                self.table.remove_column(column); self.table.append_column(column)
        field = layout.get('sort', self.default_sort)
        if field in self.columns:
            self.table.sort_by_column(self.columns[field], Gtk.SortType.DESCENDING if layout.get('descending', self.default_order == Gtk.SortType.DESCENDING) else Gtk.SortType.ASCENDING)

    def save_layout(self):
        columns = self.table.get_columns()
        reverse = {column: field for field, column in self.columns.items()}
        sorter = self.table.get_sorter()
        self.backend.preferences.save(self.layout_name, {'columns': [dict(field=reverse[column], width=column.get_fixed_width(), visible=column.get_visible())
            for column in (columns.get_item(index) for index in range(columns.get_n_items()))],
            'sort': reverse.get(sorter.get_primary_sort_column(), self.default_sort), 'descending': sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING})
