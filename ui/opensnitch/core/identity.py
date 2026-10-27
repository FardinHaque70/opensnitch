"""Cached executable identity and metadata indexing without GUI objects."""
from dataclasses import dataclass
from pathlib import Path
import os
from opensnitch.desktop_parser import LinuxDesktopParser, desktop_paths
from .events import Event


@dataclass(frozen=True)
class ApplicationIdentity:
    node_id: str
    executable_path: str
    display_name: str
    icon_reference: str
    exact: bool
    icon_owner: str = ''
    icon_candidates: tuple[str, ...] = ()
    artwork_label: str = 'Application artwork: {0}'

    @property
    def tooltip(self):
        artwork = self.artwork_label.format(self.icon_owner) if self.icon_owner else ''
        return '\n'.join(filter(None, (self.display_name, self.executable_path, self.node_id, artwork)))


def icon_directories():
    roots = [os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')]
    roots.extend((os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':'))
    directories = [os.path.expanduser('~/.icons')]
    for root in roots:
        if os.path.isabs(root):
            directories.extend((os.path.join(root, 'icons'), os.path.join(root, 'pixmaps')))
    return tuple(dict.fromkeys(directories))


def packaged_companions(entries, ambiguous, package_root='/var/lib/pacman/local'):
    """Borrow artwork only for a frontend/backend pair proven in one package.

    This optional Arch manifest adapter never executes package tools or changes
    identity/policy. Missing manifests leave the normal generic fallback intact.
    """
    candidates = []
    for executable, entry in entries.items():
        path = Path(executable)
        if executable in ambiguous or not entry[1]:
            continue
        suffix = next((suffix for suffix in ('-ui', '-gui') if path.name.endswith(suffix)), None)
        if suffix:
            companion = str(path.with_name(path.name[:-len(suffix)]))
            if companion not in entries and Path(companion).is_file():
                candidates.append((executable, companion, entry))
    if not candidates:
        return {}, ()
    claims, manifests = {}, []
    for manifest in Path(package_root).glob('*/files'):
        try:
            files = set(manifest.read_text().splitlines())
        except (OSError, UnicodeError):
            continue
        for frontend, companion, entry in candidates:
            if frontend.lstrip('/') in files and companion.lstrip('/') in files:
                claims.setdefault(companion, set()).add(entry)
                manifests.append(str(manifest))
    return {path: next(iter(entries)) for path, entries in claims.items() if len(entries) == 1}, tuple(set(manifests))


def build_index(paths, icon_paths):
    parser = LinuxDesktopParser(paths=paths, watch=False)
    icons = {}
    for root in icon_paths:
        for directory, dirs, files in os.walk(root):
            dirs.sort()
            for filename in sorted(files):
                path = Path(directory) / filename
                if path.suffix.lower() in ('.png', '.xpm'):
                    icons.setdefault(path.stem, []).append(str(path))
    roots = {}
    ambiguous_roots = set()
    for executable, entry in parser.apps.items():
        directory = str(Path(executable).parent)
        if (executable in parser.ambiguous or not entry[1]
                or not any(directory.startswith(prefix) for prefix in ('/usr/lib/', '/usr/lib64/', '/opt/'))):
            continue
        # Never borrow artwork for a shared library/bin directory.
        # Require an app-specific directory named by its metadata.
        identifiers = {Path(executable).name.casefold(), Path(entry[3]).stem.casefold(),
                       Path(entry[1]).stem.casefold()}
        if Path(directory).name.casefold() not in identifiers:
            continue
        if directory in roots and roots[directory][:2] != entry[:2]:
            ambiguous_roots.add(directory)
        roots[directory] = entry
    for root in ambiguous_roots:
        roots.pop(root, None)
    companions, manifests = packaged_companions(parser.apps, parser.ambiguous)
    return parser.apps, parser.ambiguous, parser.files + list(manifests), icons, roots, companions


class IdentityCore:
    def __init__(self, changed=None, unknown_name='Unknown application', artwork_label='Application artwork: {0}'):
        self.changed = changed if changed is not None else Event()
        self.unknown_name = unknown_name
        self.artwork_label = artwork_label
        self._entries = {}
        self._ambiguous = set()
        self._bundle_roots = {}
        self._companions = {}
        self._icon_paths = {}
        self._local_nodes = set()
        self._cache = {}

    def accept_index(self, snapshot):
        self._entries, self._ambiguous = snapshot[:2]
        self._icon_paths = snapshot[3]
        self._bundle_roots = snapshot[4] if len(snapshot) > 4 else {}
        self._companions = snapshot[5] if len(snapshot) > 5 else {}
        self._cache.clear()

    def register_node(self, node_id, local):
        local = bool(local)
        was_local = node_id in self._local_nodes
        if local:
            self._local_nodes.add(node_id)
        else:
            self._local_nodes.discard(node_id)
        if was_local != local:
            self._cache.clear()
            self.changed.emit()

    def is_local_node(self, node_id):
        return node_id in self._local_nodes

    def installed_executables(self):
        return tuple(path for path in self._entries if path not in self._ambiguous)

    def resolve(self, node_id, executable_path):
        key = (node_id or '', executable_path or '')
        if key in self._cache:
            return self._cache[key]
        node_id, path = key
        name = os.path.basename(path.rstrip('/')) or self.unknown_name
        icon = ''
        exact = False
        icon_owner = ''
        lookup = os.path.normpath(path) if os.path.isabs(path) else ''
        if node_id in self._local_nodes and lookup and lookup not in self._ambiguous:
            entry = self._entries.get(lookup)
            if entry is not None:
                name, icon = entry[0], entry[1] or ''
                exact = True
            elif lookup in self._companions:
                owner = self._companions[lookup]
                icon, icon_owner = owner[1], owner[0]
            else:
                # Companion executables keep their own name and fallback
                # identity. Only the containing application's artwork is used.
                for directory in sorted(self._bundle_roots, key=len, reverse=True):
                    if lookup.startswith(directory + '/'):
                        owner = self._bundle_roots[directory]
                        icon, icon_owner = owner[1], owner[0]
                        break
        stem = Path(icon).stem if os.path.isabs(icon) or Path(icon).suffix in ('.png', '.svg', '.xpm') else icon
        candidates = tuple(self._icon_paths.get(stem, ())) if icon else ()
        identity = ApplicationIdentity(node_id, path, name, icon, exact, icon_owner, candidates, self.artwork_label)
        self._cache[key] = identity
        return identity
