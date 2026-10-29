"""GLib/Gio adapters for the shared live session. No Qt imports."""
from .apps import GLib, Gio
from .backend import InventoryBackend
from .prompts import PromptQueue
from opensnitch.core.session import ApplicationSession


class SaveTimer:
    def __init__(self):
        self.source = 0
        self.callback = None

    def start(self):
        self.stop()
        self.source = GLib.timeout_add(500, self._save)

    def _save(self):
        self.source = 0
        self.callback()
        return False

    def stop(self):
        if self.source:
            GLib.source_remove(self.source)
            self.source = 0


def dispatch(callback, *args):
    def run():
        callback(*args)
        return False
    GLib.idle_add(run)


class LiveBackend:
    def __init__(self, application, address, directory=None):
        import os
        from pathlib import Path
        directory = Path(directory) if directory else Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opensnitch'
        from opensnitch.core.preferences import ViewPreferences
        self.preferences = ViewPreferences(directory)
        self._closed = False
        self.application = application
        self.save_timer = SaveTimer()
        self.session = ApplicationSession(directory, self.save_timer, dispatch)
        self.save_timer.callback = self.session.catalog.flush
        self.prompts = PromptQueue(application, self.session.settings, self.session.identities.resolve,
            self.session.policies.application_rules, update_checksums=self.session.update_checksums)
        self.session.alert.connect(self._alert)
        self.session.message.connect(self._message)
        self.session.start(address, self.prompts.submit)
        self.metadata = InventoryBackend(directory)
        self._index = None
        self.metadata.subscribe(self._metadata_ready)
        from .systemd import SystemdCatalog
        from opensnitch.core.services import ServiceAccessController
        self.services = SystemdCatalog()
        self.services.changed.connect(self._services_ready)
        self.service_controls = ServiceAccessController(self.services, self.request_policy,
            self._service_action_changed, self._message)
        self._services_registered = False
        self._tick = GLib.timeout_add(1000, self._poll)

    def _metadata_ready(self, snapshot):
        index = self.metadata.metadata_index
        if index is not None and index is not self._index:
            self._index = index
            self.session.accept_index(index)

    def _services_ready(self):
        self.session.observe_services(self.services.records)

    def _service_action_changed(self):
        self.session.publish()

    def _poll(self):
        try:
            self.session.tick()
            if not self._services_registered and 'unix:/local' in self.session.nodes:
                self._services_registered = True
                self.session.observe_services(self.services.records)
        except Exception as error:
            self.session._error(str(error))
        return not self._closed

    @property
    def snapshot(self):
        return self.session.snapshot

    def subscribe(self, callback):
        return self.session.subscribe(callback)

    def request_interception(self, enabled):
        return self.session.request_interception(enabled)

    def request_policy(self, key, action, duration='always'):
        return self.session.request_policy(key, action, duration)

    def refresh(self):
        if self.services._active:
            self.services.refresh()
        self.metadata.refresh(reindex=True)
        self.session.refresh_policies()

    def _message(self, text):
        if self.application.window:
            self.application.window.show_message(text)

    def _alert(self, node, raw):
        from opensnitch.core.transport import pb
        alert = pb.Alert.FromString(raw)
        self._message(pb.Alert.Type.Name(alert.type).capitalize() + ': ' + alert.text)
        if alert.action == pb.Alert.SHOW_ALERT:
            notification = Gio.Notification.new('OpenSnitch · ' + pb.Alert.Type.Name(alert.type).capitalize())
            notification.set_body(alert.text)
            self.application.send_notification('daemon-alert-' + str(alert.id), notification)

    def close(self):
        if self._closed:
            return
        self._closed = True
        GLib.source_remove(self._tick)
        self.service_controls.cancel()
        self.services.close()
        self.metadata.close()
        self.prompts.close()
        self.session.close()
        self.save_timer.stop()
