"""Contains the plugin manager object."""

from __future__ import annotations

import importlib.metadata
import logging
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING, Any, NamedTuple

import pluggy
from pluggy._manager import DistFacade  # ruff:ignore[import-private-name] - pluggy does not export it, but its own loader uses this class

from tox import provision
from tox.config.loader import api as loader_api
from tox.session.cmd.run import parallel, sequential
from tox.tox_env import package as package_api
from tox.tox_env.python.virtual_env import pep723_runner, runner
from tox.tox_env.python.virtual_env.package import cmd_builder, pyproject
from tox.tox_env.register import REGISTER, ToxEnvRegister

from . import NAME, spec
from .inline import load_inline

if sys.version_info >= (3, 11):  # pragma: >=3.11 cover
    from typing import Self
else:  # pragma: <3.11 cover
    from typing_extensions import Self

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path
    from types import ModuleType

    from tox.config.cli.parser import ToxParser
    from tox.config.sets import ConfigSet, EnvConfigSet
    from tox.execute import Outcome
    from tox.session.state import State
    from tox.tox_env.api import ToxEnv
    from tox.tox_env.installer import InstallArguments

#: signals plugins may take over when they are imported; changes are tracked while a
#: candidate is vetted in the staging registry so a rejected plugin cannot leave its
#: handlers installed
_TRACKED_SIGNALS = ("SIGINT", "SIGTERM")


class PluginLoadError(NamedTuple):
    """Stable diagnostic for an external (optional) plugin that could not be activated."""

    name: str
    detail: str


def _describe(exception: BaseException) -> str:
    return f"{type(exception).__name__}: {exception}".strip()


def _discover_entry_points() -> Sequence[tuple[importlib.metadata.EntryPoint, importlib.metadata.Distribution]]:
    """Gather every entry point advertised for tox alongside its distribution.

    Mirrors what :func:`pluggy.PluginManager.load_setuptools_entrypoints` iterates, but
    returns the raw candidates so they can be imported and admitted one by one.
    """
    return [
        (entry_point, dist)
        for dist in list(importlib.metadata.distributions())
        for entry_point in dist.entry_points
        if entry_point.group == NAME
    ]


class _SignalGuard:
    """Snapshot tracked process signal handlers so they can be rolled back on rejection."""

    def __init__(self) -> None:
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> Self:
        if threading.current_thread() is threading.main_thread():
            self._previous = {
                sig: signal.getsignal(sig) for name in _TRACKED_SIGNALS if (sig := getattr(signal, name, None))
            }
        return self

    def restore(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        # restoration is explicit on rejection; on admission the handlers are kept
        return None


class Plugin:
    def __init__(self) -> None:
        self.manager: pluggy.PluginManager = self._new_manager()
        self.inline_module: ModuleType | None = None
        #: diagnostics for external plugins rejected while the current generation was built
        self.load_errors: tuple[PluginLoadError, ...] = ()
        #: incremented every time a freshly built registry generation replaces the live one
        self.generation: int = 0

    @staticmethod
    def _new_manager() -> pluggy.PluginManager:
        manager = pluggy.PluginManager(NAME)
        manager.add_hookspecs(spec)
        for name in os.environ.get("TOX_DISABLED_EXTERNAL_PLUGINS", "").split(","):
            manager.set_blocked(name)
        return manager

    @staticmethod
    def _internal_plugins() -> tuple[object, ...]:
        from tox.session import state  # ruff:ignore[import-outside-top-level]
        from tox.session.cmd import (  # ruff:ignore[import-outside-top-level]
            depends,
            devenv,
            exec_,
            legacy,
            list_env,
            man,
            quickstart,
            schema,
            show_config,
            version_flag,
        )

        return (
            loader_api,
            provision,
            pep723_runner,
            runner,
            pyproject,
            cmd_builder,
            legacy,
            version_flag,
            exec_,
            quickstart,
            show_config,
            schema,
            devenv,
            list_env,
            man,
            depends,
            parallel,
            sequential,
            package_api,
            state,
        )

    def _register_internal_plugins(self, manager: pluggy.PluginManager) -> None:
        for plugin in self._internal_plugins():
            manager.register(plugin)

    def _validate_external(self, plugin: object, name: str, admitted: Sequence[tuple[str, object]]) -> None:
        """Run all version/conflict checks for one candidate against throwaway registries.

        Raises on the first failed check; nothing it touches is shared with the live
        session, so a failure leaves no residue.
        """
        from tox.config.cli.parser import ToxParser  # ruff:ignore[import-outside-top-level]

        # hook contract/version validation in a registry containing only the candidate
        candidate = self._new_manager()
        candidate.register(plugin, name=name)
        candidate.check_pending()
        # a plugin unable to register its environment types is not usable either
        candidate.hook.tox_register_tox_env(register=ToxEnvRegister())

        # build the parser the committed generation would have so far, then let the
        # candidate extend it on its own, so any conflict is attributable to it alone
        probe = self._new_manager()
        self._register_internal_plugins(probe)
        for other_name, other in admitted:
            probe.register(other, name=other_name)
        parser = ToxParser.core()
        probe.hook.tox_add_option(parser=parser)
        candidate.hook.tox_add_option(parser=parser)

    def _admit_external(
        self,
        entry_point: importlib.metadata.EntryPoint,
        admitted: Sequence[tuple[str, object]],
    ) -> tuple[object | None, str | None]:
        """Vet one external plugin in isolation.

        The candidate is imported, checked against the hook specification (version
        check) and exercised against the CLI parser that the committed generation would
        have (command/option conflict check) using throwaway registries only. Signal
        handlers installed by a rejected candidate are rolled back.

        :returns: the imported plugin object on success, otherwise ``None`` and a detail
        """
        with _SignalGuard() as signal_guard:
            try:
                plugin = entry_point.load()
            except Exception as exception:  # ruff:ignore[blind-except] - import failures are diagnosed, never fatal
                signal_guard.restore()
                return None, _describe(exception)

            if entry_point.name in {name for name, _ in admitted} or any(plugin is other for _, other in admitted):
                # same distribution entry point name or plugin object already admitted
                signal_guard.restore()
                return None, "plugin already registered under a different distribution entry point"

            try:
                self._validate_external(plugin, entry_point.name, admitted)
            except Exception as exception:  # ruff:ignore[blind-except] - an incompatible optional plugin is skipped
                signal_guard.restore()
                return None, _describe(exception)
        return plugin, None

    def _build_generation(
        self,
        inline: ModuleType | None,
    ) -> tuple[pluggy.PluginManager, ToxEnvRegister, ModuleType | None, tuple[PluginLoadError, ...]]:
        """Discover and vet everything in staging registries without touching the live ones."""
        failures: list[PluginLoadError] = []
        admitted: list[tuple[str, object, importlib.metadata.Distribution]] = []
        seen: set[str] = set()
        for entry_point, dist in _discover_entry_points():
            # mirror pluggy: blocked names and duplicate entry point names are skipped silently
            if entry_point.name in seen:
                continue
            seen.add(entry_point.name)
            if self.manager.is_blocked(entry_point.name):
                continue
            plugin, detail = self._admit_external(entry_point, [(n, p) for n, p, _ in admitted])
            if plugin is None:
                error = PluginLoadError(entry_point.name, detail or "unknown error")
                failures.append(error)
                logging.warning("external plugin %r skipped: %s", entry_point.name, error.detail)
                continue
            admitted.append((entry_point.name, plugin, dist))

        staging = self._new_manager()
        if inline is not None:
            staging.register(inline)
        for name, plugin, dist in admitted:
            staging.register(plugin, name=name)
            staging._plugin_distinfo.append((plugin, DistFacade(dist)))  # ruff:ignore[private-member-access]
        self._register_internal_plugins(staging)
        active_inline = inline
        try:
            staging.check_pending()
        except pluggy.PluginValidationError:
            if inline is None:
                raise
            logging.warning("toxfile.py uses hooks not available in this tox version, skipping inline plugin")
            staging.unregister(inline)
            active_inline = None
            staging.check_pending()

        register = ToxEnvRegister()
        staging.hook.tox_register_tox_env(register=register)
        return staging, register, active_inline, tuple(failures)

    def tox_extend_envs(self) -> list[Iterable[str]]:
        additional_env_names_hook_value = self.manager.hook.tox_extend_envs()
        # NOTE: S101 is suppressed below to allow for type narrowing in MyPy
        assert isinstance(additional_env_names_hook_value, list)  # ruff:ignore[assert]
        return additional_env_names_hook_value

    def tox_add_option(self, parser: ToxParser) -> None:
        self.manager.hook.tox_add_option(parser=parser)

    def tox_add_core_config(self, core_conf: ConfigSet, state: State) -> None:
        self.manager.hook.tox_add_core_config(core_conf=core_conf, state=state)

    def tox_add_env_config(self, env_conf: EnvConfigSet, state: State) -> None:
        self.manager.hook.tox_add_env_config(env_conf=env_conf, state=state)

    def tox_register_tox_env(self, register: ToxEnvRegister) -> None:
        self.manager.hook.tox_register_tox_env(register=register)

    def tox_before_run_commands(self, tox_env: ToxEnv) -> None:
        self.manager.hook.tox_before_run_commands(tox_env=tox_env)

    def tox_after_run_commands(self, tox_env: ToxEnv, exit_code: int, outcomes: list[Outcome]) -> None:
        self.manager.hook.tox_after_run_commands(tox_env=tox_env, exit_code=exit_code, outcomes=outcomes)

    def tox_on_install(self, tox_env: ToxEnv, arguments: InstallArguments, section: str, of_type: str) -> None:
        self.manager.hook.tox_on_install(tox_env=tox_env, arguments=arguments, section=section, of_type=of_type)

    def tox_env_teardown(self, tox_env: ToxEnv) -> None:
        self.manager.hook.tox_env_teardown(tox_env=tox_env)

    def load_plugins(self, path: Path) -> None:
        """Build a new plugin generation in isolation and swap it in atomically.

        Discovery, hook/version validation, command conflict checks, environment type
        registration and signal takeover all happen against throwaway registries. Only
        once the whole generation is valid do the live pluggy registry and the tox
        environment register get replaced, as a single step, so:

        - a failing optional plugin is skipped with a stable diagnostic while every other
          valid plugin (and all built-ins) keeps working,
        - the previous generation stays live and untouched on failure,
        - repeated in-process runs (config changes/restarts) never mix two generations.
        """
        inline = _load_inline(path)
        staging, register, active_inline, failures = self._build_generation(inline)
        # commit point - below this line the new generation must take effect as a whole
        self.manager = staging
        self.inline_module = active_inline
        self.load_errors = failures
        REGISTER._adopt(register)  # ruff:ignore[private-member-access]
        self.generation += 1


def _load_inline(path: Path) -> ModuleType | None:  # used to be able to unregister plugin tests
    return load_inline(path)


MANAGER = Plugin()

__all__ = (
    "MANAGER",
    "Plugin",
    "PluginLoadError",
)
