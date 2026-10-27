"""Development desktop registration; never replaces the installed Qt launcher."""
import json
import os
from pathlib import Path
from opensnitch.core.catalog import atomic_write

APPLICATION_ID = 'io.github.opensnitch.GtkInventory'


def exec_argument(value):
    # Desktop Entry Exec quoting differs from shell quoting, including %%.
    value = str(value)
    if '\n' in value or '\r' in value:
        raise ValueError('Invalid desktop launcher path')
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    # The desktop key-value parser consumes one layer of backslashes first.
    return '"' + value.replace('\\', '\\\\') + '"'


def register_application(data_home=None, launcher=None):
    ui = Path(__file__).resolve().parents[2]
    directory = Path(data_home) if data_home is not None else Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
    icon = directory / 'icons/hicolor/64x64/apps' / (APPLICATION_ID + '.png')
    desktop = directory / 'applications' / (APPLICATION_ID + '.desktop')
    if launcher is None:
        marker = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opensnitch/gtk-default.json'
        try:
            state = json.loads(marker.read_text())
            if state.get('enabled') and Path(state['launcher']).is_file():
                launcher = Path(state['launcher'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    command = exec_argument(launcher) if launcher else '/usr/bin/python3 ' + exec_argument(ui / 'bin/opensnitch-gtk-dev')
    artwork = (ui / 'resources/icons/64x64/opensnitch-ui.png').read_bytes()
    text = ('[Desktop Entry]\nType=Application\nName=OpenSnitch (Development)\n'
            'Comment=GTK development frontend with automatic Qt rollback\n'
            'Exec=' + command + '\n'
            'Icon=' + str(icon) + '\nTerminal=false\nCategories=System;Security;Network;\n'
            'StartupWMClass=' + APPLICATION_ID + '\n'
            + ('NoDisplay=true\n' if launcher else ''))
    for path, contents in ((icon, artwork), (desktop, text.encode('utf-8'))):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != contents:
            atomic_write(path, contents)
            path.chmod(0o644)
    return desktop, icon
