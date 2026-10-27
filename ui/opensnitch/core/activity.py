"""Existing event history: immutable pages, parameterized queries and async browsing."""
from dataclasses import dataclass, replace
import csv
import os
from pathlib import Path
import tempfile
from .events import Event

FIELDS = ('time', 'node', 'action', 'protocol', 'src_ip', 'src_port', 'dst_ip', 'dst_host', 'dst_port',
          'uid', 'pid', 'process', 'process_args', 'process_cwd', 'rule')
SORTS = {'time': 'time', 'executable': 'process', 'action': 'action', 'remote': "dst_ip || ':' || dst_port",
         'local': "src_ip || ':' || src_port", 'host': 'dst_host', 'protocol': 'protocol', 'pid': 'pid',
         'uid': 'uid', 'rule': 'rule', 'arguments': 'process_args', 'cwd': 'process_cwd', 'node': 'node'}


@dataclass(frozen=True)
class ActivityRecord:
    identifier: int
    time: str
    node: str
    action: str
    protocol: str
    source: str
    source_port: str
    destination: str
    host: str
    destination_port: str
    uid: str
    pid: str
    executable: str
    arguments: str
    cwd: str
    rule: str

    @property
    def key(self):
        return self.node, self.identifier


@dataclass(frozen=True)
class ActivityQuery:
    text: str = ''
    node: str = ''
    action: str = ''
    protocol: str = ''
    executable: str = ''
    rule: str = ''
    since: str = ''
    sort: str = 'time'
    descending: bool = True
    offset: int = 0
    limit: int = 250
    anchor: int | None = None
    name_matches: tuple = ()


@dataclass(frozen=True)
class ActivityPage:
    rows: tuple[ActivityRecord, ...] = ()
    total: int = 0
    offset: int = 0
    limit: int = 250
    anchor: int = 0
    message: str = 'Open Activity to load recorded decisions.'
    error: str = ''
    nodes: tuple[str, ...] = ()


def activity_where(connection, query, anchor):
    if query.sort not in SORTS or not 1 <= query.limit <= 1000 or query.offset < 0:
        raise ValueError('Invalid activity query')
    clauses, parameters = ['rowid<=?'], [anchor]
    for field, value in (('node', query.node), ('action', query.action), ('protocol', query.protocol),
                         ('process', query.executable), ('rule', query.rule)):
        if value:
            clauses.append(field + '=?'); parameters.append(value)
    if query.since:
        clauses.append('time>=?'); parameters.append(query.since)
    if query.text.strip():
        # Python casefold handles localized names; no optional SQLite extension.
        names = frozenset(query.name_matches)
        connection.create_function('activity_casefold', 1, lambda value: str(value or '').casefold(), deterministic=True)
        connection.create_function('activity_name_match', 2, lambda node, path: int((node, path) in names))
        haystack = " || char(10) || ".join("COALESCE(" + field + ",'')" for field in FIELDS)
        clauses.append('(instr(activity_casefold(' + haystack + '),?)>0 OR activity_name_match(node,process))')
        parameters.append(query.text.strip().casefold())
    return ' AND '.join(clauses), tuple(parameters)


def activity_rows(connection, query, anchor):
    where, parameters = activity_where(connection, query, anchor)
    direction = ' DESC' if query.descending else ' ASC'
    sql = 'SELECT rowid,' + ','.join(FIELDS) + ' FROM connections WHERE ' + where
    sql += ' ORDER BY ' + SORTS[query.sort] + direction + ',rowid' + direction
    return sql, parameters


def read_activity(connection, query):
    anchor = query.anchor if query.anchor is not None else connection.execute('SELECT COALESCE(MAX(rowid),0) FROM connections').fetchone()[0]
    where, parameters = activity_where(connection, query, anchor)
    total = connection.execute('SELECT COUNT(*) FROM connections WHERE ' + where, parameters).fetchone()[0]
    offset = min(query.offset, ((max(total, 1) - 1) // query.limit) * query.limit)
    sql, parameters = activity_rows(connection, query, anchor)
    rows = tuple(ActivityRecord(int(row[0]), *(str(value) if value is not None else '' for value in row[1:])) for row in connection.execute(sql + ' LIMIT ? OFFSET ?', parameters + (query.limit, offset)))
    nodes = tuple(row[0] for row in connection.execute('SELECT DISTINCT node FROM connections ORDER BY node') if row[0])
    return ActivityPage(rows, total, offset, query.limit, anchor, nodes=nodes)


def export_activity(connection, query, destination):
    """Export all matching rows of a fixed snapshot atomically, on the worker."""
    anchor = query.anchor if query.anchor is not None else connection.execute('SELECT COALESCE(MAX(rowid),0) FROM connections').fetchone()[0]
    sql, parameters = activity_rows(connection, query, anchor)
    path = Path(destination)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    count = 0
    try:
        with os.fdopen(descriptor, 'w', newline='', encoding='utf-8') as output:
            writer = csv.writer(output); writer.writerow(FIELDS)
            for row in connection.execute(sql, parameters):
                writer.writerow(row[1:]); count += 1
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return count


class ActivityBrowser:
    def __init__(self, history, name_matches=lambda text: ()):
        self.history, self.name_matches = history, name_matches
        self.changed = Event()
        self.query = ActivityQuery()
        self.snapshot = ActivityPage()
        self.active, self.live = False, True
        self._busy = self._pending = self._closed = False
        self._revision = 0

    def set_active(self, active):
        self.active = active
        if active:
            self.refresh()

    def configure(self, **changes):
        self._revision += 1
        changes.setdefault('offset', 0)
        self.query = replace(self.query, **changes)
        self.refresh()

    def set_live(self, live):
        self.live = live
        self.query = replace(self.query, offset=0 if live else self.query.offset,
                             anchor=None if live else self.snapshot.anchor)
        self._revision += 1
        self.refresh()

    def history_changed(self):
        if self.active and self.live:
            self.refresh()

    def refresh(self, latest=False):
        if self._closed:
            return
        if latest:
            self._revision += 1
            self.query = replace(self.query, anchor=None, offset=0)
        if self._busy:
            self._pending = True
            return
        self._busy = True
        revision = self._revision
        query = replace(self.query, name_matches=tuple(self.name_matches(self.query.text)))
        def received(page, error):
            if self._closed:
                return
            self._busy = False
            if revision == self._revision:
                if error:
                    self.snapshot = replace(self.snapshot, error=error, message='History could not be loaded; retaining the previous page.')
                else:
                    self.query = replace(self.query, offset=page.offset)
                    if not self.live:
                        self.query = replace(self.query, anchor=page.anchor)
                    self.snapshot = replace(page, message='Live recorded decisions' if self.live else 'Paused history snapshot')
                self.changed.emit(self.snapshot)
            if self._pending:
                self._pending = False
                self.refresh()
        self.history.request('activity', query, callback=received)

    def page(self, direction):
        offset = max(0, self.snapshot.offset + direction * self.snapshot.limit)
        if offset >= self.snapshot.total and direction > 0:
            return
        self.live = False
        self._revision += 1
        self.query = replace(self.query, offset=offset, anchor=self.snapshot.anchor)
        self.refresh()

    def clear(self, callback):
        anchor = self.snapshot.anchor
        self._revision += 1
        def cleared(count, error):
            if self._closed:
                return
            callback(count, error)
            if not error:
                self.refresh(latest=True)
        self.history.request('clear_activity', anchor, callback=cleared)

    def export(self, destination, callback):
        query = replace(self.query, anchor=self.snapshot.anchor,
                        name_matches=tuple(self.name_matches(self.query.text)))
        self.history.request('export_activity', query, destination, callback=callback)

    def close(self):
        self._closed = True
