"""Telegram mirror general plugin. No tools or agent prompt modifications."""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _noop(**_kwargs):
    return None


def register(ctx):
    from hermes_constants import get_hermes_home
    from .hooks import HookBridge
    from .configuration import settings_path

    home = Path(get_hermes_home()).resolve()
    path = settings_path(home)
    if not path.exists():
        # Unconfigured profile (fresh install, `hermes plugins validate`, another profile that
        # discovers the shared checkout): register the declared hooks as inert no-ops so the
        # capability surface is identical, and start no threads or network I/O.
        logger.info("telegram-mirror: no config at %s; hooks registered dormant", path)
        for name in HookBridge.NAMES:
            ctx.register_hook(name, _noop)
        return

    from .mirror import Mirror
    from .configuration import load_settings

    settings = load_settings(home, ctx.profile_name)
    runtime = Mirror(home, settings)
    runtime.bridge = HookBridge(runtime, capacity=settings.get('progress_queue_limit', 256))
    for name, kind in HookBridge.NAMES.items():
        ctx.register_hook(name, runtime.bridge.callback(kind))
    ctx.on_unload(runtime.close)
    runtime.start()
