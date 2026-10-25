"""Adapters between the shared app model and existing OpenSnitch APIs."""

import json
from pathlib import Path
import time
import logging

from PyQt5 import QtCore

from opensnitch.application_catalog import ApplicationCatalog
from opensnitch.application_controls import ApplicationAccessController
from opensnitch.application_identity import ApplicationIdentityService
from opensnitch.application_policy import ApplicationPolicyService, ApplicationRule, executable_paths
from opensnitch.database import Database
from opensnitch.database.enums import RuleFields
from opensnitch.config import Config
from opensnitch.nodes import Nodes
from opensnitch.rules import Rule, Rules
import opensnitch.proto as proto

ui_pb2, _ = proto.import_()


def rule_files_snapshot(configuration):
    """Track external writes without reading the daemon's root-only files."""
    config = json.loads(configuration or '{}')
    directory = Path(config.get('Rules', {}).get('Path') or '/etc/opensnitchd/rules')
    result = {}
    for path in directory.iterdir():
        if path.suffix == '.json':
            stat = path.stat()
            result[path.name] = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return result


def to_proto(rule):
    result = ui_pb2.Rule(name=rule.name, action=rule.action, duration=rule.duration,
                         enabled=rule.enabled, precedence=rule.precedence,
                         created=rule.created, description=rule.description, nolog=rule.nolog)
    def fill(target, value):
        target.type = value.get('type', '')
        target.operand = value.get('operand', '')
        target.data = value.get('data', '')
        target.sensitive = value.get('sensitive', False)
        for child in value.get('list', ()):
            fill(target.list.add(), child)
    fill(result.operator, rule.operator)
    return result


class _NotificationCallback:
    def __init__(self, callback):
        self.callback = callback

    def emit(self, node, reply):
        self.callback(reply.code == ui_pb2.OK, reply.data)


class ApplicationRuntime(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__(QtCore.QCoreApplication.instance())
        self.catalog = ApplicationCatalog.instance()
        self.identities = ApplicationIdentityService.instance()
        self.policies = ApplicationPolicyService()
        self.nodes = Nodes.instance()
        self.db = Database.instance()
        self.availability = {}
        self._subscriptions = {}
        self._rule_files = {}
        self.errors = {}
        self.controller = ApplicationAccessController(
            self.catalog, self.policies, self._send, self.online, self._confirmed,
            self._verify_current, parent=self)
        self.catalog.changed.connect(self.changed)
        self.controller.changed.connect(self.changed)
        self.identities.changed.connect(self.sync_installed)
        self.nodes.nodesUpdated.connect(self.sync_nodes)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.poll)
        self._timer.start()
        self.sync_nodes()

    def online(self, node):
        return bool(self.nodes.get_node(node) and self.nodes.get_node(node).get('online'))

    def attach_service_catalog(self, provider):
        if getattr(self, '_service_catalog', None) is provider:
            return
        self._service_catalog = provider
        provider.changed.connect(self.sync_services)
        self.sync_services()

    @QtCore.pyqtSlot()
    def sync_services(self):
        provider = getattr(self, '_service_catalog', None)
        if provider is None:
            return
        paths = {record.executable_path for record in provider.records.values() if record.executable_path}
        for node in self.nodes.get_nodes():
            if self.identities.is_local_node(node):
                self.catalog.observe_many(node, paths, 'services')

    @QtCore.pyqtSlot()
    def sync_installed(self):
        for node in self.nodes.get_nodes():
            if self.identities.is_local_node(node):
                self.catalog.observe_many(node, self.identities.installed_executables(), 'installed')
        self.changed.emit()

    def sync_nodes(self, count=None):
        for node, info in self.nodes.get_nodes().items():
            self.identities.register_node(node, self.nodes.is_local(node))
            configuration = info.get('data')
            if configuration is not None and self._subscriptions.get(node) is not configuration:
                rules = [ApplicationRule.from_proto(rule) for rule in configuration.rules]
                try:
                    self.controller.reconcile(node, rules)
                except OSError as exc:
                    self.catalog.error.emit(str(exc))
                self._subscriptions[node] = configuration
                if self.identities.is_local_node(node):
                    try:
                        self._rule_files[node] = rule_files_snapshot(configuration.config)
                    except (OSError, ValueError, TypeError):
                        self._rule_files.pop(node, None)
        self.sync_installed()
        self.sync_services()
        self.poll()

    def observe_statistics(self, node, statistics):
        # One traffic batch must not rebuild every application row per event.
        latest = {}
        for event in statistics.events:
            path = event.connection.process_path
            latest[path] = max(latest.get(path, 0), event.unixnano / 1000000000)
        with QtCore.QSignalBlocker(self.catalog):
            self.catalog.observe_many(node, statistics.by_executable.keys(), 'observed', observed_at=0)
            for path, timestamp in latest.items():
                self.catalog.observe_many(node, [path], 'observed', timestamp)
        if latest or statistics.by_executable:
            self.catalog.changed.emit()

    def poll(self):
        try:
            self._poll()
        except Exception:
            logging.exception('Could not refresh the application inventory')
            self.errors = {node: 'The current policy could not be refreshed. Use the rule editor.'
                           for node in self.nodes.get_nodes()}
            self.catalog.error.emit('Application refresh failed; showing the last known state.')
            self.changed.emit()

    def _poll(self):
        changed = False
        # A saved inventory is not evidence of a current backend rule snapshot.
        nodes = set(self.nodes.get_nodes()) | set(self.policies.rules)
        for entry in list(self.catalog.entries.values()):
            owner = entry.ownership
            if owner.get('expires_at', float('inf')) <= time.time() and not owner.get('uncertain'):
                known = self.policies.rule_by_name(entry.node_id, owner.get('name'))
                if known is not None and known.fingerprint == owner.get('fingerprint'):
                    # A frontend clock is not confirmation that the daemon
                    # removed a timer rule. Preserve the mirror and ownership
                    # until a fresh subscription provides authoritative state.
                    uncertain = dict(owner, uncertain=True, operation='expiry')
                    self.catalog.set_ownership(entry.key, uncertain)
                    changed = True
        for node in nodes:
            if node not in self.policies.rules and (self.nodes.get_node(node) or {}).get('data') is None:
                continue  # No daemon subscription has established this node's policy yet.
            records = self.db.get_rules(node)
            if records is None:
                self.errors[node] = 'The current rules could not be read. Showing last known state.'
                changed = True
                continue
            rules = []
            while records.next():
                rule = Rule.new_from_records(records)
                if rule.operator.type == 'list':
                    # The legacy editor's reader only reconstructs one level.
                    # Parse the stored list recursively for policy classification.
                    raw = json.loads(records.value(RuleFields.OpData))
                    rule.operator.ClearField('list')
                    def fill_children(target, children):
                        for child in children:
                            operator = target.list.add()
                            operator.type = child.get('type', '')
                            operator.operand = child.get('operand', '')
                            operator.data = child.get('data', '')
                            operator.sensitive = child.get('sensitive', False)
                            fill_children(operator, child.get('list', ()))
                    fill_children(rule.operator, raw)
                rules.append(ApplicationRule.from_proto(rule))
            rules.sort(key=lambda rule: rule.name)
            if node not in self.policies.rules or tuple(rules) != self.policies.rules[node]:
                self.policies.replace_rules(node, rules)
                changed = True
            paths = set()
            for rule in rules:
                paths.update(executable_paths(rule.operator))
            self.catalog.observe_many(node, paths, 'rules')
            if Config.RULES_DURATION_FILTER:
                self.errors[node] = 'Temporary rules are excluded by preferences. Enable them to summarize application policies.'
            elif node in self.errors:
                self.errors.pop(node, None)
                changed = True
        for entry in self.catalog.entries.values():
            if self.identities.is_local_node(entry.node_id):
                exists = Path(entry.executable_path).exists()
                if self.availability.get(entry.key) != exists:
                    self.availability[entry.key] = exists
                    changed = True
        if changed:
            self.changed.emit()

    def _send(self, node, operation, rule, callback):
        notification = ui_pb2.Notification(
            type=ui_pb2.DELETE_RULE if operation == 'delete' else ui_pb2.CHANGE_RULE,
            rules=[to_proto(rule)])
        self.nodes.send_notification(node, notification, _NotificationCallback(callback))

    def _confirmed(self, node, operation, rule):
        if operation == 'delete':
            self.db.delete_rule(rule.name, node)
        elif not Rules.instance().add_rules(node, [to_proto(rule)]):
            raise OSError('Could not update the local rules mirror')
        # Acknowledge only the file this operation changed. An unrelated
        # external edit during the request must still fail the next preflight.
        baseline = self._rule_files.get(node)
        if baseline is not None:
            current = rule_files_snapshot(self.nodes.get_node_config(node))
            filename = rule.name + '.json'
            if filename in current:
                baseline[filename] = current[filename]
            else:
                baseline.pop(filename, None)
        self.poll()

    def _verify_current(self, entry):
        if entry.node_id in self.errors:
            return self.errors[entry.node_id]
        if not self.identities.is_local_node(entry.node_id):
            return 'Use the node rule editor for remote changes; quick controls require local verification.'
        try:
            baseline = self._rule_files.get(entry.node_id)
            if baseline is None:
                return 'The rule snapshot could not be verified. Reconnect the UI to refresh its rules.'
            current = rule_files_snapshot(self.nodes.get_node_config(entry.node_id))
            if current != baseline:
                return 'Rules changed outside Apps. Reconnect the UI to refresh them before changing access.'
        except (OSError, ValueError, KeyError, TypeError):
            return 'The current rules could not be verified. Use Manage rules.'
        return ''
