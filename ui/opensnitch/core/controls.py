"""Acknowledgment-aware application policy state; adapters own scheduling."""
from dataclasses import replace
import time
import uuid
from opensnitch.application_policy import ApplicationRule, ApplicationPolicyService, managed_rule_name
from .events import Event
from .rule_edit import validate_rule, validate_identifier, duration_seconds
from .rules import application_match


class AccessControllerCore:
    def __init__(self, catalog, policies, transport, online, apply_confirmed,
                 verify_current=None, timeout=15, dispatch=None, changed=None, message=None, verify_node=None):
        self.changed = changed if changed is not None else Event()
        self.message = message if message is not None else Event()
        self.dispatch = dispatch or (lambda callback, *args: callback(*args))
        self.catalog = catalog
        self.policies = policies
        self.transport = transport
        self.online = online
        self.apply_confirmed = apply_confirmed
        self.verify_current = verify_current or (lambda entry: '')
        self.timeout = timeout
        self.pending = {}
        self._general = {}
        self.verify_node = verify_node or (lambda node: '')
        self.last_error = ''

    def reason(self, entry):
        if self.general_pending(entry.node_id):
            return 'A rule editor operation is awaiting the daemon.'
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
        return self._queue(key, operation, rule)

    def general_pending(self, node):
        return any(value[0] == node for value in self._general.values())

    def rule_pending(self, node, name):
        return (node, '@rule/' + name) in self.pending

    def rule_reason(self, node):
        if self.catalog.read_only:
            return 'The application inventory cannot be saved.'
        if not self.online(node):
            return 'This computer is disconnected.'
        if any(key[0] == node for key in self.pending):
            return 'Wait for the pending rule operation.'
        return self.verify_node(node)

    def request_rule(self, node, proposed, expected=None, operation='change', callback=None):
        reason = self.rule_reason(node)
        current = self.policies.rule_by_name(node, proposed.name)
        try:
            if reason:
                raise ValueError(reason)
            if operation not in ('change', 'enable', 'disable', 'delete'):
                raise ValueError('Unsupported rule operation.')
            if expected is None:
                if operation != 'change' or current is not None:
                    raise ValueError('A rule with this identifier already exists, or the target is unavailable.')
            elif (current is None or current.name != expected.name
                  or current.policy_fingerprint != expected.policy_fingerprint):
                raise ValueError('This rule changed while it was open. Reload and review the current rule before saving.')
            if expected and proposed.name != expected.name:
                raise ValueError('Rule identifiers stay fixed while editing. Use Duplicate for a new identifier.')
            if operation == 'change' and expected is not None and proposed.policy_fingerprint == expected.policy_fingerprint:
                raise ValueError('No changes to save.')
            if operation in ('enable', 'disable') and (expected is None or proposed.policy_fingerprint != replace(expected, enabled=operation == 'enable').policy_fingerprint):
                raise ValueError('Enable/disable must preserve every other rule field.')
            if operation == 'delete':
                validate_identifier(proposed.name)
            else:
                validate_rule(proposed, expected)
            if not self.catalog.flush():
                raise ValueError('Could not save frontend inventory; no rule was sent.')
        except (ValueError, TypeError) as error:
            self.last_error = str(error)
            self.message.emit(self.last_error)
            return False
        self.last_error = ''
        return self._queue((node, '@rule/' + proposed.name), operation, proposed,
            (node, expected, callback or (lambda success, error: None)))

    def _queue(self, key, operation, rule, general=None):
        nonce = uuid.uuid4().hex
        self.pending[key] = (nonce, time.monotonic() + self.timeout, operation, rule)
        if general:
            self._general[nonce] = general
        self.changed.emit()
        try:
            self.transport(key[0], operation, rule,
                lambda success, error='': self.dispatch(self._receive_reply, nonce, success, error))
        except Exception as exc:
            self.dispatch(self._receive_reply, nonce, False, str(exc))
        return True

    def _receive_rule_reply(self, nonce, key, operation, rule, success, error):
        node, expected, callback = self._general[nonce]
        try:
            if success:
                # The server regenerates Created when reconstructing a rule.
                rule = replace(rule, created=int(time.time()))
                self.apply_confirmed(node, operation, rule)
                match = application_match(rule.operator)
                for entry in tuple(self.catalog.entries.values()):
                    if entry.node_id != node or entry.ownership.get('name') != rule.name:
                        continue
                    if operation == 'delete' or match.executable != entry.executable_path:
                        self.catalog.set_ownership(entry.key, {})
                    else:
                        owner = self._ownership(entry.key, rule)
                        seconds = duration_seconds(rule.duration)
                        if rule.enabled and seconds is not None:
                            owner['expires_at'] = time.time() + seconds
                        self.catalog.set_ownership(entry.key, owner)
                if not self.catalog.flush():
                    raise OSError('The daemon acknowledged the change, but ownership metadata could not be saved.')
        except Exception as failure:
            success, error = False, str(failure)
        finally:
            self._general.pop(nonce, None)
        self.changed.emit()
        callback(success, error or ('' if success else 'The daemon did not confirm the operation. Refresh and review its rules.'))

    def _receive_reply(self, nonce, success, error):
        for key, pending in tuple(self.pending.items()):
            if pending[0] != nonce:
                continue
            del self.pending[key]
            _, _, operation, rule = pending
            if nonce in self._general:
                self._receive_rule_reply(nonce, key, operation, rule, success, error)
                return
            if success:
                try:
                    # Update the UI mirror only after the backend acknowledges.
                    self.apply_confirmed(key[0], operation, rule)
                    owner = {} if operation == 'delete' else self._ownership(key, rule)
                    if operation != 'delete' and rule.duration in ('1h', '12h'):
                        owner['expires_at'] = time.time() + (3600 if rule.duration == '1h' else 43200)
                    self.catalog.set_ownership(key, owner)
                    self.message.emit('Application rule updated.')
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
                owner['previous_policy_fingerprint'] = previous.policy_fingerprint
                expires = self.catalog.entries[key].ownership.get('expires_at')
                if expires is not None:
                    owner['previous_expires_at'] = expires
            self.catalog.set_ownership(key, owner)
        except OSError:
            self.catalog.entries[key].ownership['uncertain'] = True

    @staticmethod
    def _ownership(key, rule):
        owner = {'name': rule.name, 'fingerprint': rule.fingerprint, 'policy_fingerprint': rule.policy_fingerprint}
        if rule.name != managed_rule_name(key[1]):
            owner.update(adopted=True, executable_path=key[1])
        return owner

    def check_timeouts(self):
        for key, pending in tuple(self.pending.items()):
            if pending[1] <= time.monotonic() or not self.online(key[0]):
                if pending[0] in self._general:
                    self._receive_reply(pending[0], False, 'Rule operation unconfirmed: timeout or disconnected computer. Refresh and review its rules.')
                    continue
                del self.pending[key]
                self._uncertain(key, pending[2], pending[3])
                self.message.emit('Rule change unconfirmed: timeout or disconnected node. Review its rules.')
                self.changed.emit()

    def reconcile(self, node, confirmed_rules, allow_rebase=False):
        """Reconcile metadata only from an authoritative daemon subscription.

        Legacy owners have no timestamp-independent baseline. Migrate them only
        when one exact executable rule still has the same owned identifier.
        New owners keep rejecting meaningful edits until explicit refresh.
        """
        rules = {rule.name: rule for rule in confirmed_rules}
        snapshot = ApplicationPolicyService()
        snapshot.replace_rules(node, rules.values())
        for entry in list(self.catalog.entries.values()):
            owner = entry.ownership
            if entry.node_id != node or not owner:
                continue
            actual = rules.get(owner.get('name'))
            if actual is None:
                self.catalog.set_ownership(entry.key, {})
                continue
            expected = owner.get('policy_fingerprint')
            previous = owner.get('previous_policy_fingerprint')
            matches_current = actual.policy_fingerprint == expected if expected else actual.fingerprint == owner.get('fingerprint')
            matches_previous = actual.policy_fingerprint == previous if previous else actual.fingerprint == owner.get('previous_fingerprint')
            target = snapshot.simple_application_rule(*entry.key)
            valid_owner = (owner.get('name') == managed_rule_name(entry.executable_path)
                           or (owner.get('adopted') and owner.get('executable_path') == entry.executable_path))
            can_rebase = valid_owner and target is not None and target.name == actual.name and (not expected or allow_rebase)
            if not (matches_current or matches_previous or can_rebase):
                continue
            updated = dict(owner)
            if matches_previous and 'previous_expires_at' in owner:
                updated['expires_at'] = owner['previous_expires_at']
            elif can_rebase and not (matches_current or matches_previous):
                updated.pop('expires_at', None)
            updated.update(fingerprint=actual.fingerprint, policy_fingerprint=actual.policy_fingerprint)
            for field in ('uncertain', 'operation', 'previous_fingerprint', 'previous_policy_fingerprint', 'previous_expires_at'):
                updated.pop(field, None)
            if updated != owner:
                self.catalog.set_ownership(entry.key, updated)
