"""Telegram mirror general plugin. No tools or agent prompt modifications."""
from pathlib import Path

def register(ctx):
    from hermes_constants import get_hermes_home
    from .mirror import Mirror
    from .hooks import HookBridge
    from .configuration import load_settings

    home = Path(get_hermes_home()).resolve()
    settings = load_settings(home, ctx.profile_name)
    runtime = Mirror(home, settings)
    runtime.bridge = HookBridge(runtime, capacity=settings.get('progress_queue_limit', 256))
    for name, kind in HookBridge.NAMES.items():
        ctx.register_hook(name, runtime.bridge.callback(kind))
    ctx.on_unload(runtime.close)
    runtime.start()
