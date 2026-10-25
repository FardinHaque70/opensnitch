"""App-first inventory view. Existing rule and activity editors remain intact."""

from datetime import datetime
import os

from PyQt5 import QtCore, QtGui, QtWidgets

from opensnitch.application_policy import ApplicationPolicyService, references_executable
from opensnitch.config import Config
from opensnitch.utils import Icons


class ApplicationRuleReview(QtWidgets.QDialog):
    edit_requested = QtCore.pyqtSignal(str, str, str)

    def __init__(self, runtime, entry, parent, identity=None):
        super().__init__(parent)
        self.key = entry.key
        identity = identity or runtime.identities.resolve(*entry.key)
        self.setWindowTitle(self.tr('Rules for {0}').format(identity.display_name))
        self.setWindowIcon(runtime.identities.icon_for(identity, self))
        self.resize(680, 290)
        layout = QtWidgets.QVBoxLayout(self)
        text = self.tr('Review application rules here. Changes take effect when saved in the rule editor.')
        self.explanation = QtWidgets.QLabel(text)
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        path = QtWidgets.QLabel(entry.executable_path)
        path.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        path.setWordWrap(True)
        layout.addWidget(path)
        self.rules = QtWidgets.QTreeWidget()
        self.rules.setHeaderLabels([self.tr('Access'), self.tr('Duration'), self.tr('Scope'), self.tr('Rule name')])
        self.rules.setRootIsDecorated(False)
        self.rules.setUniformRowHeights(True)
        self.rules.setColumnWidth(1, 130)
        self.rules.setColumnWidth(2, 160)
        self.rules.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.rules.setAccessibleName(self.tr('Existing application rules'))
        for rule in runtime.policies.rules.get(entry.node_id, ()):
            if not references_executable(rule.operator, entry.executable_path):
                continue
            access = {'allow': self.tr('Allow'), 'deny': self.tr('Block'), 'reject': self.tr('Reject')}.get(rule.action, rule.action)
            if not rule.enabled:
                access += self.tr(' (disabled)')
            simple = rule.operator.get('type') == 'simple' and rule.operator.get('operand') == 'process.path'
            scope = self.tr('Application only') if simple else self.tr('Custom conditions')
            item = QtWidgets.QTreeWidgetItem(self.rules, [access,
                self.tr('Until changed') if rule.duration == 'always' else rule.duration, scope, rule.name])
            item.setData(0, QtCore.Qt.UserRole, rule.name)
            item.setToolTip(3, rule.name)
        layout.addWidget(self.rules, 1)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        self.edit_button = self.buttons.addButton(self.tr('Edit selected rule…'), QtWidgets.QDialogButtonBox.ActionRole)
        self.edit_button.setEnabled(self.rules.topLevelItemCount() > 0)
        self.edit_button.clicked.connect(self.edit_rule)
        self.buttons.rejected.connect(self.reject)
        self.rules.itemDoubleClicked.connect(self.edit_rule)
        layout.addWidget(self.buttons)
        if self.rules.topLevelItemCount():
            self.rules.setCurrentItem(self.rules.topLevelItem(0))
        else:
            if entry.ownership.get('uncertain'):
                self.explanation.setText(self.tr('The previous operation is unconfirmed and no related rule is available in the current snapshot. Reconnect the UI to refresh its rules before changing access.'))
            else:
                self.explanation.setText(self.tr('No application rules are configured for this executable. Global rules may still apply.'))

    def edit_rule(self, *args):
        item = self.rules.currentItem()
        if item is not None:
            name = item.data(0, QtCore.Qt.UserRole)
            self.accept()
            self.edit_requested.emit(self.key[0], self.key[1], name)


class ApplicationsView(QtWidgets.QWidget):
    edit_rule_requested = QtCore.pyqtSignal(str, str, str)
    activity_requested = QtCore.pyqtSignal(str, str)

    def __init__(self, runtime, node=None, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        self.fixed_node = node
        self.search = ''
        self._rule_review = None
        self._selection_key = None
        self._settings_prefix = 'apps/catalog/' + (node or 'all') + '/'
        self._config = Config.get()
        self._sort_column = min(3, max(0, self._config.getInt(self._settings_prefix + 'sortColumn')))
        self._descending = self._config.getBool(self._settings_prefix + 'descending')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.notice = QtWidgets.QLabel(self.tr('Configured access'))
        self.notice.setToolTip(self.tr('Groups show configured application rules. Global rules and interception settings may override them; they do not prove internet reachability.'))
        self.notice.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        top = QtWidgets.QHBoxLayout()
        self.nodes = QtWidgets.QComboBox()
        self.nodes.setAccessibleName(self.tr('Application node'))
        self.nodes.currentIndexChanged.connect(self.rebuild)
        top.addWidget(self.nodes)
        self.add_button = QtWidgets.QPushButton(Icons.new(self, 'document-new'), self.tr('Add app'))
        self.add_button.clicked.connect(self.add_application)
        top.addWidget(self.add_button)
        self.show_hidden = QtWidgets.QCheckBox(self.tr('Show hidden'))
        self.show_hidden.toggled.connect(self.rebuild)
        top.addWidget(self.show_hidden)
        top.addStretch()
        top.addWidget(self.notice)
        layout.addLayout(top)
        actions = QtWidgets.QHBoxLayout()
        self.allow = QtWidgets.QPushButton(Icons.new(self, 'emblem-default'), self.tr('Allow'))
        self.block = QtWidgets.QPushButton(Icons.new(self, 'emblem-important'), self.tr('Block'))
        self.defaults = QtWidgets.QPushButton(self.tr('Use defaults'))
        self.duration = QtWidgets.QComboBox()
        for text, value in ((self.tr('Until changed'), 'always'), (self.tr('Until restart'), 'until restart'),
                            (self.tr('1 hour'), '1h'), (self.tr('12 hours'), '12h')):
            self.duration.addItem(text, value)
        self.duration.setAccessibleName(self.tr('Application access duration'))
        self.manage = QtWidgets.QPushButton(self.tr('Manage rules'))
        self.activity = QtWidgets.QPushButton(self.tr('Activity'))
        self.hide_button = QtWidgets.QPushButton(self.tr('Hide'))
        self.allow.clicked.connect(lambda: self.change_access('allow'))
        self.block.clicked.connect(lambda: self.change_access('deny'))
        self.defaults.clicked.connect(lambda: self.change_access('defaults'))
        self.manage.clicked.connect(self.open_rules)
        self.activity.clicked.connect(self.open_activity)
        self.hide_button.clicked.connect(self.hide_application)
        for button in (self.allow, self.block, self.defaults):
            actions.addWidget(button)
        actions.addWidget(self.duration)
        actions.addStretch()
        for button in (self.manage, self.activity, self.hide_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels([self.tr('Application'), self.tr('Configured access'), self.tr('Last activity'), self.tr('Node')])
        self.tree.setUniformRowHeights(True)
        self.tree.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tree.setAccessibleName(self.tr('Applications grouped by configured access'))
        self.tree.setColumnWidth(0, 320)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 160)
        header_state = self._config.getSettings(self._settings_prefix + 'header')
        if isinstance(header_state, QtCore.QByteArray):
            self.tree.header().restoreState(header_state)
        self.tree.header().setSectionsClickable(True)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().sectionClicked.connect(self.sort_by)
        self.tree.itemSelectionChanged.connect(self.update_selection)
        self.copy_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence.Copy, self.tree)
        self.copy_shortcut.activated.connect(self.copy_path)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.open_activity())
        self._groups = {}
        self._items = {}
        for name in ('Unavailable',) + ApplicationPolicyService.GROUPS:
            group = QtWidgets.QTreeWidgetItem([self.tr(name)])
            group.setData(0, QtCore.Qt.UserRole + 1, name)
            self.tree.addTopLevelItem(group)
            group.setFirstColumnSpanned(True)
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setExpanded(True)
            self._groups[name] = group
        layout.addWidget(self.tree, 1)
        self.details_toggle = QtWidgets.QToolButton()
        self.details_toggle.setText(self.tr('Details'))
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(QtCore.Qt.RightArrow)
        self.details_toggle.toggled.connect(self.toggle_details)
        layout.addWidget(self.details_toggle, 0, QtCore.Qt.AlignLeft)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(180)
        self.details.setAccessibleName(self.tr('Selected application details'))
        self.details.hide()
        layout.addWidget(self.details)
        self.feedback = QtWidgets.QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        self.feedback.hide()
        layout.addWidget(self.feedback)
        runtime.changed.connect(self.rebuild)
        runtime.controller.message.connect(self.show_feedback)
        runtime.catalog.error.connect(self.show_feedback)
        if runtime.catalog.load_error:
            self.show_feedback(self.tr('Inventory is read-only: ') + runtime.catalog.load_error)
        self.rebuild()

    @QtCore.pyqtSlot(str)
    def show_feedback(self, text):
        self.feedback.setText(text)
        self.feedback.setVisible(bool(text))

    def selected_key(self):
        item = self.tree.currentItem()
        value = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        return tuple(value) if value is not None else None

    def save_settings(self):
        self._config.setSettings(self._settings_prefix + 'header', self.tree.header().saveState())
        self._config.setSettings(self._settings_prefix + 'sortColumn', self._sort_column)
        self._config.setSettings(self._settings_prefix + 'descending', self._descending)

    def set_search(self, text):
        self.search = text.casefold()
        self.rebuild()

    def node_label(self, node):
        data = self.runtime.nodes.get_node(node)
        config = data.get('data') if data else None
        return getattr(config, 'name', '') or node

    def rebuild(self, *args):
        key = self.selected_key()
        scroll = self.tree.verticalScrollBar().value()
        anchor = self.tree.itemAt(0, 0)
        anchor_offset = self.tree.visualItemRect(anchor).top() if anchor else 0
        selected_node = self.nodes.currentData()
        self.nodes.blockSignals(True)
        self.nodes.clear()
        self.nodes.addItem(self.tr('All nodes'), '')
        nodes = sorted({entry.node_id for entry in self.runtime.catalog.entries.values()} | set(self.runtime.nodes.get_nodes()))
        for node in nodes:
            self.nodes.addItem(self.node_label(node), node)
        self.nodes.setCurrentIndex(max(0, self.nodes.findData(self.fixed_node or selected_node)))
        self.nodes.setEnabled(self.fixed_node is None)
        self.nodes.blockSignals(False)
        self.tree.blockSignals(True)
        self.tree.setUpdatesEnabled(False)
        entries = []
        node_filter = self.fixed_node or self.nodes.currentData()
        for entry in self.runtime.catalog.entries.values():
            # Service discovery alone should not fill Apps with inactive system
            # programs. Desktop, observed, rule and manual sources remain visible.
            if entry.sources == ['services']:
                continue
            if node_filter and node_filter != entry.node_id:
                continue
            if entry.hidden and not self.show_hidden.isChecked():
                continue
            identity = self.runtime.identities.resolve(*entry.key)
            if self.search and self.search not in (identity.display_name + '\n' + entry.executable_path).casefold():
                continue
            summary = self.runtime.policies.summarize(*entry.key)
            group = summary.group if entry.node_id in self.runtime.policies.rules and entry.node_id not in self.runtime.errors else 'Unavailable'
            access = self.tr(group)
            if entry.key in self.runtime.controller.pending:
                access = self.tr('Applying…')
            elif entry.node_id in self.runtime.errors:
                access = self.tr('Policy unavailable')
            elif entry.ownership.get('uncertain'):
                access = self.tr('Unconfirmed — review rules')
            elif not self.runtime.online(entry.node_id):
                access = self.tr('Last known: ') + access if group != 'Unavailable' else self.tr('Node disconnected')
            elif len(summary.rules) == 1 and group != 'Custom rules':
                rule = summary.rules[0]
                access = {'allow': self.tr('Allow'), 'deny': self.tr('Block'), 'reject': self.tr('Reject')}.get(rule.action, rule.action)
                access += ' · ' + (self.tr('until changed') if rule.duration == 'always' else rule.duration)
            recent = datetime.fromtimestamp(entry.last_seen).strftime('%b %d %H:%M') if entry.last_seen else (
                self.tr('Observed') if 'observed' in entry.sources else self.tr('Not observed'))
            name = identity.display_name
            if self.runtime.availability.get(entry.key) is False:
                name += self.tr(' (unavailable)')
            if entry.hidden:
                name += self.tr(' (hidden)')
            entries.append((entry, identity, group, [name, access, recent, self.node_label(entry.node_id)]))
        def sort_key(row):
            if self._sort_column == 2:
                return row[0].last_seen
            return row[3][self._sort_column].casefold(), row[0].executable_path
        wanted = {row[0].key for row in entries}
        for removed in set(self._items) - wanted:
            item = self._items.pop(removed)
            if item is anchor:
                anchor = None
            item.parent().removeChild(item)
        positions = dict.fromkeys(self._groups, 0)
        for entry, identity, group, columns in sorted(entries, key=sort_key, reverse=self._descending):
            item = self._items.get(entry.key)
            parent = self._groups[group]
            position = positions[group]
            positions[group] += 1
            if item is None:
                item = QtWidgets.QTreeWidgetItem()
                item.setData(0, QtCore.Qt.UserRole, entry.key)
                self._items[entry.key] = item
            if item.parent() is not parent or parent.indexOfChild(item) != position:
                if item.parent() is not None:
                    item.parent().takeChild(item.parent().indexOfChild(item))
                parent.insertChild(position, item)
            for column, text in enumerate(columns):
                if item.text(column) != text:
                    item.setText(column, text)
            item.setToolTip(0, identity.tooltip)
            icon = self.runtime.identities.icon_for(identity, self)
            if item.icon(0).cacheKey() != icon.cacheKey():
                item.setIcon(0, icon)
        for name, group in self._groups.items():
            count = group.childCount()
            group.setText(0, '{} ({})'.format(self.tr(name), count))
            group.setHidden(count == 0)
        if key in self._items and self.tree.currentItem() is not self._items[key]:
            self.tree.setCurrentItem(self._items[key])
        self.tree.header().setSortIndicator(self._sort_column, QtCore.Qt.DescendingOrder if self._descending else QtCore.Qt.AscendingOrder)
        self.tree.doItemsLayout()
        if anchor is not None and not anchor.isHidden() and (anchor.parent() is None or anchor.parent().isExpanded()):
            self.tree.scrollToItem(anchor, QtWidgets.QAbstractItemView.PositionAtTop)
            self.tree.verticalScrollBar().setValue(self.tree.verticalScrollBar().value() - anchor_offset)
        else:
            self.tree.verticalScrollBar().setValue(scroll)
        self.tree.setUpdatesEnabled(True)
        self.tree.blockSignals(False)
        self.update_selection()

    def sort_by(self, column):
        self._descending = not self._descending if column == self._sort_column else False
        self._sort_column = column
        self.rebuild()

    def copy_path(self):
        key = self.selected_key()
        if key:
            QtWidgets.QApplication.clipboard().setText(key[1])

    def export_rows(self):
        yield [self.tr('Application'), self.tr('Configured access'), self.tr('Last activity'),
               self.tr('Node'), self.tr('Executable path')]
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            for child_index in range(group.childCount()):
                item = group.child(child_index)
                key = item.data(0, QtCore.Qt.UserRole)
                yield [item.text(column) for column in range(4)] + [key[1]]

    def update_selection(self):
        key = self.selected_key()
        if key != self._selection_key:
            self._selection_key = key
            self.show_feedback('')
        entry = self.runtime.catalog.entries.get(key)
        reason = self.runtime.controller.reason(entry) if entry else self.tr('Select an application')
        if entry and entry.node_id in self.runtime.errors:
            reason = self.runtime.errors[entry.node_id]
        if entry and not self.runtime.identities.is_local_node(entry.node_id):
            reason = self.tr('Use the node rule editor for remote changes.')
        custom = bool(entry and self.runtime.policies.quick_control_reason(entry))
        unavailable = (not entry or not self.runtime.online(entry.node_id)
                       or entry.key in self.runtime.controller.pending
                       or entry.node_id in self.runtime.errors
                       or self.runtime.catalog.read_only)
        for button in (self.allow, self.block, self.defaults):
            button.setEnabled(not unavailable)
            button.setToolTip(reason
                              or self.tr('Applies to this executable and all its instances. Other rules may override it.'))
        related = entry and any(references_executable(rule.operator, entry.executable_path)
                               for rule in self.runtime.policies.rules.get(entry.node_id, ()))
        self.defaults.setEnabled(not unavailable and bool(entry.ownership or related) if entry else False)
        if entry and not self.defaults.isEnabled() and not unavailable:
            self.defaults.setToolTip(self.tr('This application already uses defaults.'))
        self.duration.setEnabled(not unavailable)
        self.notice.setText(self.tr('Custom policy — use Manage rules') if custom else self.tr('Configured access'))
        self.notice.setToolTip((reason + '\n' if reason else '')
                               + self.tr('Groups show configured policies. Global rules and interception settings may override them; they do not prove internet reachability.'))
        for button in (self.manage, self.activity, self.hide_button):
            button.setEnabled(entry is not None)
        self.hide_button.setText(self.tr('Unhide') if entry and entry.hidden else self.tr('Hide'))
        if entry:
            summary = self.runtime.policies.summarize(*key)
            lines = [entry.executable_path, self.tr('Node: ') + self.node_label(entry.node_id),
                     self.tr('Sources: ') + ', '.join(entry.sources), reason,
                     self.tr('Related application rules:')]
            lines.extend('{} — {} — {}'.format(rule.name, rule.action, rule.duration) for rule in summary.rules)
            lines.append(self.tr('Additional global/system rules: ') + str(len(summary.global_rules)))
            self.details.setPlainText('\n'.join(filter(None, lines)))
        else:
            self.details.clear()

    def toggle_details(self, checked):
        self.details.setVisible(checked)
        self.details_toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

    def change_access(self, action):
        key = self.selected_key()
        if key:
            entry = self.runtime.catalog.entries[key]
            if not self.runtime.identities.is_local_node(entry.node_id):
                self.show_feedback(self.tr('Use Manage rules for remote nodes.'))
                return
            self.runtime.controller.request(key, action, self.duration.currentData())

    def open_rules(self):
        key = self.selected_key()
        if key:
            if self._rule_review is not None:
                self._rule_review.close()
                self._rule_review.deleteLater()
            self._rule_review = ApplicationRuleReview(self.runtime, self.runtime.catalog.entries[key], self)
            self._rule_review.edit_requested.connect(self.edit_rule_requested)
            self._rule_review.open()

    def open_activity(self):
        key = self.selected_key()
        if key:
            self.activity_requested.emit(*key)

    def hide_application(self):
        key = self.selected_key()
        if key:
            self.runtime.catalog.hide(key, not self.runtime.catalog.entries[key].hidden)

    def add_application(self):
        node = self.fixed_node or self.nodes.currentData()
        if not node:
            choices = list(self.runtime.nodes.get_nodes())
            node = choices[0] if len(choices) == 1 else None
        if not node:
            self.show_feedback(self.tr('Select a node before adding an application.'))
            return
        if self.runtime.identities.is_local_node(node):
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, self.tr('Select application executable'))
        else:
            path, accepted = QtWidgets.QInputDialog.getText(self, self.tr('Add app'), self.tr('Absolute executable path on this node:'))
            if not accepted:
                return
        if path and os.path.isabs(path):
            self.runtime.catalog.observe_many(node, [path], 'manual')
