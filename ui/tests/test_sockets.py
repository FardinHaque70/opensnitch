"""Socket snapshots keep provenance, raw identifiers and read-only task semantics."""
from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest
from opensnitch.core.sockets import SocketMonitor, SocketSnapshot, parse_sockets, endpoint
from opensnitch.core.history_writer import HistoryWriter
from opensnitch.core.preferences import ViewPreferences


def payload(path='/usr/bin/example', address='203.0.113.10', inode=42, pid=123):
    return json.dumps({'Table': [None, {'PID': pid, 'Proto': 6, 'Iface': 'eth0', 'Mark': 10,
        'Socket': {'ID': {'Source': '192.0.2.5', 'SourcePort': 55000, 'Destination': address, 'DestinationPort': 443,
                          'Interface': 2, 'Cookie': [123, 456]}, 'Family': 2, 'State': 1, 'UID': 1000,
                   'INode': inode, 'RQueue': 3, 'WQueue': 4, 'Expires': 5, 'Retrans': 6, 'Timer': 7}}],
        'Processes': {str(pid): {'Path': path, 'Comm': 'example'}}})


class Transport:
    def __init__(self):
        self.starts, self.stops, self.cancelled = [], [], []
    def watch(self, node, message, callback):
        self.starts.append((node, message, callback))
        return lambda: self.cancelled.append(node)
    def notify(self, node, message):
        self.stops.append((node, message))


class SocketTests(unittest.TestCase):
    def test_parser_preserves_node_numeric_fields_and_ipv6(self):
        local, = parse_sockets('unix:/local', payload(address='2001:db8::1'))
        remote, = parse_sockets('ipv4:192.0.2.1', payload(address='2001:db8::1'))
        self.assertNotEqual(local.key, remote.key)
        self.assertEqual((local.pid, local.executable, local.cookies, local.mark), (123, '/usr/bin/example', (123, 456), 10))
        self.assertEqual(local.protocol_name, 'TCP')
        self.assertEqual(endpoint(local.destination, local.destination_port), '[2001:db8::1]:443')
        unknown, = parse_sockets('remote', payload(pid=-1, path='relative/path'))
        self.assertEqual((unknown.pid, unknown.executable), (-1, ''))

    def test_monitor_repeated_snapshots_pause_and_stale_replies(self):
        transport = Transport(); accepted = []
        monitor = SocketMonitor(transport, lambda node: True, accepted.append)
        monitor.configure(active=True)
        node, message, reply = transport.starts[-1]
        self.assertEqual(json.loads(message.data)['data']['interval'], '5s')
        reply(True, payload()); reply(True, payload(inode=43))
        self.assertEqual(len(accepted), 2)
        self.assertEqual(monitor.snapshot.rows[0].inode, 43)
        monitor.configure(node='ipv4:192.0.2.1')
        self.assertEqual(transport.stops[-1][0], 'unix:/local')
        reply(True, payload(inode=999))
        self.assertEqual(monitor.snapshot.rows, ())
        transport.starts[-1][2](True, payload())
        self.assertEqual(monitor.snapshot.rows[0].node, 'ipv4:192.0.2.1')
        monitor.configure(interval='paused')
        self.assertEqual(monitor.snapshot.status, 'paused')
        self.assertTrue(monitor.snapshot.rows)
        monitor.close()

    def test_invalid_payload_error_disconnect_and_timeout_retain_last_snapshot(self):
        transport = Transport(); now = [1]; online = [True]
        monitor = SocketMonitor(transport, lambda node: online[0], clock=lambda: now[0])
        monitor.configure(active=True)
        reply = transport.starts[-1][2]
        reply(True, payload())
        old = monitor.snapshot.rows
        reply(True, '{bad')
        self.assertEqual(monitor.snapshot.status, 'error')
        self.assertEqual(monitor.snapshot.rows, old)
        monitor.refresh(); now[0] = 60; monitor.tick()
        self.assertEqual(monitor.snapshot.status, 'stale')
        online[0] = False; monitor.connection_changed()
        self.assertEqual(monitor.snapshot.status, 'disconnected')
        self.assertEqual(monitor.snapshot.rows, old)
        online[0] = True; monitor.connection_changed()
        self.assertEqual(monitor.snapshot.status, 'loading')
        transport.starts[-1][2](False, 'Fixture rejection')
        self.assertEqual(monitor.snapshot.status, 'error')
        monitor.close()

    def test_refresh_once_while_paused_stops_after_first_reply(self):
        transport = Transport()
        monitor = SocketMonitor(transport, lambda node: True)
        monitor.configure(active=True, interval='paused')
        self.assertFalse(transport.starts)
        monitor.refresh(once=True)
        self.assertEqual(json.loads(transport.starts[-1][1].data)['data']['interval'], '1s')
        transport.starts[-1][2](True, payload())
        self.assertEqual(monitor.snapshot.status, 'paused')
        self.assertEqual(len(transport.stops), 1)
        monitor.close()

    def test_socket_history_replaces_only_source_node_and_keeps_schema(self):
        errors = []
        writer = HistoryWriter(None, lambda callback, *args: callback(*args), errors.append)
        try:
            local = SocketSnapshot('unix:/local', parse_sockets('unix:/local', payload()), 1234)
            remote = SocketSnapshot('remote', parse_sockets('remote', payload()), 1234)
            writer.submit('sockets', local).result()
            writer.submit('sockets', remote).result()
            writer.submit('sockets', replace(local, rows=())).result()
            self.assertEqual(writer.submit('query', 'SELECT node,proc_path,src_port,proc_pid FROM sockets').result(),
                             (('remote', '/usr/bin/example', '55000', '123'),))
            self.assertEqual(writer.submit('query', 'PRAGMA user_version').result(), ((3,),))
            self.assertFalse(errors)
        finally:
            writer.close()

    def test_layout_preferences_atomic_roundtrip_and_unknown_versions_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            preferences = ViewPreferences(directory)
            preferences.save('connections', {'sort': 'pid', 'columns': []})
            self.assertEqual(ViewPreferences(directory).get('connections')['sort'], 'pid')
            path = Path(directory) / 'gtk-layout.json'; original = '{"version":999}'
            path.write_text(original)
            ViewPreferences(directory).save('connections', {})
            self.assertEqual(path.read_text(), original)


if __name__ == '__main__':
    unittest.main()
