from threading import Lock
import configparser
import threading
import glob
import os
import re
import locale
import shlex
import shutil
from pathlib import Path

is_pyinotify_available = True
try:
    import pyinotify
except Exception as e:
    is_pyinotify_available = False
    print("Error importing pyinotify:", e)

def desktop_paths():
    roots = [os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')]
    roots.extend((os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share').split(':'))
    return tuple(dict.fromkeys(os.path.join(root, 'applications')
                               for root in roots if os.path.isabs(root)))


DESKTOP_PATHS = desktop_paths()


class LinuxDesktopParser(threading.Thread):
    def __init__(self, paths=None, locale_name=None, watch=True):
        threading.Thread.__init__(self)
        self.lock = Lock()
        self.daemon = True
        self.running = False
        self.apps = {}
        self.apps_by_name = {}
        self.paths = tuple(paths) if paths is not None else desktop_paths()
        self._locale_name = locale_name
        self.files = []
        self.ambiguous = set()
        self.get_locale()
        # some things are just weird
        # (not really, i don't want to keep track of parent pids
        # just because of icons though, this hack is way easier)
        self.fixes = {
            '/opt/google/chrome/chrome': '/opt/google/chrome/google-chrome',
            '/usr/lib/firefox/firefox': '/usr/lib/firefox/firefox.sh',
            '/usr/bin/pidgin.orig': '/usr/bin/pidgin'
        }

        self.terminal_icon = os.path.join(
           os.path.abspath(os.path.dirname(__file__)),
            "res/terminal.svg"
        )

        self.rebuild()
        if watch and is_pyinotify_available:
            self.start()

    def rebuild(self):
        apps = {}
        by_name = {}
        seen = set()
        ambiguous = set()
        files = []
        for directory in self.paths:
            for path in sorted(Path(directory).rglob('*.desktop')):
                desktop_id = str(path.relative_to(directory)).replace(os.sep, '-')
                files.append(str(path))
                if desktop_id in seen:
                    continue
                seen.add(desktop_id)
                entry = self._read_desktop_file(str(path))
                if entry is None:
                    continue
                command, info = entry
                for key in dict.fromkeys((command, os.path.realpath(command))):
                    if key in apps and apps[key][:2] != info[:2]:
                        ambiguous.add(key)
                    else:
                        apps[key] = info
                by_name.setdefault(os.path.basename(command), []).append(info)
        with self.lock:
            self.apps = apps
            self.ambiguous = ambiguous
            self.apps_by_name = {key: values[0] for key, values in by_name.items()
                                 if len(values) == 1}
            self.files = files

    def get_locale(self):
        try:
            self.locale = self._locale_name if self._locale_name is not None else locale.getlocale()[0]
            self.locale_country = self.locale.split("_")[0]
        except Exception:
            self.locale = ""
            self.locale_country = ""

    def _parse_exec(self, cmd):
        try:
            args = shlex.split(cmd)
            if not args:
                return ''
            if os.path.basename(args[0]) == 'env':
                args = args[1:]
                while args and ('=' in args[0] or args[0] == '--'):
                    args.pop(0)
                if not args or args[0].startswith('-'):
                    return ''
            executable = args[0].replace('%%', '%')
            # A shared launcher does not uniquely identify its child application.
            name = os.path.basename(executable)
            if name in ('flatpak', 'sh', 'bash', 'dash', 'java', 'node', 'perl', 'ruby', 'mono') or name.startswith('python'):
                return ''
            if '%' in executable:
                return ''
            if not os.path.isabs(executable):
                executable = shutil.which(executable) or ''
            return os.path.normpath(executable) if executable else ''
        except ValueError:
            return ''

    def _localized(self, parser, field):
        for key in (field + '[' + self.locale + ']',
                    field + '[' + self.locale_country + ']', field):
            value = parser.get('Desktop Entry', key, raw=True, fallback=None)
            if value:
                return value
        return None

    def get_app_description(self, parser):
        return self._localized(parser, 'Comment')

    @staticmethod
    def discover_app_icon(app_name):
        # more hacks
        # normally qt will find icons if the system is configured properly.
        # if it's not, qt won't be able to find the icon by using QIcon().fromTheme(""),
        # so we fallback to try to determine if the icon exist in some well known system paths.
        for idir in glob.glob('/usr/share/icons/*/*/apps/*'):
            if app_name in idir:
                return idir

        return None

    def _read_desktop_file(self, desktop_path):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        try:
            parser.read(desktop_path, encoding='utf8')
            if parser.getboolean('Desktop Entry', 'Hidden', fallback=False):
                return None
            if parser.get('Desktop Entry', 'Type', fallback='Application') != 'Application':
                return None
            command = self._parse_exec(parser.get('Desktop Entry', 'Exec', raw=True, fallback=''))
            name = self._localized(parser, 'Name')
            if not command or not name:
                return None
            icon = parser.get('Desktop Entry', 'Icon', raw=True, fallback=None)
            return command, (name, icon, self.get_app_description(parser), desktop_path)
        except (OSError, UnicodeError, configparser.Error, ValueError):
            return None

    def _parse_desktop_file(self, desktop_path):
        # Compatibility for callers monitoring a changed desktop entry.
        self.rebuild()

    def get_info_by_path(self, path, default_icon):
        fallback = (os.path.basename(path), default_icon, '', None)
        path = self.fixes.get(path, path)
        with self.lock:
            if path not in self.ambiguous and path in self.apps:
                info = self.apps[path]
                return info[0], info[1] or default_icon, info[2], info[3]
        return fallback

    def get_info_by_binname(self, name, default_icon):
        info = self.apps_by_name.get(os.path.basename(name))
        return info or (os.path.basename(name), default_icon, '', None)

    def run(self):
        self.running = True
        wm = pyinotify.WatchManager()
        notifier = pyinotify.Notifier(wm)

        def inotify_callback(event):
            self.rebuild()

        for p in self.paths:
            if os.path.exists(p):
                wm.add_watch(p,
                             pyinotify.IN_CLOSE_WRITE | pyinotify.IN_DELETE | pyinotify.IN_CREATE | pyinotify.IN_MOVED_TO | pyinotify.IN_MOVED_FROM,
                             inotify_callback)
        notifier.loop()
