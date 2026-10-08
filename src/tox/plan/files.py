from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from tox.config.set_env import SetEnv

if TYPE_CHECKING:
    from tox.config.sets import ConfigSet
    from tox.tox_env.python.pip.req_file import _PythonRequirementsFile
    from tox.util.json_types import JsonValue


def hash_file(path: Path, role: str, *, root: Path | None = None) -> dict[str, JsonValue]:
    resolved = path.resolve()
    data = resolved.read_bytes()
    relative_path: str | None = None
    if root is not None:
        try:
            relative_path = str(resolved.relative_to(root.resolve()))
        except ValueError:
            relative_path = None
    return {
        "role": role,
        "path": str(resolved),
        "relative_path": relative_path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def collect_referenced_files(config_set: ConfigSet, root: Path) -> list[dict[str, JsonValue]]:
    files: dict[Path, dict[str, JsonValue]] = {}

    def add(path: Path, role: str) -> None:
        resolved = path.resolve()
        if resolved.is_file() and resolved not in files:
            files[resolved] = hash_file(resolved, role, root=root)

    for definition in config_set.get_configs():
        key = next(iter(definition.keys))
        value = config_set.load(key)
        _collect_value_files(value, key, config_set, root, add)
    return list(files.values())


def _collect_value_files(
    value: object,
    key: str,
    config_set: ConfigSet,
    root: Path,
    add: Any,
) -> None:
    if isinstance(value, SetEnv):
        _ = list(value)
        for path in value.plan_file_paths:
            add(path, f"set_env:{key}")
    elif hasattr(value, "plan_local_paths"):
        requirements = _as_python_requirements(value)
        _ = requirements.requirements
        for path in requirements.plan_local_paths:
            add(path, f"requirements:{key}")
    if key == "base_python_file":
        for name in value if isinstance(value, list) else [value]:
            add(root / str(name), "base_python_file")
    if key == "pylock" and value:
        package_root = _package_root(config_set)
        add(package_root / str(value), "pylock")


def _as_python_requirements(value: Any) -> _PythonRequirementsFile:
    return cast("_PythonRequirementsFile", value)


def _package_root(config_set: ConfigSet) -> Path:
    if "package_root" in config_set:
        return config_set.get("package_root", Path)
    return config_set._conf.core.get("tox_root", Path)  # ruff:ignore[private-member-access]
