from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tox.config.types import Command, EnvList
from tox.util.typing_compat import override

from .api import Loader
from .section import Section
from .str_convert import StrConvert

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tox.config.main import Config


class MemoryLoader(Loader[object]):
    """Loads configuration directly from data in memory."""

    plan_role = "memory"

    def __init__(self, role: str = "memory", /, **kwargs: Any) -> None:
        super().__init__(Section(prefix="<memory>", name=str(id(self))), [])
        self.plan_role = role
        self.raw: dict[str, Any] = {**kwargs}

    @override
    def load_raw(self, key: str, conf: Config | None, env_name: str | None) -> object:
        return self.raw[key]

    @override
    def found_keys(self) -> set[str]:
        return set(self.raw.keys())

    @staticmethod
    @override
    def to_bool(value: Any) -> bool:
        return bool(value)

    @staticmethod
    @override
    def to_str(value: Any) -> str:
        return str(value)

    @staticmethod
    @override
    def to_list(value: Any, of_type: type[Any]) -> Iterator[Any]:
        return iter(value)

    @staticmethod
    @override
    def to_set(value: Any, of_type: type[Any]) -> Iterator[Any]:
        return iter(value)

    @staticmethod
    @override
    def to_dict(value: Any, of_type: tuple[type[Any], type[Any]]) -> Iterator[tuple[Any, Any]]:
        return iter(value.items())

    @staticmethod
    @override
    def to_path(value: Any) -> Path:
        return Path(value)

    @staticmethod
    @override
    def to_command(value: Any) -> Command:
        if isinstance(value, Command):
            return value
        if isinstance(value, str):
            return StrConvert.to_command(value)
        if isinstance(value, dict) and isinstance(value.get("args"), list):
            args = [str(argument) for argument in value["args"]]
            if value.get("ignore_exit_code", False):
                args.insert(0, "-")
            if value.get("invert_exit_code", False):
                args.insert(0, "!")
            return Command(args)
        msg = f"command expected Command, str, dict or list[str], got {type(value).__name__}: {value!r}"
        raise TypeError(msg)

    @staticmethod
    @override
    def to_env_list(value: Any) -> EnvList:
        if isinstance(value, EnvList):
            return value
        if isinstance(value, str):
            return StrConvert.to_env_list(value)
        if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
            return EnvList(value)
        msg = f"env_list expected EnvList, str or list[str], got {type(value).__name__}: {value!r}"
        raise TypeError(msg)
