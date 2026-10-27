"""Read existing frontend preferences without loading Qt or rewriting settings."""
import configparser
from dataclasses import dataclass
from pathlib import Path

DURATIONS = ('once', '30s', '5m', '15m', '30m', '1h', '12h', 'until restart', 'always')


@dataclass(frozen=True)
class PromptSettings:
    action: str = 'deny'
    duration: str = '12h'
    timeout: int = 15
    target: int = 0
    advanced: bool = False
    destination: bool = False
    port: bool = False
    user: bool = False
    checksum: bool = False
    disabled: bool = False

    @classmethod
    def read(cls, directory):
        config = configparser.ConfigParser(interpolation=None)
        config.read(Path(directory) / 'settings.conf')
        def integer(key, default):
            try:
                return config.getint('global', key, fallback=default)
            except ValueError:
                return default
        def boolean(key):
            try:
                return config.getboolean('global', key, fallback=False)
            except ValueError:
                return False
        index = integer('default_duration', 6)
        return cls(action={0: 'deny', 1: 'allow', 2: 'reject'}.get(integer('default_action', 0), 'deny'),
            duration=DURATIONS[index] if 0 <= index < len(DURATIONS) else '12h',
            timeout=max(0, integer('default_timeout', 15)), target=integer('default_target', 0),
            advanced=boolean('default_popup_advanced'), destination=boolean('default_popup_advanced_dstip'),
            port=boolean('default_popup_advanced_dstport'), user=boolean('default_popup_advanced_uid'),
            checksum=boolean('default_popup_advanced_checksum'), disabled=boolean('disable_popups'))
