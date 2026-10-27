"""Version-1 frontend inventory, independent of any GUI toolkit."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import os
import tempfile
import time
from .events import Event


def atomic_write(path, data):
    """Commit a private sibling file atomically; never truncate the old inventory."""
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class CatalogEntry:
    node_id: str
    executable_path: str
    sources: list = field(default_factory=list)
    first_seen: float = 0
    last_seen: float = 0
    hidden: bool = False
    ownership: dict = field(default_factory=dict)

    @property
    def key(self):
        return self.node_id, self.executable_path


class CatalogCore:
    VERSION = 1

    def __init__(self, path, save_timer, changed=None, error=None):
        self.changed = changed if changed is not None else Event()
        self.error = error if error is not None else Event()
        self._save_timer = save_timer
        self.path = Path(path)
        self.entries = {}
        self.read_only = False
        self.load_error = ''
        self._dirty = False
        try:
            if self.path.exists():
                content = json.loads(self.path.read_text())
                if content.get('version') != self.VERSION:
                    raise ValueError('Unsupported application inventory version')
                for row in content['apps']:
                    entry = CatalogEntry(**row)
                    if not isinstance(entry.node_id, str) or not os.path.isabs(entry.executable_path):
                        raise ValueError('Invalid application inventory entry')
                    if not isinstance(entry.sources, list) or not isinstance(entry.ownership, dict):
                        raise ValueError('Invalid application inventory metadata')
                    self.entries[entry.key] = entry
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Preserve unreadable/newer files instead of silently replacing them.
            self.entries = {}
            self.read_only = True
            self.load_error = str(exc)

    def observe_many(self, node_id, paths, source, observed_at=None):
        if not node_id:
            return
        changed = False
        now = time.time() if observed_at is None else observed_at
        for path in paths:
            if not isinstance(path, str) or not os.path.isabs(path):
                continue
            key = node_id, path
            entry = self.entries.get(key)
            if entry is None:
                entry = CatalogEntry(node_id, path, first_seen=now or time.time())
                self.entries[key] = entry
                changed = True
            if source not in entry.sources:
                entry.sources.append(source)
                changed = True
            if source == 'observed' and now > entry.last_seen:
                entry.last_seen = now
                changed = True
        if changed:
            self._modified()

    def hide(self, key, hidden=True):
        if key in self.entries and self.entries[key].hidden != hidden:
            self.entries[key].hidden = hidden
            self._modified()

    def set_ownership(self, key, ownership):
        if self.read_only:
            raise OSError(self.load_error or 'Application inventory is read-only')
        entry = self.entries[key]
        previous = entry.ownership
        entry.ownership = dict(ownership)
        self._dirty = True
        if not self.flush():
            entry.ownership = previous
            raise OSError('Could not persist application policy ownership')
        self.changed.emit()

    def _modified(self):
        self._dirty = True
        self._save_timer.start()
        self.changed.emit()

    def flush(self):
        self._save_timer.stop()
        if not self._dirty:
            return True
        if self.read_only:
            self.error.emit(self.load_error)
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            document = {'version': self.VERSION, 'apps': [asdict(e) for e in self.entries.values()]}
            data = json.dumps(document, ensure_ascii=False, sort_keys=True).encode('utf8')
            atomic_write(self.path, data)
            self._dirty = False
            return True
        except OSError as exc:
            self.error.emit(str(exc))
            return False
