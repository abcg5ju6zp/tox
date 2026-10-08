"""A tox environment that can build packages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from threading import RLock
from types import MethodType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from filelock import BaseFileLock, FileLock

from tox.util.typing_compat import override

from .api import ToxEnv, ToxEnvCreateArgs

_P = ParamSpec("_P")
_R = TypeVar("_R")
_FILE_LOCKED_METHODS = frozenset(
    {
        "perform_packaging",
        "setup",
        "teardown",
        "teardown_env",
        "_setup_env",
        "_setup_with_env",
        "_clean",
        "_build_package",
        "extract_install_info",
        "_run_recreate_commands",
    },
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from tox.config.main import Config
    from tox.config.sets import EnvConfigSet

    from .runner import RunToxEnv


class Package:
    """package."""


class PathPackage(Package):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    @override
    def __str__(self) -> str:
        return str(self.path)


def _lock_method(
    thread_lock: RLock,
    file_lock: Callable[[], BaseFileLock | None],
    meth: Callable[_P, _R],
) -> Callable[_P, _R]:
    def _func(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with thread_lock:
            active_file_lock = file_lock()
            file_locks = False
            if (
                active_file_lock is not None and active_file_lock.is_locked is False
            ):  # file_lock is to lock from other tox processes
                active_file_lock.acquire()
                file_locks = True
            try:
                return meth(*args, **kwargs)
            finally:
                if file_locks and active_file_lock is not None:
                    active_file_lock.release()

    return _func


class PackageToxEnv(ToxEnv, ABC):
    def __init__(self, create_args: ToxEnvCreateArgs) -> None:
        self._thread_lock = RLock()
        self._file_lock: BaseFileLock | None = None
        self._file_lock_path: Path | None = None
        super().__init__(create_args)
        self._envs: set[str] = set()

    @override
    def __getattribute__(self, name: str) -> Any:
        # the packaging class might be used by multiple environments in parallel, hold a lock for operations on it
        obj = object.__getattribute__(self, name)
        if isinstance(obj, MethodType) and name in _FILE_LOCKED_METHODS:
            obj = _lock_method(self._thread_lock, self._get_file_lock, obj)
        return obj

    @override
    def register_config(self) -> None:
        super().register_config()
        self._file_lock_path = self.conf.get("env_dir", Path) / "file.lock"
        self.core.add_config(
            keys=["package_root", "setupdir"],
            of_type=Path,
            default=self.core.get("tox_root", Path),
            desc="indicates where the packaging root file exists (historically setup.py file or pyproject.toml now)",
        )
        self.conf.add_config(
            keys=["package_root", "setupdir"],
            of_type=Path,
            default=self.core.get("package_root", Path),
            desc="indicates where the packaging root file exists (historically setup.py file or pyproject.toml now)",
        )

    def _get_file_lock(self) -> BaseFileLock | None:
        if self._file_lock is None and self._file_lock_path is not None:
            self._file_lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_lock = FileLock(self._file_lock_path)
        return self._file_lock

    @override
    def _recreate_default(self, conf: Config, value: str | None) -> bool:
        return self.options.no_recreate_pkg is False and super()._recreate_default(conf, value)

    @abstractmethod
    def perform_packaging(self, for_env: EnvConfigSet) -> list[Package]:
        raise NotImplementedError

    def register_run_env(self, run_env: RunToxEnv) -> Generator[tuple[str, str], PackageToxEnv, None]:  # ruff:ignore[unused-method-argument, no-self-use]
        return  # empty generator by default
        yield ("", "")  # unreachable, exists to establish yield type

    def mark_active_run_env(self, run_env: RunToxEnv) -> None:
        self._envs.add(run_env.conf.name)

    def teardown_env(self, conf: EnvConfigSet) -> None:
        if conf.name in self._envs:
            # conf.name (".tox") may be missing in self._envs in the case of an automatically provisioned environment
            self._envs.remove(conf.name)
        if len(self._envs) == 0:
            self._teardown()

    @abstractmethod
    def child_pkg_envs(self, run_conf: EnvConfigSet) -> Iterator[PackageToxEnv]:
        raise NotImplementedError
