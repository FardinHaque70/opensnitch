"""Service discovery and explicitly executable-scoped application controls."""

from PyQt5 import QtCore, QtGui, QtWidgets

from opensnitch.application_catalog import CatalogEntry
from opensnitch.config import Config
from opensnitch.dialogs.applications import ApplicationRuleReview
from opensnitch.systemd_catalog import SystemdCatalog, ServiceVerificationAdapter, shared_services
from opensnitch.core.services import ServiceAccessController
from opensnitch.utils import Icons


class ServicesView(QtWidgets.QWidget):
    edit_rule_requested = QtCore.pyqtSignal(str, str, str)

    def __init__(self, runtime, node=None, provider=None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.fixed_node = node
        self.provider = provider or SystemdCatalog.instance()
        self.runtime.attach_service_catalog(self.provider)
        self._items = {}
        self._selection = None
        self.service_controls = ServiceAccessController(ServiceVerificationAdapter(self.provider, self),
            self.runtime.controller.request, self.rebuild, self.show_feedback)
        self._review = None
        self._sharing_display = None
        self._rebuilding = False
        self._config = Config.get()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        toolbar = QtWidgets.QHBoxLayout()
        self.nodes = QtWidgets.QComboBox()
        self.nodes.setAccessibleName(self.tr('Service node'))
        self.nodes.currentIndexChanged.connect(self.rebuild)
        self.scope = QtWidgets.QComboBox()
        self.scope.addItem(self.tr('System and user'), '')
        self.scope.addItem(self.tr('System'), 'system')
        self.scope.addItem(self.tr('Current user'), 'user')
        self.scope.setAccessibleName(self.tr('Service manager scope'))
        self.scope.currentIndexChanged.connect(self.rebuild)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText(self.tr('Search services, unit names or executable paths'))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.rebuild)
        self.refresh_button = QtWidgets.QPushButton(Icons.new(self, 'reload'), self.tr('Refresh'))
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.nodes)
        toolbar.addWidget(self.scope)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.refresh_button)
        layout.addLayout(toolbar)
        self.notice = QtWidgets.QLabel(self.tr('Access applies to executable paths, including instances outside these services.'))
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(QtCore.Qt.PlainText)
        self.notice.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        layout.addWidget(self.notice)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels([self.tr('Service'), self.tr('State'), self.tr('Manager'),
                                   self.tr('Executable'), self.tr('Configured access')])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.tree.setAccessibleName(self.tr('System and current-user services'))
        self.tree.setColumnWidth(0, 300)
        self.tree.setColumnWidth(1, 100)
        self.tree.setColumnWidth(2, 100)
        self.tree.setColumnWidth(3, 210)
        header = self._config.getSettings('services/header')
        if isinstance(header, QtCore.QByteArray):
            self.tree.header().restoreState(header)
        self._sort_column = min(4, max(0, self._config.getInt('services/sortColumn')))
        self._descending = self._config.getBool('services/descending')
        self.tree.header().setSectionsClickable(True)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().sectionClicked.connect(self.sort_by)
        self.tree.itemSelectionChanged.connect(self.update_selection)
        self.copy_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence.Copy, self.tree)
        self.copy_shortcut.activated.connect(self.copy_path)
        layout.addWidget(self.tree, 1)
        self.details = QtWidgets.QGroupBox(self.tr('Executable access'))
        self.details.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        details_layout = QtWidgets.QVBoxLayout(self.details)
        details_layout.setContentsMargins(6, 6, 6, 6)
        details_layout.setSpacing(4)
        self.path = QtWidgets.QLabel()
        self.path.setTextFormat(QtCore.Qt.PlainText)
        self.path.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.path.setWordWrap(True)
        self.path.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        details_layout.addWidget(self.path)
        self.shared_label = QtWidgets.QLabel()
        self.shared_label.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        details_layout.addWidget(self.shared_label)
        self.shared_list = QtWidgets.QListWidget()
        self.shared_list.setAccessibleName(self.tr('Known services sharing this executable'))
        self.shared_list.setMaximumHeight(self.fontMetrics().height() * 3 + 14)
        details_layout.addWidget(self.shared_list)
        actions = QtWidgets.QHBoxLayout()
        self.allow = QtWidgets.QPushButton(Icons.new(self, 'emblem-default'), self.tr('Allow'))
        self.block = QtWidgets.QPushButton(Icons.new(self, 'emblem-important'), self.tr('Block'))
        self.defaults = QtWidgets.QPushButton(self.tr('Use defaults'))
        self.duration = QtWidgets.QComboBox()
        for label, value in ((self.tr('Until changed'), 'always'), (self.tr('Until restart'), 'until restart'),
                             (self.tr('1 hour'), '1h'), (self.tr('12 hours'), '12h')):
            self.duration.addItem(label, value)
        self.duration.setAccessibleName(self.tr('Executable access duration'))
        self.manage = QtWidgets.QPushButton(self.tr('Related rules'))
        self.allow.clicked.connect(self.allow_executable)
        self.block.clicked.connect(self.block_executable)
        self.defaults.clicked.connect(self.reset_executable)
        self.manage.clicked.connect(self.open_rules)
        for widget in (self.allow, self.block, self.defaults, self.duration):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.manage)
        details_layout.addLayout(actions)
        layout.addWidget(self.details)
        self.feedback = QtWidgets.QLabel()
        self.feedback.setTextFormat(QtCore.Qt.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        self.feedback.hide()
        layout.addWidget(self.feedback)
        runtime.changed.connect(self.rebuild)
        runtime.controller.message.connect(self.show_feedback)
        self.provider.changed.connect(self.provider_changed)
        self.rebuild()

    def selected_record(self):
        item = self.tree.currentItem()
        key = item.data(0, QtCore.Qt.UserRole) if item else None
        return self.provider.records.get(tuple(key)) if key else None

    def node(self):
        return self.fixed_node or self.nodes.currentData()

    def local_node(self):
        return bool(self.node() and self.runtime.identities.is_local_node(self.node()))

    def rebuild(self, *args):
        if self._rebuilding:
            return
        self._rebuilding = True
        try:
            self._rebuild()
        finally:
            self._rebuilding = False

    def _rebuild(self):
        chosen = self.node()
        known_nodes = list(self.runtime.nodes.get_nodes())
        if self.fixed_node and self.fixed_node not in known_nodes:
            known_nodes.append(self.fixed_node)
        with QtCore.QSignalBlocker(self.nodes):
            self.nodes.clear()
            for node in known_nodes:
                data = self.runtime.nodes.get_node(node)
                config = data.get('data') if data else None
                self.nodes.addItem(getattr(config, 'name', '') or node, node)
            index = self.nodes.findData(chosen)
            if index < 0:
                index = next((i for i in range(self.nodes.count())
                              if self.runtime.identities.is_local_node(self.nodes.itemData(i))), 0)
            self.nodes.setCurrentIndex(index)
        self.nodes.setEnabled(self.fixed_node is None)
        local = self.local_node()
        self.refresh_button.setEnabled(local)
        self.provider.set_active(local and self.isVisible())
        if not self.node():
            notice = self.tr('Connect a local OpenSnitch node to view its services.')
        elif not local:
            notice = self.tr('Service discovery is available for this computer only. Remote service identities are not inferred.')
        elif self.provider.errors:
            notice = ' · '.join(self.provider.errors.values())
        elif not self.provider.generation:
            notice = self.tr('Reading system and current-user services…')
        else:
            notice = self.tr('Access applies to executable paths and all their instances. Service state is separate from network access.')
        self.notice.setText(notice)
        current = self.selected_record()
        key = current.key if current else None
        anchor = self.tree.itemAt(0, 0)
        offset = self.tree.visualItemRect(anchor).top() if anchor else 0
        scroll = self.tree.verticalScrollBar().value()
        search = self.search.text().casefold()
        scope = self.scope.currentData()
        rows = []
        for record in self.provider.records.values() if local else ():
            if scope and scope != record.scope:
                continue
            if search and search not in ('\n'.join((record.description, record.unit_name,
                                                     record.configured_executable, record.executable_path))).casefold():
                continue
            identity = self.runtime.identities.resolve(self.node() if record.executable_path else None,
                                                       record.executable_path or record.configured_executable)
            state = {'active': self.tr('Running') if record.sub_state == 'running' else self.tr('Active'),
                     'inactive': self.tr('Inactive'), 'failed': self.tr('Failed'),
                     'activating': self.tr('Starting'), 'deactivating': self.tr('Stopping'),
                     'template': self.tr('Template')}.get(record.active_state, record.active_state)
            access = self.tr('Read-only')
            if record.executable_path:
                summary = self.runtime.policies.summarize(self.node(), record.executable_path)
                access = self.tr(summary.group)
                entry = self.runtime.catalog.entries.get((self.node(), record.executable_path))
                if entry and entry.ownership.get('uncertain'):
                    access = self.tr('Unconfirmed — review rules')
                if self.node() not in self.runtime.policies.rules or self.node() in self.runtime.errors:
                    access = self.tr('Policy unavailable')
                elif (self.node(), record.executable_path) in self.runtime.controller.pending:
                    access = self.tr('Applying…')
                elif self.service_controls.pending and self.provider.records.get(self.service_controls.pending) and self.provider.records[self.service_controls.pending].executable_path == record.executable_path:
                    access = self.tr('Verifying executable…')
                elif not self.runtime.online(self.node()):
                    access = self.tr('Node disconnected')
            columns = [record.description or record.unit_name, state,
                       self.tr('System') if record.scope == 'system' else self.tr('User'),
                       identity.display_name if record.configured_executable else self.tr('Unresolved'), access]
            rows.append((record, identity, columns))
        wanted = {row[0].key for row in rows}
        with QtCore.QSignalBlocker(self.tree):
            self.tree.setUpdatesEnabled(False)
            for removed in set(self._items) - wanted:
                item = self._items.pop(removed)
                if item is anchor:
                    anchor = None
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            for position, (record, identity, columns) in enumerate(sorted(rows,
                    key=lambda row: (row[2][self._sort_column].casefold(), row[0].key), reverse=self._descending)):
                item = self._items.get(record.key)
                if item is None:
                    item = QtWidgets.QTreeWidgetItem()
                    item.setData(0, QtCore.Qt.UserRole, record.key)
                    self._items[record.key] = item
                previous = self.tree.indexOfTopLevelItem(item)
                if previous != position:
                    if previous >= 0:
                        self.tree.takeTopLevelItem(previous)
                    self.tree.insertTopLevelItem(position, item)
                for column, value in enumerate(columns):
                    if item.text(column) != value:
                        item.setText(column, value)
                    item.setToolTip(column, record.reason if column == 4 and record.reason else
                                    '\n'.join(filter(None, (record.description, record.unit_name,
                                                           record.executable_path or record.configured_executable))))
                item.setIcon(0, self.runtime.identities.icon_for(identity, self))
            if key in self._items and self.tree.currentItem() is not self._items[key]:
                self.tree.setCurrentItem(self._items[key])
            self.tree.header().setSortIndicator(self._sort_column,
                QtCore.Qt.DescendingOrder if self._descending else QtCore.Qt.AscendingOrder)
            self.tree.doItemsLayout()
            if anchor is not None:
                self.tree.scrollToItem(anchor, QtWidgets.QAbstractItemView.PositionAtTop)
                self.tree.verticalScrollBar().setValue(self.tree.verticalScrollBar().value() - offset)
            else:
                self.tree.verticalScrollBar().setValue(scroll)
            self.tree.setUpdatesEnabled(True)
        self.update_selection()

    def update_selection(self):
        record = self.selected_record()
        selection = (self.node(), record.key) if record else None
        if selection != self._selection:
            self._selection = selection
            self.show_feedback('')
        self.details.setVisible(record is not None)
        if record is None:
            return
        path = record.executable_path or record.configured_executable
        self.path.setText('{}\n{}'.format(record.unit_name, path or self.tr('Executable unresolved')))
        self.path.setToolTip('\n'.join(record.aliases))
        sharing = shared_services(self.provider.records, record.executable_path)
        self.shared_label.setText(self.tr('Known services sharing this executable ({0}):').format(len(sharing))
                                  if len(sharing) > 1 else self.tr('Applies to this executable, including processes outside this service.'))
        if self._sharing_display != sharing:
            self._sharing_display = sharing
            self.shared_list.clear()
            for scope, name in sharing:
                self.shared_list.addItem((self.tr('System') if scope == 'system' else self.tr('User')) + ' · ' + name)
        self.shared_list.setVisible(len(sharing) > 1)
        entry = self.runtime.catalog.entries.get((self.node(), record.executable_path))
        reason = record.reason
        if not reason and entry:
            reason = self.runtime.controller.reason(entry)
        if self.node() in self.runtime.errors:
            reason = self.runtime.errors[self.node()]
        if self.service_controls.pending:
            reason = self.tr('Verifying the service executable and shared services…')
        enabled = bool(record.executable_path and entry and not reason)
        for button in (self.allow, self.block, self.defaults):
            button.setEnabled(enabled)
            button.setToolTip(reason or self.tr('Changes access for this executable and all of its instances.'))
        self.duration.setEnabled(enabled)
        self.defaults.setEnabled(enabled and bool(self.runtime.policies.application_rules(self.node(), record.executable_path)))
        self.manage.setEnabled(bool(path))
        if reason:
            self.path.setText(self.path.text() + '\n' + reason)

    def provider_changed(self):
        self.rebuild()

    def change_access(self, action):
        record = self.selected_record()
        if not record or not record.executable_path or record.reason or self.service_controls.pending or not self.local_node():
            return
        entry = self.runtime.catalog.entries.get((self.node(), record.executable_path))
        if entry is None:
            return
        reason = self.runtime.controller.reason(entry)
        if reason:
            self.show_feedback(reason)
            return
        self.show_feedback(self.tr('Verifying executable access…'))
        self.service_controls.request(record.key, self.node(), action, self.duration.currentData())

    def allow_executable(self):
        self.change_access('allow')

    def block_executable(self):
        self.change_access('deny')

    def reset_executable(self):
        self.change_access('defaults')

    def open_rules(self):
        record = self.selected_record()
        if record is None:
            return
        path = record.executable_path or record.configured_executable
        if not path:
            return
        if self._review is not None:
            self._review.close()
            self._review.deleteLater()
        entry = self.runtime.catalog.entries.get((self.node(), path)) or CatalogEntry(self.node(), path)
        identity = self.runtime.identities.resolve(self.node() if record.executable_path else None, path)
        self._review = ApplicationRuleReview(self.runtime, entry, self, identity=identity)
        if not record.executable_path:
            self._review.explanation.setText(self.tr('These rules match the configured path. The service executable association is uncertain; review the conditions before saving.'))
        self._review.edit_requested.connect(self.edit_rule_requested)
        self._review.open()

    def show_feedback(self, text):
        self.feedback.setText(text)
        self.feedback.setVisible(bool(text))

    def sort_by(self, column):
        self._descending = not self._descending if column == self._sort_column else False
        self._sort_column = column
        self.rebuild()

    def copy_path(self):
        record = self.selected_record()
        if record:
            QtWidgets.QApplication.clipboard().setText(record.executable_path or record.configured_executable or record.unit_name)

    def export_rows(self):
        yield [self.tree.headerItem().text(i) for i in range(5)] + [self.tr('Unit name'), self.tr('Executable path')]
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            record = self.provider.records[tuple(item.data(0, QtCore.Qt.UserRole))]
            yield [item.text(i) for i in range(5)] + [record.unit_name, record.executable_path or record.configured_executable]

    def save_settings(self):
        self._config.setSettings('services/header', self.tree.header().saveState())
        self._config.setSettings('services/sortColumn', self._sort_column)
        self._config.setSettings('services/descending', self._descending)

    def refresh(self):
        self.provider.refresh(fresh=True)

    def showEvent(self, event):
        super().showEvent(event)
        self.provider.set_active(self.local_node())

    def hideEvent(self, event):
        self.provider.set_active(False)
        super().hideEvent(event)
