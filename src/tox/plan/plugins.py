from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from tox.plugin import spec
from tox.plugin.manager import MANAGER
from tox.version import version as tox_version

if TYPE_CHECKING:
    from tox.util.json_types import JsonValue


def capture_plugins(requires: list[str] | None = None) -> dict[str, JsonValue]:
    entrypoints = importlib.metadata.entry_points(group="tox")
    plugins = [_capture_plugin(plugin, entrypoints) for plugin in MANAGER.manager.get_plugins()]
    return {"tox_version": tox_version, "requires": requires or [], "plugins": plugins}


def _capture_plugin(plugin: Any, entrypoints: Any) -> dict[str, JsonValue]:
    module = _plugin_module(plugin)
    module_file = Path(module.__file__).resolve() if module.__file__ else None
    entrypoint = _entrypoint_for_module(module, entrypoints)
    role = "external"
    if plugin is MANAGER.inline_module:
        role = "inline"
    elif module.__name__.startswith("tox."):
        role = "internal"
    distributions = importlib.metadata.packages_distributions()
    distribution_names = distributions.get(module.__name__.split(".")[0], [])
    distribution = (
        distribution_names[0]
        if distribution_names
        else entrypoint.dist.name
        if entrypoint and entrypoint.dist
        else None
    )
    digest = _sha256(module_file) if module_file is not None else None
    return {
        "role": role,
        "name": getattr(plugin, "__name__", type(plugin).__name__),
        "module": module.__name__,
        "file": str(module_file) if module_file is not None else None,
        "entrypoint": entrypoint.name if entrypoint is not None else None,
        "distribution": distribution,
        "distribution_version": importlib.metadata.version(distribution) if distribution else None,
        "sha256": digest,
        "hooks": _hook_names(plugin),
    }


def _plugin_module(plugin: Any) -> ModuleType:
    module = inspect.getmodule(plugin)
    if module is not None:
        return module
    if isinstance(plugin, ModuleType):
        return plugin
    msg = f"plugin {plugin!r} has no importable module"
    raise TypeError(msg)


def _entrypoint_for_module(module: ModuleType, entrypoints: Any) -> Any:
    for entrypoint in entrypoints:
        if entrypoint.module == module.__name__:
            return entrypoint
    return None


def _hook_names(plugin: Any) -> list[str]:
    return sorted(name for name in dir(spec) if name.startswith("tox_") and callable(getattr(plugin, name, None)))


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
