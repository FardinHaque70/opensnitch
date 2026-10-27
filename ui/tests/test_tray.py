"""Real SNI/D-Bus menu exchanges on a private session bus; no live daemon."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time
import unittest
from gi.repository import Gio, GLib
from opensnitch.core.backend import BackendSnapshot
from opensnitch.gtkui.tray import TrayIcon, snapshot_status, ITEM, MENU, ITEM_PATH, MENU_PATH, WATCHER

WATCHER_XML = '''<node><interface name="org.kde.StatusNotifierWatcher">
<method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
</interface></node>'''


class FakeWatcher:
    def __init__(self):
        self.items = []
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.object = self.bus.register_object('/StatusNotifierWatcher',
            Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0], self.register, None, None)
        self.acquire()

    def acquire(self):
        reply = self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'RequestName', GLib.Variant('(su)', (WATCHER, 4)), None, 0, 2000, None)
        if reply.unpack()[0] != 1:
            raise RuntimeError('Tests require a private bus without an existing tray watcher')

    def release(self):
        self.bus.call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
            'org.freedesktop.DBus', 'ReleaseName', GLib.Variant('(s)', (WATCHER,)), None, 0, 2000, None)

    def register(self, connection, sender, path, interface, method, params, invocation):
        self.items.append((sender, params.unpack()[0]))
        invocation.return_value(None)

    def close(self):
        self.release()
        self.bus.unregister_object(self.object)


def spin(predicate, timeout=4):
    end = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError('Timed out waiting for D-Bus fixture')
        while context.pending():
            context.iteration(False)
        time.sleep(.005)


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.pool.shutdown, wait=True)
        self.watcher = FakeWatcher(); self.addCleanup(self.watcher.close)
        self.actions, self.availability, self.tokens = [], [], []
        self.tray = TrayIcon(lambda: self.actions.append('open'), lambda: self.actions.append('quit'), self.availability.append, self.tokens.append)
        self.tray.start(); self.addCleanup(self.tray.close)
        spin(lambda: self.tray.registered)
        self.bus = self.watcher.bus

    def call(self, path, interface, method, params=None):
        future = self.pool.submit(self.bus.call_sync, self.tray.connection.get_unique_name(),
            path, interface, method, params, None, Gio.DBusCallFlags.NONE, 3000, None)
        spin(future.done)
        return future.result()

    def test_pixmap_registration_activation_and_cleanup(self):
        self.assertEqual(self.watcher.items[-1][1], ITEM_PATH)
        props = self.call(ITEM_PATH, 'org.freedesktop.DBus.Properties', 'GetAll', GLib.Variant('(s)', (ITEM,))).unpack()[0]
        self.assertEqual(props['Title'], 'OpenSnitch')
        self.assertEqual(props['Menu'], MENU_PATH)
        width, height, data = props['IconPixmap'][0]
        self.assertEqual((width, height, len(data)), (64, 64, 64 * 64 * 4))
        self.assertTrue(any(data))
        self.call(ITEM_PATH, ITEM, 'ProvideXdgActivationToken', GLib.Variant('(s)', ('test-token',)))
        self.assertEqual(self.tokens, ['test-token'])
        self.call(ITEM_PATH, ITEM, 'Activate', GLib.Variant('(ii)', (0, 0)))
        spin(lambda: self.actions == ['open'])
        self.tray.close()
        self.assertFalse(self.tray.registered)
        self.assertIsNone(self.tray.connection)

    def test_menu_status_filters_events_and_quit(self):
        snapshot = BackendSnapshot((), True, True, 'Interception paused · Access applies to the selected executable.')
        self.tray.update(snapshot)
        layout = self.call(MENU_PATH, MENU, 'GetLayout', GLib.Variant('(iias)', (0, -1, []))).unpack()
        revision, root = layout
        rows = {row[0]: row[1] for row in root[2]}
        self.assertEqual(rows[1]['label'], 'Open OpenSnitch')
        self.assertIn('Interception paused', rows[2]['label'])
        self.assertFalse(rows[2]['enabled'])
        self.assertEqual(rows[4]['label'], 'Quit OpenSnitch')
        self.call(MENU_PATH, MENU, 'Event', GLib.Variant('(isvu)', (1, 'clicked', GLib.Variant('i', 0), 0)))
        spin(lambda: self.actions == ['open'])
        group = self.call(MENU_PATH, MENU, 'EventGroup', GLib.Variant('(a(isvu))', ([(2, 'clicked', GLib.Variant('i', 0), 0), (999, 'clicked', GLib.Variant('i', 0), 0)],))).unpack()
        self.assertEqual(group, ([999],))
        self.assertEqual(self.actions, ['open'])
        self.call(MENU_PATH, MENU, 'Event', GLib.Variant('(isvu)', (4, 'clicked', GLib.Variant('i', 0), 0)))
        spin(lambda: self.actions == ['open', 'quit'])
        filtered = self.call(MENU_PATH, MENU, 'GetGroupProperties', GLib.Variant('(aias)', ([1, 2], ['label']))).unpack()[0]
        self.assertTrue(all(set(props) == {'label'} for item, props in filtered))
        self.tray.update(BackendSnapshot(()))
        self.assertEqual(self.tray.status, 'Daemon disconnected')
        self.assertGreater(self.tray.revision, revision)
        self.assertIn('Updating rules', snapshot_status(replace(snapshot, authoritative=False)))

    def test_watcher_restart_reregisters_once(self):
        count = len(self.watcher.items)
        self.watcher.release()
        spin(lambda: not self.tray.registered)
        self.assertFalse(self.availability[-1])
        self.watcher.acquire()
        spin(lambda: self.tray.registered)
        self.assertEqual(len(self.watcher.items), count + 1)
        self.assertTrue(self.availability[-1])


if __name__ == '__main__': unittest.main()
