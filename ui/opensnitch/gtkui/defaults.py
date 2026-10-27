"""Opt-in account defaults with exact backups; packaged launchers stay untouched."""
import json
import os
from pathlib import Path
from opensnitch.core.catalog import atomic_write
from .desktop import APPLICATION_ID, exec_argument, register_application


def locations(config_home=None, data_home=None, bin_home=None):
    config = Path(config_home or os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    data = Path(data_home or os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    binary = Path(bin_home or Path.home() / '.local/bin')
    return config, data, binary


def launcher_source(repository):
    # Self-contained so login can fall back to the installed UI if checkout moves.
    return '''#!/usr/bin/python3
"""Open the account's GTK frontend, with installed-UI fallback."""
import configparser
import os
from pathlib import Path
import socket
import subprocess
import sys
ROOT = Path(%r)
runner = ROOT / 'ui/bin/opensnitch-gtk-dev'
if runner.is_file():
    result = subprocess.run(['/usr/bin/python3', str(runner), '--default', *sys.argv[1:]])
    if result.returncode == 0:
        raise SystemExit(0)
if '--rollback' in sys.argv:
    raise SystemExit('The supervised GTK session is not available; defaults can be restored from its saved backups.')
# Never start another writer if a frontend already owns the configured endpoint.
config = configparser.ConfigParser(interpolation=None)
config.read(Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')/'opensnitch/settings.conf')
address = config.get('global', 'server_address', fallback='unix:///tmp/osui.sock')
if address.startswith('unix:///'):
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(address[7:])
    except OSError:
        pass
    else:
        raise SystemExit(0)
    finally:
        probe.close()
print('GTK checkout unavailable; starting installed OpenSnitch UI.', file=sys.stderr)
os.execv('/usr/bin/opensnitch-ui', ['/usr/bin/opensnitch-ui'])
''' % str(Path(repository).resolve())


def desktop_text(base, launcher, icon, start_hidden=False):
    replacements = {'Exec': exec_argument(launcher) + (' --start-hidden' if start_hidden else ''), 'Icon': str(icon),
                    'StartupWMClass': APPLICATION_ID, 'Hidden': 'false', 'X-GNOME-Autostart-enabled': 'true'}
    lines, seen, inside = [], set(), False
    for line in base.splitlines():
        if line.startswith('['):
            if inside:
                lines.extend(key + '=' + value for key, value in replacements.items() if key not in seen)
            inside = line == '[Desktop Entry]'
        key = line.split('=', 1)[0] if '=' in line else ''
        if inside and key in replacements:
            line = key + '=' + replacements[key]; seen.add(key)
        lines.append(line)
    if inside:
        lines.extend(key + '=' + value for key, value in replacements.items() if key not in seen)
    return '\n'.join(lines) + '\n'


def install(repository, config_home=None, data_home=None, bin_home=None):
    config, data, binary = locations(config_home, data_home, bin_home)
    state_dir = config / 'opensnitch'; state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = state_dir / 'gtk-default.json'
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if previous and previous.get('version') != 1:
        raise ValueError('Unknown default-frontend state version')
    launcher = binary / 'opensnitch-gtk'
    autostart = config / 'autostart/opensnitch_ui.desktop'
    desktop = data / 'applications/opensnitch_ui.desktop'
    targets = (launcher, autostart, desktop)
    backup = state_dir / 'default-frontend-backup'; backup.mkdir(exist_ok=True)
    originals = {}
    for index, path in enumerate(targets):
        if previous and previous.get('enabled'):
            originals[str(path)] = previous['originals'][str(path)]
        elif path.exists():
            saved = backup / str(index)
            atomic_write(saved, path.read_bytes())
            originals[str(path)] = {'backup': str(saved), 'mode': path.stat().st_mode & 0o777}
        else:
            originals[str(path)] = None
    _, icon = register_application(data)
    base = (Path(repository) / 'ui/resources/opensnitch_ui.desktop').read_text()
    contents = ((launcher, launcher_source(repository), 0o755),
        (autostart, desktop_text(autostart.read_text() if autostart.exists() else base, launcher, icon, start_hidden=True), 0o644),
        (desktop, desktop_text(desktop.read_text() if desktop.exists() else base, launcher, icon), 0o644))
    current = {str(path): (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None for path in targets}
    try:
        for path, text, mode in contents:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, text.encode()); path.chmod(mode)
        state = dict(version=1, enabled=True, repository=str(Path(repository).resolve()), launcher=str(launcher), originals=originals)
        atomic_write(manifest_path, json.dumps(state, indent=2).encode())
    except Exception:
        for path in targets:
            prior = current[str(path)]
            if prior:
                atomic_write(path, prior[0]); path.chmod(prior[1])
            else:
                path.unlink(missing_ok=True)
        raise
    # Refresh the app-ID entry as well, so pinned taskbar launches use defaults.
    register_application(data, launcher=launcher)
    return state


def restore(config_home=None, data_home=None, bin_home=None):
    config, data, binary = locations(config_home, data_home, bin_home)
    manifest_path = config / 'opensnitch/gtk-default.json'
    state = json.loads(manifest_path.read_text())
    if state.get('version') != 1 or not state.get('enabled'):
        raise ValueError('GTK account defaults are not enabled')
    for path in (binary / 'opensnitch-gtk', config / 'autostart/opensnitch_ui.desktop', data / 'applications/opensnitch_ui.desktop'):
        original = state['originals'][str(path)]
        if original:
            atomic_write(path, Path(original['backup']).read_bytes()); path.chmod(original['mode'])
        else:
            path.unlink(missing_ok=True)
    state['enabled'] = False
    atomic_write(manifest_path, json.dumps(state, indent=2).encode())
    register_application(data)
    return state
