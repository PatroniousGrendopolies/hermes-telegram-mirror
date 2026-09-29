"""Profile-local configuration for a shared, read-only code checkout."""
from pathlib import Path
import yaml


def load_settings(home: Path, profile: str) -> dict:
    path = Path(home) / 'plugin-data' / 'telegram-mirror' / 'mirror.yaml'
    settings = yaml.safe_load(path.read_text())
    if not isinstance(settings, dict) or settings.get('profile') != profile:
        raise ValueError('telegram-mirror configuration belongs to a different profile')
    if settings.get('host', 'gateway') not in {'gateway', 'standalone'}:
        raise ValueError('telegram-mirror host must be gateway or standalone')
    return settings
