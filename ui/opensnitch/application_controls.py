"""Acknowledgment-aware controls for exact, whole-application rules."""

from dataclasses import replace
import time
import uuid

from PyQt5 import QtCore

from opensnitch.application_policy import ApplicationRule, managed_rule_name


class ApplicationAccessController(QtCore.QObject):
    changed = QtCore.pyqtSignal()
    message = QtCore.pyqtSignal(str)
    _reply = QtCore.pyqtSignal(str, bool, str)

    def __init__(self, catalog, policies, transport, online, apply_confirmed,
                 verify_current=None, parent=None, timeout=15):
        super().__init__(parent)
        self.catalog = catalog
        self.policies = policies
        self.transport = transport
        self.online = online
        self.apply_confirmed = apply_confirmed
        self.verify_current = verify_current or (lambda entry: '')
        self.timeout = timeout
        self.pending = {}
        self._reply.connect(self._receive_reply, QtCore.Qt.QueuedConnection)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.check_timeouts)
        self._timer.start()

    def reason(self, entry):
        if entry.key in self.pending:
            return 'A rule change is awaiting the daemon.'
        if self.catalog.read_only:
            return 'The application inventory cannot be saved.'
        if not self.online(entry.node_id):
            return 'This node is disconnected.'
        return self.policies.quick_control_reason(entry)

    def request(self, key, action, duration='always'):
        entry = self.catalog.entries[key]
        reason = self.reason(entry) or self.verify_current(entry)
        if reason:
            self.message.emit(reason)
            return False
        if action not in ('allow', 'deny', 'defaults') or duration not in ('always', 'until restart', '12h', '1h'):
            raise ValueError('Unsupported application access action or duration')
        if not self.catalog.flush():
            self.message.emit('Could not save the application inventory; no rule was sent.')
            return False
        target = self.policies.simple_application_rule(*key)
        if action == 'defaults':
            rule = target
            if rule is None:
                self.message.emit('This application already uses defaults.')
                return False
            operation = 'delete'
        else:
            if target is not None:
                # The click explicitly changes this existing app policy. Keep
                # its identifier, conditions, logging and other metadata.
                rule = replace(target, action=action, duration=duration, enabled=True)
            else:
                name = managed_rule_name(entry.executable_path)
                if self.policies.rule_by_name(entry.node_id, name) is not None:
                    self.message.emit('That rule name belongs to another policy. Use Manage rules.')
                    return False
                rule = ApplicationRule(
                    name, action, duration, True, False,
                    {'type': 'simple', 'operand': 'process.path', 'data': entry.executable_path,
                     'sensitive': True, 'list': []}, int(time.time()),
                    'Application access managed by OpenSnitch UI')
            operation = 'change'
        nonce = uuid.uuid4().hex
        self.pending[key] = (nonce, time.monotonic() + self.timeout, operation, rule)
        self.changed.emit()
        try:
            self.transport(entry.node_id, operation, rule,
                           lambda success, error='': self._reply.emit(nonce, success, error))
        except Exception as exc:
            self._reply.emit(nonce, False, str(exc))
        return True

    @QtCore.pyqtSlot(str, bool, str)
    def _receive_reply(self, nonce, success, error):
        for key, pending in tuple(self.pending.items()):
            if pending[0] != nonce:
                continue
            del self.pending[key]
            _, _, operation, rule = pending
            if success:
                try:
                    # Update the UI mirror only after the backend acknowledges.
                    self.apply_confirmed(key[0], operation, rule)
                    owner = {} if operation == 'delete' else self._ownership(key, rule)
                    if operation != 'delete' and rule.duration in ('1h', '12h'):
                        owner['expires_at'] = time.time() + (3600 if rule.duration == '1h' else 43200)
                    self.catalog.set_ownership(key, owner)
                    self.message.emit('Application rule updated. Global rules may still apply.')
                except Exception as exc:
                    self._uncertain(key, operation, rule)
                    self.message.emit('The daemon acknowledged the change, but local state could not be saved: ' + str(exc))
            else:
                self._uncertain(key, operation, rule)
                self.message.emit(error or 'The daemon did not confirm the rule change. Review its rules.')
            self.changed.emit()
            return
        # A late reply must not turn a timed-out operation into visible success.

    def _uncertain(self, key, operation, rule):
        try:
            owner = dict(self._ownership(key, rule), uncertain=True, operation=operation)
            previous = self.policies.rule_by_name(key[0], rule.name)
            if previous is not None and previous.fingerprint != rule.fingerprint:
                owner['previous_fingerprint'] = previous.fingerprint
                expires = self.catalog.entries[key].ownership.get('expires_at')
                if expires is not None:
                    owner['previous_expires_at'] = expires
            self.catalog.set_ownership(key, owner)
        except OSError:
            self.catalog.entries[key].ownership['uncertain'] = True

    @staticmethod
    def _ownership(key, rule):
        owner = {'name': rule.name, 'fingerprint': rule.fingerprint}
        if rule.name != managed_rule_name(key[1]):
            owner.update(adopted=True, executable_path=key[1])
        return owner

    def check_timeouts(self):
        for key, pending in tuple(self.pending.items()):
            if pending[1] <= time.monotonic() or not self.online(key[0]):
                del self.pending[key]
                self._uncertain(key, pending[2], pending[3])
                self.message.emit('Rule change unconfirmed: timeout or disconnected node. Review its rules.')
                self.changed.emit()

    def reconcile(self, node, confirmed_rules):
        """Only a fresh daemon subscription can resolve an uncertain operation."""
        rules = {rule.name: rule for rule in confirmed_rules}
        for entry in list(self.catalog.entries.values()):
            owner = entry.ownership
            if entry.node_id != node or not owner:
                continue
            actual = rules.get(owner.get('name'))
            if actual is None:
                self.catalog.set_ownership(entry.key, {})
            elif actual.fingerprint in (owner.get('fingerprint'), owner.get('previous_fingerprint')):
                if owner.get('uncertain'):
                    updated = dict(owner)
                    if actual.fingerprint == owner.get('previous_fingerprint') and 'previous_expires_at' in owner:
                        updated['expires_at'] = owner['previous_expires_at']
                    updated['fingerprint'] = actual.fingerprint
                    updated.pop('uncertain', None)
                    updated.pop('operation', None)
                    updated.pop('previous_fingerprint', None)
                    updated.pop('previous_expires_at', None)
                    self.catalog.set_ownership(entry.key, updated)
