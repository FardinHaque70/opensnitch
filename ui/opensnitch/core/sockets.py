"""Immutable node-bound socket snapshots and the daemon's read-only monitor task."""
from dataclasses import dataclass, replace
import json
import time
from .events import Event
from .socket_names import Family, Proto, State


@dataclass(frozen=True)
class SocketRecord:
    node: str
    source: str
    source_port: int
    destination: str
    destination_port: int
    protocol: int
    family: int
    state: int
    uid: int
    inode: int
    interface: str
    pid: int
    executable: str
    command: str
    cookies: tuple
    receive_queue: int
    send_queue: int
    expires: int
    retransmits: int
    timer: int
    mark: int

    @property
    def key(self):
        return (self.node, self.family, self.protocol, self.inode, self.source,
                self.source_port, self.destination, self.destination_port)

    @property
    def protocol_name(self):
        return Proto.get(str(self.protocol), str(self.protocol))

    @property
    def state_name(self):
        return State.get(str(self.state), str(self.state))

    @property
    def family_name(self):
        return Family.get(str(self.family), str(self.family))


def endpoint(address, port):
    return ('[' + address + ']' if ':' in address else address) + ':' + str(port)


def parse_sockets(node, raw):
    """Parse the existing daemon task format; malformed replies never replace data."""
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get('Table'), list):
        raise ValueError('Invalid socket table')
    processes = payload.get('Processes') or {}
    if not isinstance(processes, dict):
        raise ValueError('Invalid process map')
    records = {}
    for value in payload['Table']:
        if value is None:
            continue
        sock, pid = value['Socket'], int(value['PID'])
        address = sock['ID']
        process = processes.get(str(pid), {}) if pid >= 0 else {}
        if not isinstance(process, dict):
            raise ValueError('Invalid process record')
        path = process.get('Path', '')
        if not isinstance(path, str) or (path and not path.startswith('/')):
            path = ''
        record = SocketRecord(node, str(address['Source']), int(address['SourcePort']),
            str(address['Destination']), int(address['DestinationPort']), int(value['Proto']),
            int(sock['Family']), int(sock['State']), int(sock['UID']), int(sock['INode']),
            str(value.get('Iface') or address.get('Interface', '')), pid if process else -1,
            path, str(process.get('Comm', '')), tuple(address.get('Cookie') or ()),
            int(sock.get('RQueue', 0)), int(sock.get('WQueue', 0)), int(sock.get('Expires', 0)),
            int(sock.get('Retrans', 0)), int(sock.get('Timer', 0)), int(value.get('Mark', 0)))
        records[record.key] = record
    return tuple(records.values())


@dataclass(frozen=True)
class SocketSnapshot:
    node: str = ''
    rows: tuple[SocketRecord, ...] = ()
    timestamp: float = 0
    status: str = 'paused'
    message: str = 'Open Connections to load a socket snapshot.'


class SocketMonitor:
    INTERVALS = ('paused', '1s', '5s', '10s', '30s')

    def __init__(self, transport, online, accepted=lambda snapshot: None, clock=time.monotonic):
        self.transport, self.online, self.accepted, self.clock = transport, online, accepted, clock
        self.changed = Event()
        self.snapshot = SocketSnapshot()
        self.node, self.interval = 'unix:/local', '5s'
        self.protocol = self.family = self.state = 0
        self.active = False
        self._cancel = None
        self._monitor_node = ''
        self._serial = 0
        self._last_received = 0
        self._connected = False
        self._once = False

    def configure(self, node=None, interval=None, protocol=None, family=None, state=None, active=None):
        previous = self.node, self.interval, self.protocol, self.family, self.state, self.active
        for name, value in (('node', node), ('interval', interval), ('protocol', protocol),
                            ('family', family), ('state', state), ('active', active)):
            if value is not None:
                if name == 'interval' and value not in self.INTERVALS:
                    raise ValueError('Unsupported socket refresh interval')
                if name in ('protocol', 'family', 'state') and (not isinstance(value, int) or not 0 <= value <= 255):
                    raise ValueError('Invalid socket filter')
                setattr(self, name, value)
        current = self.node, self.interval, self.protocol, self.family, self.state, self.active
        if current != previous:
            if (self.node, self.protocol, self.family, self.state) != (previous[0], previous[2], previous[3], previous[4]):
                self.snapshot = SocketSnapshot(node=self.node)
            self.refresh()

    def _stop(self):
        self._serial += 1
        if self._cancel:
            self._cancel(); self._cancel = None
            self.transport.notify(self._monitor_node, self._notification(False))

    def _notification(self, start):
        from .transport import pb
        data = {'interval': ('1s' if self._once else self.interval), 'state': self.state, 'proto': self.protocol, 'family': self.family} if start else {}
        return pb.Notification(type=pb.TASK_START if start else pb.TASK_STOP,
            data=json.dumps({'name': 'sockets-monitor', 'data': data}))

    def refresh(self, once=False):
        self._stop()
        self._once = once and self.interval == 'paused'
        self._connected = self.online(self.node)
        if not self.active or (self.interval == 'paused' and not self._once):
            self._publish('paused', 'Paused · Showing the last socket snapshot.' if self.snapshot.timestamp else 'Socket monitoring paused.')
            return
        if not self._connected:
            self._publish('disconnected', 'Computer disconnected · The last snapshot is not current.')
            return
        self._publish('loading', 'Loading one socket snapshot…' if self._once else 'Loading sockets · Refresh every ' + self.interval)
        serial = self._serial
        self._last_received = self.clock()
        self._monitor_node = self.node
        self._cancel = self.transport.watch(self.node, self._notification(True),
            lambda success, data: self._receive(serial, success, data))

    def _receive(self, serial, success, data):
        if serial != self._serial:
            return
        if not success:
            self._publish('error', 'Socket monitoring failed: ' + data)
            return
        try:
            rows = parse_sockets(self.node, data)
        except (ValueError, TypeError, KeyError, IndexError) as error:
            self._publish('error', 'Invalid socket snapshot: ' + str(error))
            return
        self._last_received = self.clock()
        snapshot = SocketSnapshot(self.node, rows, time.time(), 'live', 'Live socket snapshot · Refresh every ' + self.interval)
        self.accepted(snapshot)
        self.snapshot = snapshot
        if self._once:
            self._stop()
            self.snapshot = replace(snapshot, status='paused', message='Paused · Showing the requested socket snapshot.')
        self.changed.emit(self.snapshot)

    def _publish(self, status, message):
        self.snapshot = replace(self.snapshot, node=self.node, status=status, message=message)
        self.changed.emit(self.snapshot)

    def connection_changed(self):
        connected = self.online(self.node)
        if connected != self._connected:
            self.refresh()

    def tick(self):
        if self.snapshot.status in ('loading', 'live') and self.clock() - self._last_received > max(15, (1 if self.interval == 'paused' else int(self.interval[:-1])) * 3):
            self._publish('stale', 'Socket updates have stopped · Showing the last snapshot. Refresh to retry.')

    def close(self):
        self._stop()
