"""One sqlite3 worker owns all history writes; schemas and identifiers are unchanged."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import sqlite3

from .schema import SCHEMA_VERSION, STATEMENTS
from opensnitch.application_policy import operator_dict
from opensnitch.proto import import_
pb, _ = import_()
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def date(timestamp=None):
    return (datetime.now() if timestamp is None else datetime.fromtimestamp(timestamp)).strftime(DATE_FORMAT)


class HistoryWriter:
    def __init__(self, path, dispatch, on_error, wal=False):
        self.path = str(path) if path else ':memory:'
        self.dispatch, self.on_error = dispatch, on_error
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='gtk-history')
        self._connection = None
        self._closed = False
        self.ready = self._executor.submit(self._open, wal)

    def _open(self, wal):
        connection = sqlite3.connect(self.path, timeout=2)
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version != SCHEMA_VERSION and (version != 0 or tables):
            connection.close()
            raise ValueError('Unsupported history schema; return to Qt to migrate it first')
        try:
            connection.execute('PRAGMA journal_mode=' + ('OFF' if self.path == ':memory:' else ('WAL' if wal else 'DELETE')))
            with connection:
                for statement in STATEMENTS:
                    connection.execute(statement)
                connection.execute('PRAGMA user_version=' + str(SCHEMA_VERSION))
        except Exception:
            connection.close()
            raise
        self._connection = connection

    def submit(self, operation, *args):
        if self._closed:
            raise RuntimeError('History writer is closed')
        future = self._executor.submit(self._run, operation, args)
        def finished(result):
            try:
                result.result()
            except Exception as error:
                self.dispatch(self.on_error, str(error))
        future.add_done_callback(finished)
        return future

    def request(self, operation, *args, callback):
        """Repository reads/UI operations report errors without disabling policies."""
        if self._closed:
            self.dispatch(callback, None, 'History repository is closed')
            return
        if operation not in ('activity', 'clear_activity', 'export_activity'):
            raise ValueError('Unsupported history operation')
        future = self._executor.submit(self._run, operation, args)
        def finished(result):
            try:
                value = result.result()
            except Exception as error:
                self.dispatch(callback, None, str(error))
            else:
                self.dispatch(callback, value, '')
        future.add_done_callback(finished)

    def _activity(self, query):
        from .activity import read_activity
        return read_activity(self._connection, query)

    def _clear_activity(self, anchor):
        return self._connection.execute('DELETE FROM connections WHERE rowid<=?', (anchor,)).rowcount

    def _export_activity(self, query, destination):
        from .activity import export_activity
        return export_activity(self._connection, query, destination)

    def _run(self, operation, args):
        self.ready.result()
        with self._connection:
            return getattr(self, '_' + operation)(*args)

    def _subscription(self, node, raw):
        config = pb.ClientConfig.FromString(raw)
        self._connection.execute('DELETE FROM rules WHERE node=?', (node,))
        for rule in config.rules:
            self._rule(node, rule.SerializeToString())
        self._connection.execute('INSERT OR REPLACE INTO nodes(addr,hostname,version,status,last_connection) VALUES (?,?,?,?,?)',
                                 (node, config.name, config.version, '✓ online', date()))

    def _rule(self, node, raw, delete=False):
        rule = pb.Rule.FromString(raw)
        if delete:
            self._connection.execute('DELETE FROM rules WHERE node=? AND name=?', (node, rule.name))
            return
        operator = operator_dict(rule.operator)
        data = json.dumps(operator['list']) if operator['type'] == 'list' else operator['data']
        self._connection.execute('INSERT OR REPLACE INTO rules '
            '(time,node,name,enabled,precedence,action,duration,operator_type,operator_sensitive,operator_operand,operator_data,description,nolog,created) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (date(), node, rule.name, str(rule.enabled), str(rule.precedence), rule.action, rule.duration,
             rule.operator.type, str(rule.operator.sensitive), rule.operator.operand, data,
             rule.description, str(rule.nolog), date(rule.created)))

    def _statistics(self, node, raw):
        stats = pb.Statistics.FromString(raw)
        for event in stats.events:
            c = event.connection
            self._connection.execute('INSERT OR IGNORE INTO connections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (date(event.unixnano / 1e9), node, event.rule.action, c.protocol, c.src_ip, str(c.src_port),
                 c.dst_ip, c.dst_host, str(c.dst_port), str(c.user_id), str(c.process_id), c.process_path,
                 ' '.join(c.process_args), c.process_cwd, event.rule.name))
            self._connection.execute('UPDATE rules SET time=? WHERE node=? AND name=?',
                                     (date(event.unixnano / 1e9), node, event.rule.name))
        self._connection.execute('UPDATE nodes SET daemon_version=?,daemon_uptime=?,daemon_rules=?,cons=?,cons_dropped=?,last_connection=? WHERE addr=?',
            (stats.daemon_version, str(timedelta(seconds=stats.uptime)), str(stats.rules), str(stats.connections), str(stats.dropped), date(), node))
        for table, values in (('hosts', stats.by_host), ('procs', stats.by_executable),
                              ('addrs', stats.by_address), ('ports', stats.by_port), ('users', stats.by_uid)):
            # Same combined statistics tables as the Qt frontend. Node-bound
            # provenance remains in connections and the in-memory snapshots.
            for key, hits in values.items():
                self._connection.execute('INSERT OR REPLACE INTO ' + table + '(what,hits) VALUES (?,?)', (key, hits))

    def _sockets(self, snapshot):
        # One node's new snapshot must never delete another node's records.
        self._connection.execute('DELETE FROM sockets WHERE node=?', (snapshot.node,))
        self._connection.executemany('INSERT INTO sockets '
            '(last_seen,node,src_port,src_ip,dst_ip,dst_port,proto,uid,inode,iface,family,state,cookies,rqueue,wqueue,expires,retrans,timer,proc_path,proc_comm,proc_pid) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ((date(snapshot.timestamp), row.node, str(row.source_port), row.source, row.destination,
              str(row.destination_port), str(row.protocol), str(row.uid), str(row.inode), row.interface,
              str(row.family), str(row.state), str(list(row.cookies)), str(row.receive_queue), str(row.send_queue),
              str(row.expires), str(row.retransmits), str(row.timer), row.executable, row.command,
              str(row.pid) if row.pid >= 0 else '') for row in snapshot.rows))

    def _alert(self, node, raw):
        alert = pb.Alert.FromString(raw)
        body = alert.text
        if alert.what == pb.Alert.KERNEL_EVENT:
            body += '\n' + alert.proc.path
        if node != 'unix:/local':
            body = 'node: ' + node + '\n\n' + body
        self._connection.execute('INSERT INTO alerts VALUES (?,?,?,?,?,?,?,?)',
            (date(), node, pb.Alert.Type.Name(alert.type), '', '',
             'KERNEL EVENT' if alert.what == pb.Alert.KERNEL_EVENT else 'GENERIC', body, 0))

    def _disconnect(self, node):
        self._connection.execute('UPDATE nodes SET status=? WHERE addr=?', ('✗ offline', node))

    def _query(self, sql, parameters=()):
        # Only used through internal repository adapters/tests, never user input.
        return tuple(self._connection.execute(sql, parameters))

    def close(self):
        if self._closed:
            return
        self._closed = True
        def finish():
            if self._connection is not None:
                self._connection.close()
        self._executor.submit(finish)
        self._executor.shutdown(wait=True)
