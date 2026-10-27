"""Read-only compatibility checks before the development frontend handoff."""
import configparser
import os
from pathlib import Path
import sqlite3


def configuration_directory():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'opensnitch'


def check(directory=None):
    directory = Path(directory) if directory else configuration_directory()
    config = configparser.ConfigParser(interpolation=None)
    config.read(directory / 'settings.conf')
    if config.get('auth', 'type', fallback='simple') != 'simple':
        raise ValueError('Keep the Qt frontend for the configured TLS endpoint until GTK authentication is migrated')
    inventory = directory / 'applications.json'
    if inventory.exists():
        from opensnitch.core.catalog import CatalogCore
        class NoTimer:
            def stop(self): pass
        catalog = CatalogCore(inventory, NoTimer())
        if catalog.read_only:
            raise ValueError('Application inventory cannot be safely opened: ' + catalog.load_error)
    actions = directory / 'actions'
    if actions.is_dir() and any(path.is_file() and path.suffix == '.json' for path in actions.iterdir()):
        raise ValueError('Configured action hooks must be migrated before switching this frontend; keep Qt active')
    if config.get('database', 'type', fallback='0') != '0':
        path = Path(config.get('database', 'file'))
        if path.exists():
            connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                if connection.execute('PRAGMA user_version').fetchone()[0] != 3:
                    raise ValueError('Open this history database in Qt first to complete its existing migrations')
            finally:
                connection.close()
    address = config.get('global', 'server_address', fallback='unix:///tmp/osui.sock')
    if not address.startswith('unix:///'):
        raise ValueError('The GTK development handoff currently requires a Unix socket')
    return address
