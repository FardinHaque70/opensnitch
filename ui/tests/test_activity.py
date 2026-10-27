"""History compatibility, safe query semantics, stable pagination and exports."""
from dataclasses import replace
from pathlib import Path
from queue import Queue
import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from opensnitch.core.activity import ActivityQuery, ActivityPage, ActivityBrowser, read_activity, export_activity
from opensnitch.core.schema import STATEMENTS
from opensnitch.core.history_writer import HistoryWriter


def populate(connection, count=8, start=0):
    for i in range(start, start + count):
        connection.execute('INSERT INTO connections VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (f'2026-01-01 12:{i:02}:00', 'unix:/local' if i % 2 == 0 else 'remote',
             'allow' if i % 2 == 0 else 'deny', 'tcp', '192.0.2.5', str(5000 + i), '203.0.113.10', 'example.test',
             '443', '1000', str(i), '/usr/bin/example', '--example, "quoted"', '/a path', 'original-rule'))
    connection.commit()


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        for statement in STATEMENTS:
            self.db.execute(statement)
        populate(self.db)

    def test_existing_history_fields_identifiers_and_recent_first(self):
        page = read_activity(self.db, ActivityQuery(limit=3))
        self.assertEqual((page.total, len(page.rows), page.rows[0].pid), (8, 3, '7'))
        self.assertEqual(page.nodes, ('remote', 'unix:/local'))
        self.assertEqual(page.rows[0].rule, 'original-rule')
        self.assertEqual(page.rows[0].arguments, '--example, "quoted"')
        self.assertNotEqual(page.rows[0].key, page.rows[1].key)
        anchored = replace(ActivityQuery(limit=3), anchor=page.anchor, offset=3)
        populate(self.db, 2, start=8)  # New insertions cannot shift an older page.
        second = read_activity(self.db, anchored)
        self.assertEqual((second.total, second.rows[0].pid), (8, '4'))

    def test_bound_filters_casefold_friendly_names_and_no_sql_interpolation(self):
        query = ActivityQuery(node='unix:/local', action='allow', protocol='tcp', executable='/usr/bin/example')
        self.assertEqual(read_activity(self.db, query).total, 4)
        self.assertEqual(read_activity(self.db, replace(query, text="' OR 1=1 --")).total, 0)
        self.assertEqual(read_activity(self.db, replace(query, text='does-not-match-raw', name_matches=(('unix:/local', '/usr/bin/example'),))).total, 4)
        self.assertEqual(read_activity(self.db, replace(query, text='EXAMPLE.TEST')).total, 4)
        with self.assertRaises(ValueError):
            read_activity(self.db, replace(query, sort='time; DROP TABLE connections'))

    def test_export_all_filtered_pages_raw_values_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.csv'
            query = ActivityQuery(action='deny', limit=1)
            self.assertEqual(export_activity(self.db, query, path), 4)
            with path.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]['process'], '/usr/bin/example')
            self.assertEqual(rows[0]['process_args'], '--example, "quoted"')
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                export_activity(self.db, replace(query, sort='invalid'), path)
            self.assertEqual(path.read_bytes(), original)

    def test_worker_clear_keeps_new_events_inventory_and_rules(self):
        errors = []
        writer = HistoryWriter(None, lambda fn, *args: fn(*args), errors.append)
        self.addCleanup(writer.close)
        writer.ready.result()
        # Isolated legacy records with no schema migration.
        for i in (1, 2):
            writer.submit('query', 'INSERT INTO connections(time,node,process,pid) VALUES (?,?,?,?)', ('2026', 'local', '/example', str(i))).result()
        writer.submit('query', 'INSERT INTO rules(node,name) VALUES (?,?)', ('local', 'keep')).result()
        callbacks = Queue()
        writer.request('activity', ActivityQuery(), callback=lambda *args: callbacks.put(args))
        page, error = callbacks.get(timeout=2)
        self.assertFalse(error)
        writer.submit('query', 'INSERT INTO connections(time,node,process,pid) VALUES (?,?,?,?)', ('2027', 'local', '/example', '3')).result()
        writer.request('clear_activity', page.anchor, callback=lambda *args: callbacks.put(args))
        count, error = callbacks.get(timeout=2)
        self.assertEqual((count, error), (2, ''))
        self.assertEqual(writer.submit('query', 'SELECT pid FROM connections').result(), (('3',),))
        self.assertEqual(writer.submit('query', 'SELECT name FROM rules').result(), (('keep',),))
        self.assertFalse(errors)

    def test_browser_rejects_late_filter_results_and_pauses_on_old_pages(self):
        class Repository:
            def __init__(self): self.calls = []
            def request(self, *args, callback): self.calls.append((args, callback))
        repository = Repository(); browser = ActivityBrowser(repository)
        browser.set_active(True)
        browser.configure(text='latest-filter')
        self.assertEqual(len(repository.calls), 1)
        repository.calls[0][1](ActivityPage(total=99, anchor=99), '')
        self.assertEqual(browser.snapshot.total, 0)
        self.assertEqual(len(repository.calls), 2)
        repository.calls[1][1](ActivityPage(total=500, limit=250, anchor=500), '')
        browser.page(1)
        self.assertFalse(browser.live)
        args, callback = repository.calls[-1]
        self.assertEqual((args[1].offset, args[1].anchor), (250, 500))
        callback(ActivityPage(total=500, offset=250, anchor=500), '')
        before = len(repository.calls); browser.history_changed()
        self.assertEqual(len(repository.calls), before)
        browser.close()

    def test_activity_core_import_requires_no_ui_toolkit(self):
        subprocess.run([sys.executable, '-c', "import opensnitch.core.activity,sys; assert not any(n.startswith(('gi','PyQt')) for n in sys.modules)"], check=True)


if __name__ == '__main__':
    unittest.main()
