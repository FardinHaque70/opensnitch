"""Standalone isolated Apps UI smoke check; run with offscreen Qt and temporary XDG_CONFIG_HOME."""
import os
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

# Never fall back to the user's real catalog/settings when run directly.
_isolated_config = tempfile.TemporaryDirectory(prefix='opensnitch-app-smoke-')
os.environ['XDG_CONFIG_HOME'] = _isolated_config.name

from PyQt5 import QtCore, QtGui, QtWidgets, QtTest

app = QtWidgets.QApplication([])
if os.environ.get('APP_FIRST_DARK'):
    palette = app.palette()
    for role, color in ((QtGui.QPalette.Window, '#25282c'), (QtGui.QPalette.Base, '#191c20'),
                        (QtGui.QPalette.AlternateBase, '#292d33'), (QtGui.QPalette.Button, '#30343a'),
                        (QtGui.QPalette.Text, '#eeeeee'), (QtGui.QPalette.WindowText, '#eeeeee'),
                        (QtGui.QPalette.ButtonText, '#eeeeee'), (QtGui.QPalette.Highlight, '#345f8b'),
                        (QtGui.QPalette.HighlightedText, '#ffffff')):
        palette.setColor(role, QtGui.QColor(color))
    app.setPalette(palette)
from opensnitch.config import Config
from opensnitch.database import Database
from opensnitch.nodes import Nodes
from opensnitch.rules import Rules
from opensnitch.plugins import PluginsManager
from opensnitch.actions import Actions
from opensnitch.application_identity import ApplicationIdentityService
from opensnitch.application_runtime import ApplicationRuntime, to_proto
from opensnitch.application_policy import ApplicationRule
from opensnitch.systemd_catalog import SystemdCatalog, ServiceIdentity
from opensnitch.dialogs.stats import StatsDialog
import opensnitch.proto as proto
pb, _ = proto.import_()

Config.init()
db = Database.instance()
db.initialize()
PluginsManager.instance().load_plugins()
Actions.instance().loadAll()

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    desktop = root / 'applications'
    desktop.mkdir()
    binaries = root / 'bin'
    binaries.mkdir()
    paths = []
    for name, color in [('Browser', '#3985ce'), ('Music', '#ae489b'), ('Updater', '#238b62'),
                         ('Sync', '#d18d27'), ('Editor', '#54616e')]:
        executable = binaries / name.lower()
        executable.write_text('')
        paths.append(str(executable))
        icon = root / (name + '.png')
        image = QtGui.QImage(48, 48, QtGui.QImage.Format_ARGB32)
        image.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(image)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(QtGui.QColor(color)); painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(3, 3, 42, 42, 8, 8)
        painter.setPen(QtGui.QColor('white')); painter.setFont(QtGui.QFont('Sans', 22))
        painter.drawText(image.rect(), QtCore.Qt.AlignCenter, name[0]); painter.end()
        image.save(str(icon))
        (desktop / (name + '.desktop')).write_text(
            '[Desktop Entry]\nName=Example ' + name + '\nExec=' + str(executable) + '\nIcon=' + str(icon) + '\n')
    identities = ApplicationIdentityService(paths=(str(desktop),), icon_paths=())
    ApplicationIdentityService._instance = identities
    def op(path):
        return {'type': 'simple', 'operand': 'process.path', 'data': path, 'sensitive': True, 'list': []}
    records = [ApplicationRule('browser-rule', 'allow', 'always', True, False, op(paths[0]), int(time.time())),
               ApplicationRule('music-rule', 'deny', 'always', True, False, op(paths[1]), int(time.time())),
               ApplicationRule('updater-rule', 'allow', '12h', True, False, op(paths[2]), int(time.time())),
               ApplicationRule('sync-rule', 'allow', 'always', True, False,
                   {'type': 'list', 'operand': '', 'data': '', 'sensitive': False,
                    'list': [op(paths[3]), {'type': 'simple', 'operand': 'dest.port', 'data': '443', 'sensitive': False, 'list': []}]}, int(time.time()))]
    rule_directory = root / 'rules'
    rule_directory.mkdir()
    for record in records:
        (rule_directory / (record.name + '.json')).write_text(json.dumps(record.__dict__))
    config = pb.ClientConfig(name='Local computer', config=json.dumps({'Rules': {'Path': str(rule_directory)}}), rules=[to_proto(r) for r in records])
    Nodes.instance().add('unix:/local', config)
    class FixtureServices(SystemdCatalog):
        def __init__(self):
            super().__init__(auto_refresh=False)
            self.next_records = None
            self.queued = None

        def refresh(self, fresh=False):
            if not fresh:
                return self.generation
            self._serial += 1
            records = self.next_records if self.next_records is not None else dict(self.records)
            self.next_records = None
            self.queued = self._serial, records, dict(self.errors)
            QtCore.QTimer.singleShot(0, self.deliver)
            return self._serial

        @QtCore.pyqtSlot()
        def deliver(self):
            if self.queued:
                queued, self.queued = self.queued, None
                self._accept(*queued)

    service_provider = FixtureServices()
    service_records = [
        ServiceIdentity('system', 'browser-worker.service', 'Browser background service', 'active', 'running',
                        configured_executable=paths[0], executable_path=paths[0]),
        ServiceIdentity('user', 'user-worker.service', 'User background service', 'inactive', 'dead',
                        configured_executable=paths[0], executable_path=paths[0]),
        ServiceIdentity('system', 'music.service', 'Music background service', 'inactive', 'dead',
                        configured_executable=paths[1], executable_path=paths[1]),
        ServiceIdentity('system', 'script.service', 'Script background service', 'active', 'running',
                        configured_executable='/usr/bin/python3', reason='Shared interpreter; review rules manually.'),
        ServiceIdentity('system', 'only-daemon.service', 'Service-only discovery', 'inactive',
                        configured_executable=str(binaries / 'only-daemon'), executable_path=str(binaries / 'only-daemon')),
        ServiceIdentity('system', 'isolated.service', 'Isolated filesystem service', 'inactive',
                        configured_executable=paths[0], reason='Filesystem mapping makes the host executable uncertain.')]
    service_provider._serial = 1
    service_provider._accept(1, {record.key: record for record in service_records}, {})
    SystemdCatalog._instance = service_provider
    dialog = StatsDialog(db=db, appicon=QtGui.QIcon())
    runtime = ApplicationRuntime.instance()
    sent = []
    runtime.controller.transport = lambda *args: sent.append(args)
    def acknowledge():
        node, operation, rule, callback = sent[-1]
        filename = rule_directory / (rule.name + '.json')
        if operation == 'delete':
            filename.unlink(missing_ok=True)
        else:
            filename.write_text(json.dumps(rule.__dict__))
        callback(True, '')
        app.processEvents()
    deadline = time.monotonic() + 5
    while identities._worker is not None and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.01)
    runtime.sync_installed()
    runtime.catalog.observe_many('unix:/local', [paths[0], paths[3]], 'observed', time.time())
    dialog.resize(1150, 740); dialog.show(); app.processEvents()
    view = dialog._apps_view
    assert dialog._navigation.bar.tabText(0) == 'Apps'
    assert dialog._navigation.bar.tabData(0) == dialog.TAB_PROCS
    assert dialog.tabWidget.widget(dialog.TAB_PROCS) is dialog.tab_7
    assert [dialog._navigation.bar.tabText(i) for i in range(5)] == ['Apps', 'Services', 'Connections', 'Activity', 'Advanced']
    dialog._navigation.show_advanced()
    assert dialog._navigation.extra_active('advanced')
    assert not dialog._export_csv_action.isEnabled()
    assert not dialog.newRuleButton.isVisible() and not dialog.fwButton.isVisible()
    dialog._advanced_view.buttons[dialog.TAB_NODES].click()
    assert dialog.tabWidget.currentIndex() == dialog.TAB_NODES
    assert dialog._navigation.bar.currentIndex() == 4
    assert 'Nodes' in dialog._navigation.context_label.text()
    dialog.tabWidget.setCurrentIndex(dialog.TAB_PROCS)
    assert dialog._navigation.bar.currentIndex() == 0
    dialog.activateWindow(); view.tree.setFocus(); app.processEvents()
    QtTest.QTest.keyClick(view.tree, QtCore.Qt.Key_Tab, QtCore.Qt.ControlModifier)
    assert dialog._navigation.extra_active('services')
    assert not dialog.navToolBar.isVisible()
    QtTest.QTest.keyClick(dialog._services_view.search, QtCore.Qt.Key_Tab, QtCore.Qt.ControlModifier)
    assert dialog.tabWidget.currentIndex() == dialog.TAB_NETSTAT
    QtTest.QTest.keyClick(dialog.netstatTable, QtCore.Qt.Key_Tab, QtCore.Qt.ControlModifier)
    assert dialog.tabWidget.currentIndex() == dialog.TAB_MAIN
    dialog.tabWidget.setCurrentIndex(dialog.TAB_PROCS)
    visible = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount()) if not view.tree.topLevelItem(i).isHidden()]
    print('Groups:', [item.text(0) for item in visible])
    assert sum(group.childCount() for group in visible) == 5
    view.set_search('example browser')
    assert sum(view.tree.topLevelItem(i).childCount() for i in range(view.tree.topLevelItemCount())) == 1
    view.set_search('')
    exported = list(view.export_rows())
    assert len(exported) == 6 and all(row[4] in paths for row in exported[1:])
    browser_group = next(view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())
                         if view.tree.topLevelItem(i).data(0, QtCore.Qt.UserRole + 1) == 'Allowed')
    view.tree.setCurrentItem(browser_group.child(0)); view.copy_path()
    assert app.clipboard().text() == paths[0]
    view.toggle_details(True)
    assert paths[0] in view.details.toPlainText()
    assert view.allow.isEnabled() and view.block.isEnabled() and view.defaults.isEnabled()
    view.change_access('deny'); app.processEvents()
    assert dialog.tabWidget.currentIndex() == dialog.TAB_PROCS
    assert dialog._apps_stack.currentIndex() == 0
    assert view._rule_review is None
    assert ('unix:/local', paths[0]) in runtime.controller.pending
    assert view.tree.currentItem().text(1) == 'Applying…'
    assert sent[-1][2].name == 'browser-rule' and sent[-1][2].action == 'deny'
    assert runtime.policies.summarize('unix:/local', paths[0]).group == 'Allowed'
    acknowledge()
    assert runtime.policies.summarize('unix:/local', paths[0]).group == 'Blocked'
    assert 'Block' in view.tree.currentItem().text(1)
    view.change_access('allow'); acknowledge()
    assert runtime.policies.summarize('unix:/local', paths[0]).group == 'Allowed'
    view.open_rules(); app.processEvents()
    review = view._rule_review
    assert review.isVisible()
    assert review.rules.topLevelItemCount() == 1
    assert review.rules.currentItem().data(0, QtCore.Qt.UserRole) == 'browser-rule'
    review.grab().save('/tmp/opensnitch-app-rule-review.png')
    review.reject(); app.processEvents()
    assert view.selected_key() == ('unix:/local', paths[0])
    view.open_rules(); app.processEvents()
    captured = []
    def capture_editor(records, node):
        captured.append((records.value(dialog.COL_R_NAME), node))
    with patch.object(dialog._rules_dialog, 'edit_rule', side_effect=capture_editor), \
         patch.object(dialog._rules_dialog, 'raise_'), patch.object(dialog._rules_dialog, 'activateWindow'):
        QtTest.QTest.mouseClick(view._rule_review.edit_button, QtCore.Qt.LeftButton)
    assert captured == [('browser-rule', 'unix:/local')]
    assert dialog.tabWidget.currentIndex() == dialog.TAB_PROCS
    assert not runtime.controller.pending
    view.open_rules(); app.processEvents()
    QtTest.QTest.mouseClick(view._rule_review.edit_button, QtCore.Qt.LeftButton)
    app.processEvents()
    assert dialog._rules_dialog.isVisible()
    assert dialog._rules_dialog.ruleNameEdit.text() == 'browser-rule'
    assert dialog._rules_dialog.actionAllowRadio.isChecked()
    assert dialog.tabWidget.currentIndex() == dialog.TAB_PROCS
    dialog._rules_dialog.hide(); app.processEvents()
    assert not runtime.controller.pending
    assert runtime.policies.summarize('unix:/local', paths[0]).group == 'Allowed'
    view.tree.setCurrentItem(view._items[('unix:/local', paths[3])])
    before_custom = len(sent)
    view.change_access('deny'); app.processEvents()
    assert len(sent) == before_custom
    assert 'Custom or conflicting' in view.feedback.text()
    assert not view._rule_review.isVisible()
    assert dialog.tabWidget.currentIndex() == dialog.TAB_PROCS
    view.tree.setCurrentItem(view._items[('unix:/local', paths[0])])
    view.sort_by(2)
    assert view._sort_column == 2
    dialog.grab().save(os.environ.get('APP_FIRST_SCREENSHOT', '/tmp/opensnitch-app-first-preview.png'))
    # Preserve the node/path when opening and returning from existing activity.
    dialog._open_catalog_activity('unix:/local', paths[0])
    app.processEvents()
    assert dialog._apps_stack.currentIndex() == 1
    assert "c.node = 'unix:/local'" in dialog.procsTable.model().query().lastQuery()
    dialog._cb_cmd_back_clicked(dialog.TAB_PROCS)
    app.processEvents()
    assert dialog._apps_stack.currentIndex() == 0
    assert dialog._apps_view.tree.topLevelItemCount() == 6
    # Repeated live updates retain selected rows, collapsed sections and the
    # actual top visible item, even while that selection is offscreen.
    runtime.catalog.observe_many('unix:/local', [str(binaries / ('tool-%03d' % i)) for i in range(160)], 'manual')
    app.processEvents()
    browser = view._items[('unix:/local', paths[0])]
    view.tree.setCurrentItem(browser)
    view._groups['Custom rules'].setExpanded(False)
    view.tree.verticalScrollBar().setValue(view.tree.verticalScrollBar().maximum() // 2)
    app.processEvents()
    anchor = view.tree.itemAt(0, 0)
    value = view.tree.verticalScrollBar().value()
    assert value > 0
    for _ in range(20):
        runtime.changed.emit(); app.processEvents()
        assert view.tree.currentItem() is browser
        assert view.tree.itemAt(0, 0) is anchor
        assert view.tree.verticalScrollBar().value() == value
        assert not view._groups['Custom rules'].isExpanded()
    runtime.catalog.observe_many('unix:/local', [str(binaries / 'aaa-new-entry')], 'manual')
    app.processEvents()
    assert view.tree.itemAt(0, 0) is anchor
    assert view.tree.currentItem() is browser
    assert view.tree.y() < view.allow.height() * 2 + 24
    # Services share the same inventory, policy summaries and action controller.
    dialog._navigation.bar.setCurrentIndex(1); app.processEvents()
    services = dialog._services_view
    assert dialog._navigation.extra_active('services')
    assert services.tree.topLevelItemCount() == 6
    assert services._items[('system', 'isolated.service')].text(3) == 'browser'
    assert dialog.tabWidget.count() == 9  # Legacy constants are unchanged.
    services.tree.setCurrentItem(services._items[('system', 'browser-worker.service')])
    assert services.shared_list.count() == 2
    assert services.shared_list.isVisible()
    assert services.block.isEnabled()
    extra = ServiceIdentity('user', 'extra-worker.service', 'Additional worker', 'inactive',
                            configured_executable=paths[0], executable_path=paths[0])
    service_provider.next_records = dict(service_provider.records)
    service_provider.next_records[extra.key] = extra
    before_service = len(sent)
    services.block_executable(); app.processEvents()
    assert len(sent) == before_service  # Newly shared scope must be reviewed inline.
    assert 'Service associations changed' in services.feedback.text()
    assert services.shared_list.count() == 3
    services.block_executable(); app.processEvents()
    assert len(sent) == before_service + 1
    assert sent[-1][0] == 'unix:/local' and sent[-1][2].operator['data'] == paths[0]
    assert services._items[('system', 'browser-worker.service')].text(4) == 'Applying…'
    assert services._items[('user', 'user-worker.service')].text(4) == 'Applying…'
    acknowledge()
    assert runtime.policies.summarize('unix:/local', paths[0]).group == 'Blocked'
    assert services._items[('user', 'user-worker.service')].text(4) == 'Blocked'
    services.allow_executable(); app.processEvents(); acknowledge()
    assert runtime.policies.summarize('unix:/local', paths[0]).group == 'Allowed'
    services.copy_path()
    assert app.clipboard().text() == paths[0]
    exported_services = list(services.export_rows())
    assert len(exported_services) == 8 and len(exported_services[0]) == 7
    services.tree.setCurrentItem(services._items[('system', 'script.service')])
    assert not services.allow.isEnabled() and not services.block.isEnabled()
    assert services.manage.isEnabled()
    services.scope.setCurrentIndex(2)
    assert all(services.provider.records[tuple(services.tree.topLevelItem(i).data(0, QtCore.Qt.UserRole))].scope == 'user'
               for i in range(services.tree.topLevelItemCount()))
    services.scope.setCurrentIndex(0)
    services.search.setText('user-worker.service')
    assert services.tree.topLevelItemCount() == 1
    services.search.clear()
    services.tree.setCurrentItem(services._items[('system', 'browser-worker.service')])
    dialog.grab().save('/tmp/opensnitch-services-preview.png')
    Nodes.instance().add('ipv4:203.0.113.9', pb.ClientConfig(name='Remote fixture', config='{}'))
    services.nodes.setCurrentIndex(services.nodes.findData('ipv4:203.0.113.9')); app.processEvents()
    assert services.tree.topLevelItemCount() == 0
    assert 'Remote service identities are not inferred' in services.notice.text()
    assert ('ipv4:203.0.113.9', paths[0]) not in runtime.catalog.entries
    services.nodes.setCurrentIndex(services.nodes.findData('unix:/local'))
    many_services = dict(service_provider.records)
    for i in range(100):
        record = ServiceIdentity('system', 'bulk-%03d.service' % i, 'Service %03d' % i,
                                 configured_executable=paths[0], executable_path=paths[0])
        many_services[record.key] = record
    service_provider._serial += 1
    service_provider._accept(service_provider._serial, many_services, {})
    services.tree.setCurrentItem(services._items[('system', 'browser-worker.service')])
    services.tree.verticalScrollBar().setValue(services.tree.verticalScrollBar().maximum() // 2)
    app.processEvents()
    service_anchor = services.tree.itemAt(0, 0)
    service_scroll = services.tree.verticalScrollBar().value()
    assert service_scroll > 0
    services.shared_list.verticalScrollBar().setValue(services.shared_list.verticalScrollBar().maximum())
    shared_scroll = services.shared_list.verticalScrollBar().value()
    for _ in range(20):
        runtime.changed.emit(); app.processEvents()
        assert services.tree.itemAt(0, 0) is service_anchor
        assert services.tree.verticalScrollBar().value() == service_scroll
        assert services.shared_list.verticalScrollBar().value() == shared_scroll
        assert services.selected_record().key == ('system', 'browser-worker.service')
    before_unavailable = len(sent)
    service_provider.next_records = {}
    service_provider.errors = {'system': 'System manager unavailable'}
    services.block_executable(); app.processEvents()
    assert len(sent) == before_unavailable
    assert services.tree.topLevelItemCount() == 0
    assert 'System manager unavailable' in services.notice.text()
    assert not runtime.controller.pending
    services.save_settings()
    dialog._navigation.select_tab(dialog.TAB_PROCS)
    assert dialog.navToolBar.isVisible()
    # Primary navigation preserves diagnostic tools and existing editors.
    before_navigation = len(sent)
    dialog._navigation.show_advanced(); app.processEvents()
    assert dialog._advanced_view.isVisible() and not dialog.navToolBar.isVisible()
    assert not dialog._export_csv_action.isEnabled()
    dialog.grab().save('/tmp/opensnitch-advanced-preview.png')
    for logical in (dialog.TAB_RULES, dialog.TAB_NODES, dialog.TAB_HOSTS, dialog.TAB_ADDRS, dialog.TAB_PORTS, dialog.TAB_USERS):
        dialog._advanced_view.buttons[logical].click(); app.processEvents()
        assert dialog.tabWidget.currentIndex() == logical
        assert dialog._navigation.context.isVisible()
        assert dialog._navigation.bar.currentIndex() == 4
        assert dialog._export_csv_action.isEnabled()
        dialog._navigation.advanced_back.click()
        assert dialog._navigation.extra_active('advanced')
    dialog._advanced_view.firewall.click(); app.processEvents()
    assert dialog._fw_dialog.isVisible()
    dialog._fw_dialog.hide()
    dialog._advanced_view.new_rule.click(); app.processEvents()
    assert dialog._rules_dialog.isVisible()
    dialog._rules_dialog.hide()
    assert len(sent) == before_navigation
    # Real SQL rows retain their identifiers while delegates show friendly names.
    from opensnitch.customwidgets.applicationidentity import APPLICATION_IDENTITY_ROLE
    fields = '(time, node, action, protocol, src_ip, src_port, dst_ip, dst_host, dst_port, uid, pid, process, process_args, process_cwd, rule)'
    for stamp, node in (('fixture-local', 'unix:/local'), ('fixture-remote', 'ipv4:203.0.113.9')):
        db.insert('connections', fields, (stamp, node, 'allow', 'tcp', '127.0.0.1', '5000', '203.0.113.8',
            'example.test', '443', '1000', '42', paths[0], '--url <literal>&value', str(binaries), 'browser-rule'))
    dialog.tabWidget.setCurrentIndex(dialog.TAB_MAIN); dialog.filterLine.clear(); app.processEvents()
    activity = dialog.eventsTable.model()
    assert activity.rowCount() == 2
    activity_header = dialog.eventsTable.horizontalHeader()
    activity_header.moveSection(activity_header.visualIndex(dialog.COL_PROCS), 3)
    dialog._application_first_action.trigger()
    assert activity_header.visualIndex(dialog.COL_PROCS) == 0
    for row in range(activity.rowCount()):
        index = activity.index(row, dialog.COL_PROCS)
        node = activity.index(row, dialog.COL_NODE).data()
        assert index.data() == paths[0]
        assert index.data(APPLICATION_IDENTITY_ROLE).exact == (node == 'unix:/local')
        assert paths[0] in index.data(QtCore.Qt.ToolTipRole)
    local_row = next(row for row in range(activity.rowCount()) if activity.index(row, dialog.COL_NODE).data() == 'unix:/local')
    dialog._cb_main_table_double_clicked(activity.index(local_row, dialog.COL_PROCS)); app.processEvents()
    assert dialog.tabWidget.currentIndex() == dialog.TAB_PROCS
    assert "c.node = 'unix:/local'" in dialog.procsTable.model().query().lastQuery()
    assert dialog.LAST_SELECTED_ITEM == paths[0]
    assert dialog.procsLabel.text() == 'Example Browser'
    dialog._cb_cmd_back_clicked(dialog.TAB_PROCS)
    dialog.tabWidget.setCurrentIndex(dialog.TAB_MAIN); app.processEvents()
    from opensnitch.dialogs.conndetails import ConnDetails
    details = ConnDetails(dialog)
    details.showByField('time', 'fixture-local'); app.processEvents()
    assert 'Example Browser' in details.windowTitle()
    assert paths[0] in details._textedit.toPlainText()
    assert '--url <literal>&value' in details._textedit.toPlainText()
    assert not details.windowIcon().isNull()
    details.hide()
    dialog.grab().save('/tmp/opensnitch-activity-preview.png')
    dialog.tabWidget.setCurrentIndex(dialog.TAB_NETSTAT); app.processEvents()
    assert dialog._navigation.bar.currentIndex() == 2
    assert dialog._netstat_selected_node() == 'unix:/local'
    socket = {'Processes': {'42': {'Path': paths[0], 'Comm': 'browser'}}, 'Table': [
        {'PID': 42, 'Iface': 'lo', 'Proto': 6, 'Socket': {'ID': {'Interface': 0, 'SourcePort': 5000,
         'Source': '127.0.0.1', 'Destination': '203.0.113.8', 'DestinationPort': 443, 'Cookie': [1, 2]},
         'UID': 1000, 'INode': 9, 'Family': 2, 'State': 1, 'RQueue': 0, 'WQueue': 0,
         'Expires': 0, 'Retrans': 0, 'Timer': 0}}]}
    dialog._update_netstat_table('unix:/local', json.dumps(socket)); app.processEvents()
    sockets = dialog.netstatTable.model()
    assert sockets.rowCount() == 1 and sockets.index(0, 1).data(APPLICATION_IDENTITY_ROLE).exact
    assert sockets.index(0, 1).data() == paths[0]
    assert sockets.index(0, 2).data() == 'Established'
    assert sockets.index(0, 7).data() == 'TCP'
    dialog.grab().save('/tmp/opensnitch-connections-preview.png')
    dialog._update_netstat_table('ipv4:203.0.113.9', json.dumps(socket)); app.processEvents()
    assert sockets.snapshot_node == 'ipv4:203.0.113.9'
    assert dialog._netstat_selected_node() == 'unix:/local'
    assert not sockets.index(0, 1).data(APPLICATION_IDENTITY_ROLE).exact
    with patch.object(dialog._proc_details_dialog, 'monitor') as monitor:
        dialog._cb_table_double_clicked(sockets.index(0, dialog.COL_NET_PID))
        monitor.assert_called_once_with({'42': 'ipv4:203.0.113.9'})
    with patch.object(dialog._nodes, 'send_notification', return_value=None) as transmit:
        with QtCore.QSignalBlocker(dialog.comboNetstatInterval):
            dialog.comboNetstatInterval.setCurrentIndex(1)
        dialog._monitor_node_netstat()
        dialog._navigation.show_advanced()
        assert dialog._netstat_monitor_suspended
        dialog._navigation.bar.setCurrentIndex(2)
        dialog.tabWidget.setCurrentIndex(dialog.TAB_MAIN)
        calls = [(call.args[0], call.args[1].type) for call in transmit.call_args_list]
        assert calls == [('unix:/local', pb.TASK_START), ('unix:/local', pb.TASK_STOP),
                         ('unix:/local', pb.TASK_START), ('unix:/local', pb.TASK_STOP)]
        assert all(not call.args[1].rules for call in transmit.call_args_list)
        with QtCore.QSignalBlocker(dialog.comboNetstatInterval):
            dialog.comboNetstatInterval.setCurrentIndex(0)
    dialog.tabWidget.setCurrentIndex(dialog.TAB_NETSTAT); app.processEvents()
    dialog._cb_table_double_clicked(sockets.index(0, 1)); app.processEvents()
    assert dialog.tabWidget.currentIndex() == dialog.TAB_PROCS
    assert "c.node = 'ipv4:203.0.113.9'" in dialog.procsTable.model().query().lastQuery()
    dialog._cb_cmd_back_clicked(dialog.TAB_PROCS)
    dialog.tabWidget.setCurrentIndex(dialog.TAB_MAIN)
    # CSV still contains executable identifiers, not the friendly presentation.
    import csv
    export_path = str(root / 'activity.csv')
    with patch.object(QtWidgets.QFileDialog, 'getSaveFileName', return_value=(export_path, 'CSV')):
        dialog._on_menu_export_csv_clicked(False)
    exported = list(csv.reader(Path(export_path).open()))
    assert len(exported) == 3 and all(row[dialog.COL_PROCS] == paths[0] for row in exported[1:])
    inventory_count = len(runtime.catalog.entries)
    db.clean('connections'); dialog._refresh_active_table()
    assert len(runtime.catalog.entries) == inventory_count
    assert runtime.policies.summarize('unix:/local', paths[0]).group == 'Allowed'
    assert len(sent) == before_navigation
    print('Primary navigation, Advanced tools/editors, raw exports, local/remote activity, socket provenance and history-independent inventory verified.')
    print('Services: shared-scope verification, inactive units, read-only wrappers, policy propagation, search, export and remote provenance verified.')
    print('Search, export, raw-path copy, direct acknowledgment-aware controls, details, sorting, rule navigation and activity round trip verified. Only mock rule messages sent to isolated fixtures; no real daemon contacted.')
    runtime._timer.stop(); runtime.controller._timer.stop(); runtime.catalog.flush(); identities.stop()
    dialog.hide()
