"""An unconfigured profile must register the declared hooks inertly and start nothing."""
import importlib
import sys
import types
from pathlib import Path


class _Ctx:
    profile_name = "default"

    def __init__(self):
        self.hooks = []
        self.unload = []

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))

    def on_unload(self, fn):
        self.unload.append(fn)


def test_unconfigured_profile_is_dormant(tmp_path, monkeypatch):
    fake = types.ModuleType("hermes_constants")
    fake.get_hermes_home = lambda: str(tmp_path)
    monkeypatch.setitem(sys.modules, "hermes_constants", fake)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root.parent))
    pkg = importlib.import_module(root.name)
    from importlib import reload
    pkg = reload(pkg)
    hooks_mod = importlib.import_module(f"{root.name}.hooks")
    ctx = _Ctx()
    pkg.register(ctx)
    assert {n for n, _ in ctx.hooks} == set(hooks_mod.HookBridge.NAMES)
    assert ctx.unload == []
    assert all(fn() is None for _, fn in ctx.hooks)
    assert not (tmp_path / "plugin-data").exists()
