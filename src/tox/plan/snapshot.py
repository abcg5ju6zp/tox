from __future__ import annotations

from typing import TYPE_CHECKING, cast

from tox.config.loader.memory import MemoryLoader
from tox.config.loader.native import to_native
from tox.config.of_type import ConfigDynamicDefinition
from tox.config.types import Command

if TYPE_CHECKING:
    from tox.config.main import Config
    from tox.config.sets import ConfigSet
    from tox.util.json_types import JsonValue


def snapshot_config_set(config_set: ConfigSet) -> dict[str, dict[str, JsonValue]]:
    values: dict[str, JsonValue] = {}
    sources: dict[str, JsonValue] = {}
    for definition in config_set.get_configs():
        key = next(iter(definition.keys))
        value = config_set.load(key)
        source = definition.plan_source if isinstance(definition, ConfigDynamicDefinition) else None
        if value is None and (source is None or source.get("type") == "default"):
            continue
        values[key] = _to_json(value)
        if isinstance(definition, ConfigDynamicDefinition):
            sources[key] = cast("JsonValue", source or {"type": "unknown", "key": key})
    return {"values": values, "sources": sources}


def snapshot_config(config: Config) -> dict[str, JsonValue]:
    envs: dict[str, JsonValue] = {}
    for name in config._env_to_conf_set:  # ruff:ignore[private-member-access]
        envs[name] = snapshot_config_set(config.get_env(name))
    return {"core": snapshot_config_set(config.core), "envs": envs}


def memory_loader(snapshot: dict[str, JsonValue], *, role: str = "execution-plan") -> MemoryLoader:
    values = snapshot.get("values")
    if not isinstance(values, dict):
        msg = "configuration snapshot must contain a values object"
        raise TypeError(msg)
    return MemoryLoader(role, **values)


def attach_config_snapshot(config: Config, snapshot: dict[str, JsonValue]) -> None:
    core = snapshot.get("core")
    if not isinstance(core, dict):
        msg = "execution plan config snapshot must contain a core object"
        raise TypeError(msg)
    envs = snapshot.get("envs")
    if not isinstance(envs, dict):
        msg = "execution plan config snapshot must contain an envs object"
        raise TypeError(msg)
    config.plan_core_snapshot = core
    config.plan_env_snapshots = envs


def _to_json(value: object) -> JsonValue:
    if isinstance(value, list) and value and all(isinstance(item, Command) for item in value):
        return [
            {
                "args": list(command.args),
                "ignore_exit_code": command.ignore_exit_code,
                "invert_exit_code": command.invert_exit_code,
            }
            for command in cast("list[Command]", value)
        ]
    return to_native(value)
