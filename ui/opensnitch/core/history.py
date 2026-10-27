"""Read-only access to the existing history schema.

Connections are opened inside the calling worker, never shared across GUI threads.
The active frontend owns writes through its corresponding repository adapter.
"""
from pathlib import Path
import sqlite3


class HistoryReader:
    def __init__(self, path=None):
        self.path = Path(path) if path else None

    def activity(self):
        if self.path is None:
            return ()
        uri = self.path.resolve().as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            connection.execute('PRAGMA query_only=ON')
            return tuple(connection.execute(
                'SELECT node, process, MAX(time) FROM connections GROUP BY node, process'))
        finally:
            connection.close()
