"""Single-writer application session. All methods run on the receiving UI thread."""
from dataclasses import replace
import copy
import configparser
import fcntl
import json
import logging
from pathlib import Path
import time

from .backend import ApplicationSnapshot, BackendSnapshot, InterceptionState
from .catalog import CatalogCore
from .controls import AccessControllerCore
from .events import Event
from .history_writer import HistoryWriter
from .identity import IdentityCore
from .protocol import rule_files_snapshot, to_proto
from .settings import PromptSettings
from .sockets import SocketMonitor
from .activity import ActivityBrowser
from .rules import RuleIndex
from .rule_edit import duration_seconds
from .transport import UITransport, pb
from opensnitch.application_policy import ApplicationPolicyService, ApplicationRule, executable_paths, describe_rule


class ApplicationSession:
    def __init__(self, directory, save_timer, dispatch):
        self.directory = Path(directory)
        self.dispatch = dispatch
        self._closed = False
        self._updating = False
        self.changed, self.message, self.alert = Event(), Event(), Event()
        self.catalog = CatalogCore(self.directory / 'applications.json', save_timer)
        if self.catalog.read_only:
            raise ValueError(self.catalog.load_error)
        self.identities = IdentityCore()
        self.policies = ApplicationPolicyService()
        self.rule_index = RuleIndex()
        self.nodes, self._baselines, self._stale = {}, {}, {}
        self._online = set()
        self._refresh_needed = {}
        self._reconcile_requested = set()
        self._edited_names = {}
        self._checksum_pending = set()
        self._checksum_operations = {}
        self._expiry_seen = set()
        self._expiry_retries = {}
        self._logged_policies = {}
        self.error = ''
        self.transport = self.history = self._lease = self.sockets = self.activity = None
        self.cleanup_job = None
        self._interception_pending = None
        self._interception_known = False
        self._interception_enabled = None
        self._interception_error = ''
        self._interception_serial = 0
        self.application_snapshots = {}
        self.snapshot = BackendSnapshot((), message='Waiting for the daemon…')
        self.controller = AccessControllerCore(self.catalog, self.policies, self._send,
            self.online, self._confirmed, self.verify_current, dispatch=self.deliver, verify_node=self.verify_node)
        self.catalog.changed.connect(self.publish)
        self.catalog.error.connect(self._error)
        self.controller.changed.connect(self.publish)
        self.controller.message.connect(self._message)

    def settings(self):
        return PromptSettings.read(self.directory)

    def deliver(self, callback, *args):
        def run():
            if not self._closed:
                callback(*args)
        self.dispatch(run)

    def start(self, address, prompt_callback):
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lease = open(self.directory / 'applications.lock', 'a+')
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.transport = UITransport(address, self.deliver, self.handle_event, prompt_callback, self.settings)
            self.transport.start()
            self.sockets = SocketMonitor(self.transport, self.online, self.accept_sockets)
            config = configparser.ConfigParser(interpolation=None)
            config.read(self.directory / 'settings.conf')
            memory = config.get('database', 'type', fallback='0') == '0'
            path = config.get('database', 'file', fallback='file::memory:')
            self.history = HistoryWriter(None if memory or path == 'file::memory:' else path,
                self.deliver, self._error, config.getboolean('database', 'jrnl_wal', fallback=False))
            self.history.ready.result(timeout=5)
            self.activity = ActivityBrowser(self.history, self.activity_name_matches)
        except Exception:
            self.close()
            raise
        self.publish()

    def subscribe(self, callback):
        disconnect = self.changed.connect(callback)
        callback(self.snapshot)
        return disconnect

    def online(self, node):
        return node in self._online

    def accept_index(self, index):
        self._updating = True
        try:
            self.identities.accept_index(index)
            for node in self.nodes:
                if self.identities.is_local_node(node):
                    self.catalog.observe_many(node, self.identities.installed_executables(), 'installed')
        finally:
            self._updating = False
        self.publish()

    def handle_event(self, kind, node, data=None):
        if self._closed:
            return
        self._updating = True
        try:
            if kind == 'subscribe':
                configuration = pb.ClientConfig.FromString(data)
                self.nodes[node] = configuration
                self.identities.register_node(node, node == 'unix:/local')
                rules = tuple(ApplicationRule.from_proto(rule) for rule in configuration.rules)
                self.policies.replace_rules(node, rules)
                signatures = {(node, rule.name, rule.fingerprint) for rule in rules if rule.enabled}
                self._expiry_seen = {value for value in self._expiry_seen if value[0] != node or value in signatures}
                self._expiry_retries = {key: value for key, value in self._expiry_retries.items() if key[0] != node or key in signatures}
                self.controller.reconcile(node, rules, allow_rebase=node in self._reconcile_requested)
                for entry in tuple(self.catalog.entries.values()):
                    if entry.node_id == node and entry.ownership.get('name') in self._edited_names.get(node, ()):
                        target = self.policies.simple_application_rule(*entry.key)
                        if target is None or target.name != entry.ownership.get('name'):
                            self.catalog.set_ownership(entry.key, {})
                self._edited_names.pop(node, None)
                self._reconcile_requested.discard(node)
                self._stale.pop(node, None)
                self._refresh_needed.pop(node, None)
                if node == 'unix:/local':
                    try:
                        self._baselines[node] = rule_files_snapshot(configuration.config)
                    except (OSError, ValueError, TypeError):
                        self._baselines.pop(node, None)
                paths = {path for rule in rules for path in executable_paths(rule.operator)}
                self.catalog.observe_many(node, paths, 'rules')
                if self.identities.is_local_node(node):
                    self.catalog.observe_many(node, self.identities.installed_executables(), 'installed')
                if self.history:
                    self.history.submit('subscription', node, data)
                if node == 'unix:/local':
                    self._interception_subscribed(configuration.isFirewallRunning)
            elif kind == 'connected':
                self._online.add(node)
            elif kind == 'disconnect':
                self._online.discard(node)
                if node == 'unix:/local':
                    self._interception_known = False
                    pending = self._interception_pending
                    if pending and pending['phase'] == 'sending':
                        self._interception_failed(pending['serial'], 'Daemon disconnected before confirming the change.')
                self.controller.check_timeouts()
                if self.history:
                    self.history.submit('disconnect', node)
            elif kind == 'statistics':
                statistics = pb.Statistics.FromString(data)
                self.catalog.observe_many(node, statistics.by_executable, 'observed', observed_at=0)
                for event in statistics.events:
                    current_rule = self.policies.rule_by_name(node, event.rule.name)
                    if current_rule and current_rule.duration == 'once':
                        self._stale[node] = 'A one-use rule may have been consumed; refreshing its state.'
                        self._refresh_needed[node] = time.monotonic()
                    self.catalog.observe_many(node, [event.connection.process_path], 'observed', event.unixnano / 1e9)
                if self.history:
                    future = self.history.submit('statistics', node, data)
                    future.add_done_callback(lambda result: self.deliver(self.activity.history_changed))
            elif kind == 'prompt-reply':
                rule = pb.Rule.FromString(data)
                for path in executable_paths(ApplicationRule.from_proto(rule).operator):
                    self.catalog.observe_many(node, [path], 'observed')
                # AskRule has no acknowledgment RPC. Refresh the daemon's full
                # subscription after it consumes the response, never guess policy.
                self._stale[node] = 'Waiting for the daemon to confirm the prompt decision.'
                self._refresh_needed[node] = time.monotonic() + 1
            elif kind == 'alert':
                if self.history:
                    self.history.submit('alert', node, data)
                self.alert.emit(node, data)
        except Exception as error:
            self._error(str(error))
        finally:
            self._updating = False
        if self.sockets and kind in ('connected', 'disconnect'):
            self.sockets.connection_changed()
        self.publish()

    def verify_current(self, entry):
        return self.verify_node(entry.node_id)

    def verify_node(self, node):
        if self._interception_pending and node == 'unix:/local':
            return 'An app filtering change is awaiting the daemon.'
        if self.error:
            return self.error
        if self.controller.general_pending(node):
            return 'A rule editor operation is awaiting the daemon.'
        if node in self._stale:
            return self._stale[node] + ' Refresh policies before changing access.'
        if node != 'unix:/local':
            return 'Use the Qt node rule editor for remote changes during migration.'
        if any(key[0] == node for key in self._checksum_pending):
            return 'A checksum update is awaiting the daemon.'
        try:
            baseline = self._baselines.get(node)
            if baseline is None or rule_files_snapshot(self.nodes[node].config) != baseline:
                return 'Rules changed or could not be verified. Refresh policies before changing access.'
        except (OSError, ValueError, KeyError, TypeError):
            return 'The current rules could not be verified. Refresh policies.'
        return ''

    def interception_state(self):
        node = 'unix:/local'
        pending = self._interception_pending
        enabled = self._interception_enabled if self.online(node) and self._interception_known else None
        busy = bool(self.controller.pending or self._checksum_pending or
                    (self.cleanup_job and not self.cleanup_job.finished))
        available = (self.online(node) and not pending and not busy and node not in self._stale
                     and self.transport is not None and not self.transport.has_prompts(node))
        return InterceptionState(enabled, available, bool(pending), pending['target'] if pending else None, self._interception_error)

    def request_interception(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('App filtering must be on or off.')
        state = self.interception_state()
        if not state.available or state.enabled == enabled:
            self.publish()
            return False
        self._interception_serial += 1
        serial = self._interception_serial
        self._interception_pending = dict(serial=serial, target=enabled, phase='sending', deadline=time.monotonic() + 15)
        self._interception_error = ''
        self.publish()
        def replied(success, error=''):
            pending = self._interception_pending
            if self._closed or not pending or pending['serial'] != serial:
                return
            if not success:
                self._interception_failed(serial, error or 'The daemon rejected the change.')
                return
            self._interception_enabled = pending['target']
            self._interception_known = True
            self._interception_pending = None
            self.publish()
        try:
            self.transport.send('unix:/local', pb.Notification(type=pb.ENABLE_INTERCEPTION if enabled else pb.DISABLE_INTERCEPTION), replied)
        except Exception as error:
            self._interception_failed(serial, str(error))
        return True

    def _interception_failed(self, serial, error):
        pending = self._interception_pending
        if not pending or pending['serial'] != serial:
            return
        self._interception_pending = None
        self._interception_known = False
        self._interception_error = error + ' Choose On or Off to retry.'
        self._message(self._interception_error)
        self.publish()

    def _interception_subscribed(self, enabled):
        # 1.7.2 isFirewallRunning is the initialized firewall service, not the
        # pause flag. It stays true after DISABLE_INTERCEPTION. Never use true
        # to overwrite an acknowledged pause or claim a confirmed On state.
        self._interception_known = not enabled
        self._interception_enabled = False if not enabled else None

    def start_cleanup(self, plan, changed):
        from .cleanup import CleanupJob
        if self.cleanup_job and not self.cleanup_job.finished:
            return None
        self.cleanup_job = CleanupJob(self, plan, changed)
        self.cleanup_job.start()
        return self.cleanup_job

    def request_policy(self, key, action, duration='always'):
        if key not in self.catalog.entries:
            return False
        return self.controller.request(key, action, duration)

    def request_rule(self, node, proposed, expected=None, operation='change', callback=None):
        def completed(success, error):
            self._edited_names.setdefault(node, set()).add(proposed.name)
            self._reconcile_requested.add(node)
            self._stale[node] = 'Refreshing the acknowledged rule operation.' if success else 'The rule operation is unconfirmed.'
            self._refresh_needed[node] = time.monotonic() + .2
            self.publish()
            if callback:
                callback(success, error)
        return self.controller.request_rule(node, proposed, expected, operation, completed)

    def _send(self, node, operation, rule, callback):
        kind = {'delete': pb.DELETE_RULE, 'enable': pb.ENABLE_RULE, 'disable': pb.DISABLE_RULE}.get(operation, pb.CHANGE_RULE)
        self.transport.send(node, pb.Notification(type=kind,
            rules=[pb.Rule(name=rule.name) if operation == 'delete' else to_proto(rule, pb.Rule)]), callback)

    def _confirmed(self, node, operation, rule):
        rules = [value for value in self.policies.rules.get(node, ()) if value.name != rule.name]
        if operation != 'delete':
            rules.append(rule)
        self.policies.replace_rules(node, rules)
        if operation != 'delete':
            self.catalog.observe_many(node, executable_paths(rule.operator), 'rules', observed_at=0)
        if self.history:
            self.history.submit('rule', node, to_proto(rule, pb.Rule).SerializeToString(), operation == 'delete')
        baseline = self._baselines.get(node)
        if baseline is not None:
            current = rule_files_snapshot(self.nodes[node].config)
            filename = rule.name + '.json'
            if filename in current:
                baseline[filename] = current[filename]
            else:
                baseline.pop(filename, None)
            if current == baseline:
                self._stale.pop(node, None)

    def refresh_policies(self):
        if self.controller.pending or self._checksum_pending:
            self._message('Wait for the pending operation before refreshing policies.')
            return
        for node in tuple(self._online):
            if self.transport.has_prompts(node):
                continue
            self._reconcile_requested.add(node)
            self._stale[node] = 'Refreshing the daemon rule snapshot.'
            self.transport.disconnect(node)
        self.publish()

    def tick(self):
        if self._closed:
            return
        self.controller.check_timeouts()
        pending = self._interception_pending
        if pending and time.monotonic() >= pending['deadline']:
            self._interception_failed(pending['serial'], 'App filtering change timed out.')
        if self.cleanup_job:
            self.cleanup_job.tick()
        if self.sockets:
            self.sockets.tick()
        changed = False
        for nonce, (deadline, callback) in tuple(self._checksum_operations.items()):
            if time.monotonic() >= deadline:
                callback(False, 'Checksum update timed out; refresh policies.')
        for node, rules in self.policies.rules.items():
            for rule in rules:
                if not rule.enabled:
                    continue
                try:
                    seconds = duration_seconds(rule.duration)
                except (ValueError, TypeError):
                    continue
                if seconds is not None:
                    owner = next((entry.ownership for entry in self.catalog.entries.values()
                                  if entry.node_id == node and entry.ownership.get('name') == rule.name), {})
                    deadline = owner.get('expires_at', rule.created + seconds)
                    signature = (node, rule.name, rule.fingerprint)
                    if deadline <= time.time() and (signature not in self._expiry_seen or (node not in self._stale and time.monotonic() >= self._expiry_retries.get(signature, 0))):
                        self._expiry_seen.add(signature)
                        self._expiry_retries[signature] = time.monotonic() + 1
                        self._stale[node] = 'A temporary policy may have expired; refreshing its state.'
                        self._refresh_needed[node] = time.monotonic()
                        changed = True
        for node in tuple(self._online):
            if node not in self.nodes:
                continue
            try:
                baseline = self._baselines.get(node)
                if baseline is not None and rule_files_snapshot(self.nodes[node].config) != baseline:
                    self._stale[node] = 'Rules changed outside the confirmed snapshot.'
                    changed = True
            except (OSError, ValueError, TypeError):
                self._stale[node] = 'The rule files could not be verified.'
                changed = True
        for entry in self.catalog.entries.values():
            if 'expires_at' not in entry.ownership:
                continue
            owned_rule = self.policies.rule_by_name(entry.node_id, entry.ownership.get('name'))
            if owned_rule is not None and not owned_rule.enabled:
                continue
            if entry.ownership.get('expires_at', float('inf')) <= time.time() and not entry.ownership.get('uncertain'):
                owner = dict(entry.ownership, uncertain=True, operation='expiry')
                self.catalog.set_ownership(entry.key, owner)
                self._refresh_needed[entry.node_id] = time.monotonic()
        for node, deadline in tuple(self._refresh_needed.items()):
            if time.monotonic() >= deadline and not self.transport.has_prompts(node) and not self.controller.pending:
                self._refresh_needed.pop(node, None)
                self.transport.disconnect(node)
        if changed:
            self.publish()

    def update_checksums(self, node, names, connection, callback):
        actual = connection.process_checksums.get('process.hash.md5', '')
        entry = self.catalog.entries.get((node, connection.process_path))
        reason = self.verify_current(entry) if entry else 'Application inventory unavailable.'
        if not actual or reason or not self.online(node):
            callback(False, reason or 'Checksum or node unavailable.'); return
        related = {rule.name: rule for rule in self.policies.application_rules(node, connection.process_path)}
        requested = []
        for name in names:
            old = related.get(name)
            if old is None:
                callback(False, 'The selected rule no longer matches this application.'); return
            operator = copy.deepcopy(old.operator)
            def update(op):
                if op.get('operand') == 'process.hash.md5':
                    op['data'] = actual
                for child in op.get('list', ()):
                    update(child)
            update(operator)
            if operator == old.operator:
                callback(False, 'The selected rule has no different checksum to update.'); return
            requested.append((old, replace(old, operator=operator)))
        if not requested:
            callback(False, 'No checksum rules were selected.'); return
        self._checksum_pending.update((node, old.name) for old, new in requested)
        import uuid
        nonce = uuid.uuid4().hex
        def receive(success, error):
            if self._checksum_operations.pop(nonce, None) is None:
                return
            try:
                for old, new in requested:
                    self._checksum_pending.discard((node, old.name))
                    if success:
                        self._confirmed(node, 'change', new)
                        if entry.ownership.get('name') == old.name and entry.ownership.get('fingerprint') == old.fingerprint:
                            self.catalog.set_ownership(entry.key, dict(entry.ownership, fingerprint=new.fingerprint, policy_fingerprint=new.policy_fingerprint))
            except Exception as failure:
                success, error = False, 'The daemon acknowledged the update, but local confirmation failed: ' + str(failure)
                for old, new in requested:
                    self._checksum_pending.discard((node, old.name))
            if not success:
                self._stale[node] = 'Checksum update was not confirmed. Refresh policies.'
            self.publish()
            callback(success, error or ('Checksum updated.' if success else self._stale[node]))
        self._checksum_operations[nonce] = (time.monotonic() + 15, receive)
        self.transport.send(node, pb.Notification(type=pb.CHANGE_RULE,
            rules=[to_proto(new, pb.Rule) for old, new in requested]), receive)
        self.publish()

    def _error(self, error):
        self.error = 'Application state could not be saved or refreshed: ' + error
        self._message(self.error)

    def _message(self, message):
        self.message.emit(message)
        self.publish()

    def activity_name_matches(self, text):
        query = text.strip().casefold()
        return tuple(key for key, snapshot in self.application_snapshots.items()
                     if query and query in snapshot.identity.display_name.casefold())

    def accept_sockets(self, snapshot):
        self.catalog.observe_many(snapshot.node,
            {row.executable for row in snapshot.rows if row.executable}, 'observed', snapshot.timestamp)
        if self.history:
            self.history.submit('sockets', snapshot)

    def observe_services(self, records):
        if 'unix:/local' not in self.nodes:
            return
        self.catalog.observe_many('unix:/local',
            {record.executable_path for record in records.values() if record.executable_path}, 'services', observed_at=0)
        self.publish()

    def publish(self):
        if self._closed or self._updating:
            return
        rows = []
        all_rows = {}
        verification = {}
        for entry in self.catalog.entries.values():
            if entry.hidden:
                continue
            identity = self.identities.resolve(*entry.key)
            current = self.online(entry.node_id) and entry.node_id in self.policies.rules and entry.node_id not in self._stale
            summary = self.policies.summarize(*entry.key)
            if entry.node_id not in verification:
                verification[entry.node_id] = self.verify_current(entry)
            reason = self.controller.reason(entry) or verification[entry.node_id]
            if entry.ownership.get('uncertain'):
                current = False
            rule = summary.rules[0] if len(summary.rules) == 1 else None
            row = ApplicationSnapshot(identity, tuple(entry.sources), entry.last_seen,
                Path(entry.executable_path).exists() if entry.node_id == 'unix:/local' else None,
                summary.group if current else 'Unavailable', entry.key in self.controller.pending, reason,
                rule.action if rule and current else '', rule.duration if rule and current else '',
                tuple(value.name for value in summary.rules), tuple(value.name for value in summary.global_rules),
                tuple((value.name, describe_rule(value)) for value in summary.global_rules))
            all_rows[entry.key] = row
            if set(entry.sources) != {'services'}:
                rows.append(row)
        rows.sort(key=lambda row: (row.identity.display_name.casefold(), row.key))
        for row in rows:
            state = (row.policy, row.action, row.duration, row.error)
            if ('observed' in row.sources or 'rules' in row.sources) and self._logged_policies.get(row.key) != state:
                logging.info('Application policy: node=%s executable=%s group=%s action=%s duration=%s controls=%s', row.identity.node_id, row.identity.executable_path, row.policy, row.action, row.duration, row.error or 'enabled')
                self._logged_policies[row.key] = state
        statuses = [('App filtering status unavailable' if node == 'unix:/local' and not self._interception_known
                     else 'Interception ' + ('enabled' if self._interception_enabled else 'paused') if node == 'unix:/local'
                     else 'Interception ' + ('enabled' if config.isFirewallRunning else 'paused'))
                    for node, config in self.nodes.items() if node in self._online]
        message = (' · '.join(statuses) if statuses else 'Daemon disconnected') + ' · Access applies to the selected executable.'
        if self.error:
            message += ' ' + self.error
        self.application_snapshots = all_rows
        rule_statuses = {}
        for node in self.policies.rules:
            if not self.online(node):
                rule_statuses[node] = 'Cached · computer disconnected'
            elif node != 'unix:/local':
                rule_statuses[node] = self._stale.get(node) or self.error or 'Current daemon snapshot'
            else:
                reason = verification.get(node)
                if reason is None:
                    reason = self.verify_node(node)
                rule_statuses[node] = reason or 'Current daemon snapshot'
        rule_rows = self.rule_index.project(self.policies.rules, rule_statuses)
        self.snapshot = BackendSnapshot(tuple(rows), bool(self._online), bool(self._online) and not self._stale, message, rule_rows, self.interception_state())
        self.changed.emit(self.snapshot)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._online.clear()
        self.controller.check_timeouts()
        self.catalog.flush()
        if self.sockets:
            self.sockets.close()
        if self.transport:
            self.transport.stop()
        if self.activity:
            self.activity.close()
        if self.history:
            self.history.close()
        if self._lease:
            self._lease.close()
