"""Small versioned GTK layout store, separate from legacy Qt settings."""
import json
from pathlib import Path
from .catalog import atomic_write


class ViewPreferences:
    def __init__(self, directory):
        self.path = Path(directory) / 'gtk-layout.json'
        self.views = {}
        self.read_only = False
        try:
            if self.path.exists():
                content = json.loads(self.path.read_text())
                if content.get('version') != 1 or not isinstance(content.get('views'), dict):
                    raise ValueError('Unsupported GTK layout preferences')
                self.views = content['views']
        except (OSError, ValueError, TypeError):
            self.read_only = True

    def get(self, view):
        value = self.views.get(view, {})
        return value if isinstance(value, dict) else {}

    def save(self, view, value):
        if self.read_only:
            return
        self.views[view] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, json.dumps({'version': 1, 'views': self.views}, ensure_ascii=False).encode())
