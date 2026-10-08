from __future__ import annotations

import logging
import os
import signal
import sys
from types import FrameType, ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from tox.config.cli.parser import ToxParser
from tox.config.loader.memory import MemoryLoader
from tox.config.sets import ConfigSet, CoreConfigSet, EnvConfigSet
from tox.execute import Outcome
from tox.plugin import impl
from tox.plugin.manager import MANAGER, Plugin
from tox.pytest import ToxProjectCreator, register_inline_plugin
from tox.session.state import State
from tox.tox_env.api import ToxEnv
from tox.tox_env.register import REGISTER, ToxEnvRegister

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture


def _noop_command(state: State) -> int:  # ruff:ignore[unused-function-argument]
    return 0


def _ignore_signal(signum: int, frame: FrameType | None) -> None:  # ruff:ignore[unused-function-argument]
    return None


def test_plugin_hooks_and_order(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    @impl
    def tox_register_tox_env(register: ToxEnvRegister) -> None:
        assert isinstance(register, ToxEnvRegister)
        logging.warning("tox_register_tox_env")

    @impl
    def tox_add_option(parser: ToxParser) -> None:
        assert isinstance(parser, ToxParser)
        logging.warning("tox_add_option")

    @impl
    def tox_add_core_config(core_conf: CoreConfigSet, state: State) -> None:
        assert isinstance(core_conf, CoreConfigSet)
        assert isinstance(state, State)
        logging.warning("tox_add_core_config")

    @impl
    def tox_add_env_config(env_conf: EnvConfigSet, state: State) -> None:
        assert isinstance(env_conf, EnvConfigSet)
        assert isinstance(state, State)
        logging.warning("tox_add_env_config")

    @impl
    def tox_before_run_commands(tox_env: ToxEnv) -> None:
        assert isinstance(tox_env, ToxEnv)
        logging.warning("tox_before_run_commands")

    @impl
    def tox_on_install(tox_env: ToxEnv, arguments: Any, section: str, of_type: str) -> None:
        assert isinstance(tox_env, ToxEnv)
        assert arguments is not None
        assert isinstance(section, str)
        assert isinstance(of_type, str)
        logging.warning("tox_on_install %s %s", section, of_type)

    @impl
    def tox_after_run_commands(tox_env: ToxEnv, exit_code: int, outcomes: list[Outcome]) -> None:
        assert isinstance(tox_env, ToxEnv)
        assert exit_code == 0
        assert isinstance(outcomes, list)
        assert all(isinstance(i, Outcome) for i in outcomes)
        logging.warning("tox_after_run_commands")

    @impl
    def tox_env_teardown(tox_env: ToxEnv) -> None:
        assert isinstance(tox_env, ToxEnv)
        logging.warning("teardown")

    plugins = tuple(v for v in locals().values() if callable(v) and hasattr(v, "tox_impl"))
    assert len(plugins) == 8
    register_inline_plugin(mocker, *plugins)

    tox_ini = """
        [tox]
        env_list=a,b
        [testenv]
        package=skip
        commands=python -c 'print(1)'
        env_list=a,b
    """
    project = tox_project({"tox.ini": tox_ini})
    result = project.run("r", "-e", "a,b")
    result.assert_success()
    cmd = "print(1)" if sys.platform == "win32" else "'print(1)'"
    expected = [
        "ROOT: tox_register_tox_env",
        "ROOT: tox_add_option",
        "ROOT: tox_add_core_config",
        "a: tox_add_env_config",
        "b: tox_add_env_config",
        "a: tox_on_install PythonRun deps",
        "a: tox_before_run_commands",
        f"a: commands[0]> python -c {cmd}",
        mocker.ANY,  # output a
        "a: tox_after_run_commands",
        "a: teardown",
        mocker.ANY,  # report finished A
        "b: tox_on_install PythonRun deps",
        "b: tox_before_run_commands",
        f"b: commands[0]> python -c {cmd}",
        mocker.ANY,  # output b
        "b: tox_after_run_commands",
        "b: teardown",
        mocker.ANY,  # report a
        mocker.ANY,  # report b
        mocker.ANY,  # overall report
    ]
    assert result.out.splitlines() == expected, result.out


@pytest.mark.parametrize(
    "dir_name",
    [
        "tox_root",
        "work_dir",
        "temp_dir",
    ],
)
def test_plugin_can_set_core_conf(
    tox_project: ToxProjectCreator,
    mocker: MockerFixture,
    dir_name: str,
    tmp_path: Path,
) -> None:
    @impl
    def tox_add_core_config(core_conf: CoreConfigSet, state: State) -> None:  # ruff:ignore[unused-function-argument]
        core_conf.loaders.insert(0, MemoryLoader(**{dir_name: tmp_path}))

    register_inline_plugin(mocker, tox_add_core_config)

    project = tox_project({})
    result = project.run("c")
    result.assert_success()

    assert result.state.conf.core[dir_name] == tmp_path


def test_plugin_can_read_env_list(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    @impl
    def tox_add_core_config(core_conf: CoreConfigSet, state: State) -> None:  # ruff:ignore[unused-function-argument]
        logging.warning("All envs: %s", ", ".join(state.envs.iter(only_active=False)))
        logging.warning("Default envs: %s", ", ".join(state.envs.iter(only_active=True)))

    register_inline_plugin(mocker, tox_add_core_config)
    ini = """
    [tox]
    env_list = explicit
    [testenv]
    package = skip
    set_env =
        implicit: A=1
    [testenv:section]
    """
    project = tox_project({"tox.ini": ini})
    result = project.run()
    assert "ROOT: All envs: explicit, section, implicit" in result.out
    assert "ROOT: Default envs: explicit" in result.out


def test_plugin_can_read_sections(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    @impl
    def tox_add_core_config(core_conf: CoreConfigSet, state: State) -> None:  # ruff:ignore[unused-function-argument]
        logging.warning("Sections: %s", ", ".join(i.key for i in state.conf.sections()))

    register_inline_plugin(mocker, tox_add_core_config)
    ini = """
    [tox]
    [testenv]
    package = skip
    [testenv:section]
    [other:section]
    """
    project = tox_project({"tox.ini": ini})
    result = project.run()
    result.assert_success()
    assert "ROOT: Sections: tox, testenv, testenv:section, other:section" in result.out


def test_plugin_injects_invalid_python_run(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    @impl
    def tox_add_env_config(env_conf: EnvConfigSet, state: State) -> None:  # ruff:ignore[unused-function-argument]
        env_conf.loaders.insert(0, MemoryLoader(deps=[1]))
        with pytest.raises(TypeError, match="deps expected"):
            assert env_conf["deps"]

    register_inline_plugin(mocker, tox_add_env_config)
    project = tox_project({"tox.ini": "[testenv]\npackage=skip"})
    result = project.run()
    result.assert_failed()
    assert "raise TypeError(_factory_type_error" in result.out


def test_plugin_extend_pass_env(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    @impl
    def tox_add_env_config(env_conf: EnvConfigSet, state: State) -> None:  # ruff:ignore[unused-function-argument]
        env_conf["pass_env"].append("MAGIC_*")

    register_inline_plugin(mocker, tox_add_env_config)
    ini = """
    [testenv]
    package=skip
    commands=python -c 'import os; print(os.environ["MAGIC_1"]); print(os.environ["MAGIC_2"])'
    """
    project = tox_project({"tox.ini": ini})
    with patch.dict(os.environ, {"MAGIC_1": "magic_1", "MAGIC_2": "magic_2"}):
        result = project.run("r")
    result.assert_success()
    assert "magic_1" in result.out
    assert "magic_2" in result.out

    result_conf = project.run("c", "-e", "py", "-k", "pass_env")
    result_conf.assert_success()
    assert "MAGIC_*" in result_conf.out


def test_plugin_extend_set_env(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    @impl
    def tox_add_env_config(env_conf: EnvConfigSet, state: State) -> None:  # ruff:ignore[unused-function-argument]
        env_conf["set_env"].update({"MAGI_CAL": "magi_cal"})

    register_inline_plugin(mocker, tox_add_env_config)
    ini = """
    [testenv]
    package=skip
    commands=python -c 'import os; print(os.environ["MAGI_CAL"])'
    """
    project = tox_project({"tox.ini": ini})
    result = project.run("r")
    result.assert_success()
    assert "magi_cal" in result.out

    result_conf = project.run("c", "-e", "py", "-k", "set_env")
    result_conf.assert_success()
    assert "MAGI_CAL=magi_cal" in result_conf.out


def test_plugin_config_frozen_past_add_env(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    def _cannot_extend_config(config_set: ConfigSet) -> None:
        for conf in (
            lambda c: c.add_constant("c", "desc", "v"),
            lambda c: c.add_config("c", of_type=str, default="c", desc="d"),
        ):
            try:
                conf(config_set)  # call to not typed function
                raise NotImplementedError
            except RuntimeError as exc:  # ruff:ignore[try-except-in-loop]
                assert str(exc) == "config set has been marked final and cannot be extended"  # ruff:ignore[pytest-assert-in-except]

    @impl
    def tox_before_run_commands(tox_env: ToxEnv) -> None:
        _cannot_extend_config(tox_env.conf)
        _cannot_extend_config(tox_env.core)

    @impl
    def tox_after_run_commands(tox_env: ToxEnv, exit_code: int, outcomes: list[Outcome]) -> None:  # ruff:ignore[unused-function-argument]
        _cannot_extend_config(tox_env.conf)
        _cannot_extend_config(tox_env.core)

    register_inline_plugin(mocker, tox_before_run_commands, tox_after_run_commands)

    project = tox_project({"tox.ini": "[testenv]\npackage=skip"})
    result = project.run("r")
    result.assert_success()


@pytest.mark.parametrize(
    ("env_val", "expect_a", "expect_b"),
    [
        pytest.param("", True, True, id="none_disabled"),
        pytest.param("dummy_plugin_a,dummy_plugin_b", False, False, id="both_disabled"),
        pytest.param("dummy_plugin_a", False, True, id="only_a_disabled"),
        pytest.param("dummy_plugin_b", True, False, id="only_b_disabled"),
    ],
)
def test_disable_external_plugins(
    tox_project: ToxProjectCreator,
    env_val: str,
    expect_a: bool,
    expect_b: bool,
) -> None:
    class DummyPluginA:
        @staticmethod
        @impl
        def tox_add_option(parser: ToxParser) -> None:  # ruff:ignore[unused-static-method-argument]
            logging.warning("dummy plugin A called")

    class DummyPluginB:
        @staticmethod
        @impl
        def tox_add_option(parser: ToxParser) -> None:  # ruff:ignore[unused-static-method-argument]
            logging.warning("dummy plugin B called")

    def fake_discover_entry_points() -> list[tuple[Any, Any]]:
        dist_a = SimpleNamespace(metadata={"Name": "dummy-plugin-a"}, version="1")
        dist_b = SimpleNamespace(metadata={"Name": "dummy-plugin-b"}, version="1")
        return [
            (SimpleNamespace(name="dummy_plugin_a", load=DummyPluginA), dist_a),
            (SimpleNamespace(name="dummy_plugin_b", load=DummyPluginB), dist_b),
        ]

    project = tox_project({"tox.ini": ""})
    with (
        patch("tox.plugin.manager._discover_entry_points", fake_discover_entry_points),
        patch("tox.plugin.manager.MANAGER", Plugin()),
        patch.dict(os.environ, {"TOX_DISABLED_EXTERNAL_PLUGINS": env_val}, clear=False),
    ):
        result = project.run("--version")

    result.assert_success()
    assert ("dummy plugin A called" in result.out) == expect_a
    assert ("dummy plugin B called" in result.out) == expect_b


def _entry_point(name: str, load: Any, *, project: str | None = None, version: str = "1.0") -> tuple[Any, Any]:
    dist = SimpleNamespace(metadata={"name": project or name}, version=version)
    return SimpleNamespace(name=name, load=load), dist


def test_external_plugin_import_error_does_not_break_session(tox_project: ToxProjectCreator) -> None:
    class GoodPlugin:
        @staticmethod
        @impl
        def tox_add_option(parser: ToxParser) -> None:
            logging.warning("good plugin loaded")
            parser.add_argument("--good-plugin-flag", action="store_true")

    missing_dependency = "No module named 'broken_dependency'"

    def broken_load() -> Any:
        raise ImportError(missing_dependency)

    def fake_discover_entry_points() -> list[tuple[Any, Any]]:
        return [
            _entry_point("good_plugin", GoodPlugin),
            _entry_point("broken_plugin", broken_load),
        ]

    project = tox_project({"tox.ini": ""})
    with patch("tox.plugin.manager._discover_entry_points", fake_discover_entry_points):
        result = project.run("-h")

    result.assert_success()
    assert "good plugin loaded" in result.out
    assert "--good-plugin-flag" in result.out
    assert "external plugin 'broken_plugin' skipped: ImportError: No module named 'broken_dependency'" in result.out
    assert [error.name for error in MANAGER.load_errors] == ["broken_plugin"]
    # the good plugin still shows up in the version output, the broken one does not
    with patch("tox.plugin.manager._discover_entry_points", fake_discover_entry_points):
        version_result = project.run("--version")
    assert "good_plugin-1.0" in version_result.out
    assert "    broken_plugin" not in version_result.out  # not among the registered plugins


def test_external_plugin_command_conflict_is_skipped(tox_project: ToxProjectCreator) -> None:
    class GoodPlugin:
        @staticmethod
        @impl
        def tox_add_option(parser: ToxParser) -> None:
            parser.add_command("good-cmd", [], "the good command", _noop_command)

    class ConflictingPlugin:
        @staticmethod
        @impl
        def tox_add_option(parser: ToxParser) -> None:
            # clashes with the built-in run command
            parser.add_command("run", [], "duplicate run", _noop_command)

    def fake_discover_entry_points() -> list[tuple[Any, Any]]:
        return [
            _entry_point("good_plugin", GoodPlugin),
            _entry_point("conflicting_plugin", ConflictingPlugin),
        ]

    project = tox_project({"tox.ini": ""})
    with patch("tox.plugin.manager._discover_entry_points", fake_discover_entry_points):
        result = project.run("-h")

    result.assert_success()
    assert "good-cmd" in result.out
    assert "external plugin 'conflicting_plugin' skipped: ValueError: conflicting subparser: run" in result.out
    assert MANAGER.load_errors == (
        ("conflicting_plugin", "ValueError: conflicting subparser: run"),
    )


def test_external_plugin_built_for_other_tox_version_is_skipped(tox_project: ToxProjectCreator) -> None:
    class NewerToxPlugin:
        @staticmethod
        @impl
        def tox_nonexistent_hook() -> None: ...

    def fake_discover_entry_points() -> list[tuple[Any, Any]]:
        return [_entry_point("newer_tox_plugin", NewerToxPlugin)]

    project = tox_project({"tox.ini": ""})
    with patch("tox.plugin.manager._discover_entry_points", fake_discover_entry_points):
        result = project.run("l")

    result.assert_success()
    assert "external plugin 'newer_tox_plugin' skipped:" in result.out
    assert "tox_nonexistent_hook" in MANAGER.load_errors[0].detail
    assert not MANAGER.manager.has_plugin("newer_tox_plugin")


def test_rejected_plugin_cannot_take_over_signals(tox_project: ToxProjectCreator) -> None:
    previous_handler = signal.getsignal(signal.SIGINT)

    class SignalHijacker:
        @staticmethod
        @impl
        def tox_nonexistent_hook() -> None: ...

    def load_and_install_handler() -> Any:
        signal.signal(signal.SIGINT, _ignore_signal)
        return SignalHijacker()

    def fake_discover_entry_points() -> list[tuple[Any, Any]]:
        return [_entry_point("signal_hijacker", load_and_install_handler)]

    project = tox_project({"tox.ini": ""})
    with patch("tox.plugin.manager._discover_entry_points", fake_discover_entry_points):
        result = project.run("-h")

    result.assert_success()
    assert "external plugin 'signal_hijacker' skipped:" in result.out
    assert signal.getsignal(signal.SIGINT) is previous_handler
    assert not MANAGER.manager.has_plugin("signal_hijacker")


def test_registry_generations_do_not_mix(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    class CustomRunner:
        @staticmethod
        def id() -> str:
            return "custom-runner"

    inline_plugin = ModuleType("custom_runner_inline_plugin")
    inline_plugin.__file__ = "/virtual/toxfile.py"

    @impl
    def tox_register_tox_env(register: ToxEnvRegister) -> None:
        register.add_run_env(CustomRunner)  # type: ignore[arg-type]

    inline_plugin.tox_register_tox_env = tox_register_tox_env  # type: ignore[attr-defined]

    first = tox_project({"tox.ini": ""})
    mocker.patch("tox.plugin.manager.load_inline", return_value=inline_plugin)
    first_result = first.run("run", "-h")
    first_result.assert_success()
    assert "custom-runner" in first_result.out
    assert "custom-runner" in REGISTER.env_runners
    assert MANAGER.manager.is_registered(inline_plugin)
    generation_with_plugin = MANAGER.generation

    # config change: the next in-process run no longer has the inline plugin
    second = tox_project({"tox.ini": ""})
    mocker.patch("tox.plugin.manager.load_inline", return_value=None)
    second_result = second.run("run", "-h")
    second_result.assert_success()
    assert "custom-runner" not in second_result.out
    assert "custom-runner" not in REGISTER.env_runners
    assert not MANAGER.manager.is_registered(inline_plugin)
    assert list(REGISTER.env_runners) == ["virtualenv", "virtualenv-pep-723"]
    assert MANAGER.generation == generation_with_plugin + 1
