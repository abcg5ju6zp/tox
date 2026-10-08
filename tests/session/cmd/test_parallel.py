from __future__ import annotations

import json
import os
import sys
from argparse import ArgumentTypeError
from signal import SIGINT, SIGKILL, SIGTERM
from subprocess import PIPE, Popen
from textwrap import dedent
from time import sleep
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from tox.session.cmd.run import common, parallel
from tox.session.cmd.run.parallel import parse_num_processes
from tox.tox_env.api import ToxEnv
from tox.tox_env.errors import Fail
from tox.util.cpu import auto_detect_cpus

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

    from tox.pytest import MonkeyPatch, ToxProjectCreator


def test_parse_num_processes_all() -> None:
    assert parse_num_processes("all") is None


def test_parse_num_processes_auto() -> None:
    auto = parse_num_processes("auto")
    assert isinstance(auto, int)
    assert auto > 0


def test_parse_num_processes_exact() -> None:
    assert parse_num_processes("3") == 3


def test_parse_num_processes_not_number() -> None:
    with pytest.raises(ArgumentTypeError, match="value must be a positive number"):
        parse_num_processes("3df")


def test_parse_num_processes_minus_one() -> None:
    with pytest.raises(ArgumentTypeError, match="value must be positive"):
        parse_num_processes("-1")


def test_parallel_zero_turns_off(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    # The -p help says "zero is turn off", so -p 0 must run sequentially (one
    # worker) rather than auto-detecting the CPU count like -p auto.
    execute = mocker.patch.object(parallel, "execute", return_value=0)
    project = tox_project({"tox.ini": "[tox]\nno_package=true\nenv_list=a,b\n[testenv]\ncommands=python -c 'pass'\n"})
    project.run("p", "-p", "0")

    assert execute.call_args.kwargs["max_workers"] == 1


def test_parallel_general(tox_project: ToxProjectCreator, monkeypatch: MonkeyPatch, mocker: MockerFixture) -> None:
    def setup(self: ToxEnv) -> None:
        if self.name == "f":
            msg = "something bad happened"
            raise Fail(msg)
        return prev_setup(self)

    prev_setup = ToxEnv._setup_env  # ruff:ignore[private-member-access]
    mocker.patch.object(ToxEnv, "_setup_env", autospec=True, side_effect=setup)
    monkeypatch.setenv("PATH", "")

    ini = """
    [tox]
    no_package=true
    skip_missing_interpreters = true
    env_list= a, b, c, d, e, f
    [testenv]
    commands=python -c 'print("run {env_name}")'
    depends = !c: c
    parallel_show_output = c: true
    [testenv:d]
    base_python = missing_skip
    [testenv:e]
    commands=python -c 'import sys; print("run {env_name}"); sys.exit(1)'
    """
    project = tox_project({"tox.ini": ini})
    outcome = project.run("p", "-p", "all")
    outcome.assert_failed()

    out = outcome.out
    oks, skips, fails = {"a", "b", "c"}, {"d"}, {"e", "f"}
    missing = set()
    for env in "a", "b", "c", "d", "e", "f":
        if env in {"c", "e"}:
            assert "run c" in out, out
        elif env == "f":
            assert "f: failed with something bad happened" in out, out
        else:
            assert f"run {env}" not in out, out
        of_type = "OK" if env in oks else ("SKIP" if env in skips else "FAIL")
        of_type_icon = "✔" if env in oks else ("⚠" if env in skips else "✖")
        env_done = f"{env}: {of_type} {of_type_icon}"
        is_missing = env_done not in out
        if is_missing:
            missing.add(env_done)
        env_report = f"  {env}: {of_type} {'code 1 ' if env in fails else ''}("
        assert env_report in out, out
        if not is_missing:
            assert out.index(env_done) < out.index(env_report), out
    assert len(missing) == 1, out


def test_parallel_run_live_out(tox_project: ToxProjectCreator) -> None:
    ini = """
    [tox]
    no_package=true
    env_list= a, b
    [testenv]
    commands=python -c 'print("run {env_name}")'
    """
    project = tox_project({"tox.ini": ini})
    outcome = project.run("p", "-p", "2", "--parallel-live")
    outcome.assert_success()
    assert "python -c" in outcome.out
    assert "run a" in outcome.out
    assert "run b" in outcome.out


def test_parallel_show_output_with_pkg(tox_project: ToxProjectCreator, demo_pkg_inline: Path) -> None:
    ini = "[testenv]\nparallel_show_output=True\ncommands=python -c 'print(\"r {env_name}\")'"
    project = tox_project({"tox.ini": ini})
    result = project.run("p", "--root", str(demo_pkg_inline), "--workdir", str(project.path / ".tox"))
    result.assert_success()
    assert "r py" in result.out


@pytest.mark.skipif(sys.platform == "win32", reason="You need a conhost shell for keyboard interrupt")
@pytest.mark.flaky(max_runs=3, min_passes=1)
def test_keyboard_interrupt(tox_project: ToxProjectCreator, demo_pkg_inline: Path, tmp_path: Path) -> None:
    marker = tmp_path / "a"
    ini = f"""
    [testenv]
    package=wheel
    commands=python -c 'from time import sleep; from pathlib import Path; \
                        p = Path("{marker!s}"); p.write_text(""); sleep(100)'
    [testenv:dep]
    depends=py
    """
    proj = tox_project(
        {
            "tox.ini": ini,
            "pyproject.toml": (demo_pkg_inline / "pyproject.toml").read_text(),
            "build.py": (demo_pkg_inline / "build.py").read_text(),
        },
    )
    cmd = ["-c", str(proj.path / "tox.ini"), "p", "-p", "1", "-e", f"py,py{sys.version_info[0]},dep"]
    process = Popen([sys.executable, "-m", "tox", *cmd], stdout=PIPE, stderr=PIPE, universal_newlines=True)
    while not marker.exists() and (process.poll() is None):
        sleep(0.05)
    process.send_signal(SIGINT)
    out, err = process.communicate()
    assert process.returncode != 0
    assert "KeyboardInterrupt" in err, err
    assert "KeyboardInterrupt - teardown started\n" in out, out
    assert "interrupt tox environment: py\n" in out, out
    assert "requested interrupt of" in out, out
    assert "send signal SIGINT" in out, out
    assert "interrupt finished with success" in out, out
    assert "interrupt tox environment: .pkg" in out, out
    assert "BrokenPipeError" not in out, out
    assert "BrokenPipeError" not in err, err
    assert "KeyError" not in out, out
    assert "KeyError" not in err, err


def test_parallels_help(tox_project: ToxProjectCreator) -> None:
    outcome = tox_project({"tox.ini": ""}).run("p", "-h")
    outcome.assert_success()


def test_parallel_legacy_accepts_no_arg(tox_project: ToxProjectCreator) -> None:
    outcome = tox_project({"tox.ini": ""}).run("-p", "-h")
    outcome.assert_success()


def test_parallel_requires_arg(tox_project: ToxProjectCreator) -> None:
    outcome = tox_project({"tox.ini": ""}).run("p", "-p", "-h")
    outcome.assert_failed()
    assert "argument -p/--parallel: expected one argument" in outcome.err


def test_parallel_all_empty_selection(tox_project: ToxProjectCreator) -> None:
    """``-p all`` with nothing selected must not crash the driver thread with a ValueError."""
    project = tox_project({"tox.toml": 'env_list = ["py"]\n[env_run_base]\npackage = "skip"\n'})
    outcome = project.run("p", "-p", "all", "--skip-env", ".*")
    outcome.assert_failed()  # nothing to run gracefully fails evaluation
    assert "max_workers must be greater than 0" not in outcome.out
    assert "Traceback" not in outcome.out


def test_parallel_no_spinner(tox_project: ToxProjectCreator) -> None:
    """Ensure passing `--parallel-no-spinner` implies `--parallel`."""
    with mock.patch.object(parallel, "execute") as mocked:
        tox_project({"tox.ini": ""}).run("p", "--parallel-no-spinner")

    mocked.assert_called_once_with(
        mock.ANY,
        max_workers=auto_detect_cpus(),
        has_spinner=False,
        live=False,
    )


def test_parallel_no_spinner_with_parallel(tox_project: ToxProjectCreator) -> None:
    """Ensure `--parallel N` is still respected with `--parallel-no-spinner`."""
    with mock.patch.object(parallel, "execute") as mocked:
        tox_project({"tox.ini": ""}).run("p", "--parallel-no-spinner", "--parallel", "2")

    mocked.assert_called_once_with(
        mock.ANY,
        max_workers=2,
        has_spinner=False,
        live=False,
    )


def test_parallel_no_spinner_ci(
    tox_project: ToxProjectCreator, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure spinner is disabled by default in CI."""
    mocked = mocker.patch.object(parallel, "execute")
    monkeypatch.setenv("CI", "1")

    tox_project({"tox.ini": ""}).run("p")

    mocked.assert_called_once_with(
        mock.ANY,
        max_workers=auto_detect_cpus(),
        has_spinner=False,
        live=False,
    )


def test_parallel_no_spinner_legacy_sequential(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    """--parallel-no-spinner alone should not force parallel mode in legacy command."""
    mocked = mocker.patch("tox.session.cmd.legacy.run_sequential")

    tox_project({"tox.ini": ""}).run("--parallel-no-spinner")

    mocked.assert_called_once()


def test_parallel_no_spinner_legacy_with_parallel(tox_project: ToxProjectCreator) -> None:
    """--parallel-no-spinner combined with -p should still run parallel without spinner."""
    with mock.patch.object(parallel, "execute") as mocked:
        tox_project({"tox.ini": ""}).run("--parallel-no-spinner", "-p", "all")

    mocked.assert_called_once_with(
        mock.ANY,
        max_workers=None,
        has_spinner=False,
        live=False,
    )


def test_parallel_list_dependencies(tox_project: ToxProjectCreator) -> None:
    proj = tox_project({
        "tox.toml": """
        [env_run_base]
        skip_install = true
        commands = [["python", "-c", "print('ok')"]]
        """
    })
    result = proj.run("p", "-e", "py", "--list-dependencies")
    result.assert_success()
    assert "pip==" in result.out


def test_no_capture_with_parallel_fails(tox_project: ToxProjectCreator) -> None:
    ini = "[testenv]\npackage=skip\ncommands=python --version"
    result = tox_project({"tox.ini": ini}).run("p", "-e", "py", "--no-capture")
    result.assert_failed()


def test_no_capture_short_flag_with_parallel_fails(tox_project: ToxProjectCreator) -> None:
    ini = "[testenv]\npackage=skip\ncommands=python --version"
    result = tox_project({"tox.ini": ini}).run("p", "-e", "py", "-i")
    result.assert_failed()


def test_parallel_fail_fast_lets_running_finish(tox_project: ToxProjectCreator) -> None:
    """Documented contract: on fail fast, running environments finish; only pending ones are skipped."""
    toml = dedent("""\
        env_list = ["a", "b"]
        [env_run_base]
        package = "skip"
        [env.a]
        commands = [["python", "-c", "raise SystemExit(7)"]]
        [env.b]
        commands = [["python", "-c", "import time, pathlib; time.sleep(2); pathlib.Path('done.txt').write_text('x')"]]
    """)
    project = tox_project({"tox.toml": toml})

    outcome = project.run("p", "-p", "2", "--fail-fast")

    outcome.assert_failed(code=7)
    assert (project.path / "done.txt").exists()
    assert "b: OK" in outcome.out


def test_parallel_spinner_stays_out_of_non_tty_output(tox_project: ToxProjectCreator) -> None:
    """Redirected output must hold plain text, not spinner control sequences."""
    toml = dedent("""\
        env_list = ["a"]
        [env_run_base]
        package = "skip"
        commands = [["python", "-c", "print('hi')"]]
    """)
    project = tox_project({"tox.toml": toml})

    outcome = project.run("p")

    outcome.assert_success()
    assert "\x1b[" not in outcome.out


def test_interrupt_logs_pkg_env_after_its_output_was_collected(
    tox_project: ToxProjectCreator, demo_pkg_inline: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    project = tox_project({
        "tox.ini": "[testenv]\npackage=wheel",
        "pyproject.toml": (demo_pkg_inline / "pyproject.toml").read_text(),
        "build.py": (demo_pkg_inline / "build.py").read_text(),
    })
    result = project.run("c", "-e", "py")
    result.assert_success()
    run_env, pkg_env = result.state.envs["py"], result.state.envs[".pkg"]
    for env in (run_env, pkg_env):
        with env.display_context(suspend=True):
            pass
    pkg_env.close_and_read_out_err()  # the run thread finished and collected the package output already

    run_env.interrupt()

    out, err = capfd.readouterr()
    assert "interrupt tox environment: .pkg" in out, out
    assert "Logging error" not in err, err


_DEPENDENCY_FAILURE_TOML = dedent("""\
    env_list = ["a", "b", "c"]
    [env_run_base]
    package = "skip"
    [env.a]
    commands = [["python", "-c", "raise SystemExit(7)"]]
    [env.b]
    depends = ["a"]
    commands = [["python", "-c", "print('successor ran')"]]
    [env.c]
    commands = [["python", "-c", "print('independent ran')"]]
""")


def test_parallel_dependency_failure_cancels_successors(tox_project: ToxProjectCreator) -> None:
    """A failed environment cancels its successors; independent environments keep running."""
    project = tox_project({"tox.toml": _DEPENDENCY_FAILURE_TOML})

    outcome = project.run("p", "-p", "all")

    outcome.assert_failed()
    assert "successor ran" not in outcome.out
    assert "a: FAIL code 7" in outcome.out
    assert "  b: SKIP " in outcome.out
    assert "  c: OK " in outcome.out
    assert "b: cancelled (dependency a failed)" in outcome.out, outcome.out


def test_parallel_dependency_failure_propagates_transitively(tox_project: ToxProjectCreator) -> None:
    """Cancellation cascades along the dependency chain and the session ends instead of hanging."""
    toml = dedent("""\
        env_list = ["a", "b", "c"]
        [env_run_base]
        package = "skip"
        [env.a]
        commands = [["python", "-c", "raise SystemExit(1)"]]
        [env.b]
        depends = ["a"]
        commands = [["python", "-c", "print('b ran')"]]
        [env.c]
        depends = ["b"]
        commands = [["python", "-c", "print('c ran')"]]
    """)
    project = tox_project({"tox.toml": toml})

    outcome = project.run("p", "-p", "all")

    outcome.assert_failed()
    assert "b ran" not in outcome.out
    assert "c ran" not in outcome.out
    assert "b: cancelled (dependency a failed)" in outcome.out, outcome.out
    assert "c: cancelled (dependency b cancelled)" in outcome.out, outcome.out


def test_parallel_cancelled_environment_in_result_json(tox_project: ToxProjectCreator) -> None:
    """The JSON report records successors cancelled by a dependency failure as skipped failures."""
    project = tox_project({"tox.toml": _DEPENDENCY_FAILURE_TOML})

    outcome = project.run("p", "-p", "all", "--result-json", "report.json")

    outcome.assert_failed()
    report = json.loads((project.path / "report.json").read_text(encoding="utf-8"))["testenvs"]["b"]["result"]
    assert report == {"success": False, "exit_code": -2, "duration": report["duration"], "skipped": True}


def test_parallel_no_resume_flag(tox_project: ToxProjectCreator, mocker: MockerFixture) -> None:
    opened = mocker.spy(common.SessionStatus, "open")
    project = tox_project({
        "tox.toml": 'env_list = ["a"]\n[env_run_base]\npackage = "skip"\ncommands = [["python", "-c", "pass"]]\n'
    })

    project.run("p", "-p", "1", "--no-resume")

    assert opened.call_args.kwargs["resume"] is False


_COUNT_CMD = (
    "from pathlib import Path; p = Path('a.count'); "
    "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')"
)
_WAIT_CMD = (
    "from pathlib import Path; import time; Path('b.start').write_text(''); "
    "(time.sleep(100) if not Path('b.release').exists() else Path('b.done').write_text('done'))"
)
_INTERRUPTED_SESSION_TOML = f"""\
env_list = ["a", "b"]
[env_run_base]
package = "skip"
[env.a]
commands = [["python", "-c", "{_COUNT_CMD}"]]
[env.b]
depends = ["a"]
commands = [["python", "-c", "{_WAIT_CMD}"]]
"""


@pytest.mark.skipif(sys.platform == "win32", reason="process groups and SIGTERM behave differently on Windows")
@pytest.mark.flaky(max_runs=3, min_passes=1)
@pytest.mark.timeout(180)
def test_resume_after_termination_reuses_confirmed_environments(tox_project: ToxProjectCreator) -> None:
    """After the process dies mid-session, a re-run reuses successful environments and finishes the unfinished ones."""
    project = tox_project({"tox.toml": _INTERRUPTED_SESSION_TOML})
    config = str(project.path / "tox.toml")
    base_cmd = [sys.executable, "-m", "tox", "-c", config, "p", "-p", "2"]

    first = Popen(base_cmd, stdout=PIPE, stderr=PIPE, cwd=project.path, start_new_session=True)
    try:
        while not ((project.path / "a.count").exists() and (project.path / "b.start").exists()):
            if first.poll() is not None:  # pragma: no cover # surfaces a premature exit instead of hanging
                out, err = first.communicate()
                msg = f"first run ended early\n{out}\n{err}"
                raise AssertionError(msg)
            sleep(0.05)
        os.killpg(os.getpgid(first.pid), SIGTERM)
        out, err = first.communicate(timeout=60)
    finally:
        if first.poll() is None:  # pragma: no cover
            os.killpg(os.getpgid(first.pid), SIGKILL)
    assert first.returncode != 0

    state_file = project.path / ".tox" / "session.json"
    assert state_file.exists()
    states = json.loads(state_file.read_text())["envs"]
    assert states["a"]["status"] == "success"
    assert states["b"]["status"] == "running"

    (project.path / "b.release").write_text("")
    second = Popen(base_cmd, stdout=PIPE, stderr=PIPE, cwd=project.path, universal_newlines=True)
    out, _ = second.communicate(timeout=120)

    assert second.returncode == 0, out
    assert (project.path / "b.done").exists()
    assert (project.path / "a.count").read_text() == "1"  # environment a was reused, never executed again
    assert not state_file.exists()  # a naturally finished session has nothing left to resume
    assert "  b: OK " in out


@pytest.mark.skipif(sys.platform == "win32", reason="process groups and SIGTERM behave differently on Windows")
@pytest.mark.flaky(max_runs=3, min_passes=1)
@pytest.mark.timeout(180)
def test_no_resume_reruns_confirmed_environments(tox_project: ToxProjectCreator) -> None:
    """--no-resume discards the interrupted session and executes even environments that already succeeded."""
    project = tox_project({"tox.toml": _INTERRUPTED_SESSION_TOML})
    config = str(project.path / "tox.toml")
    base_cmd = [sys.executable, "-m", "tox", "-c", config, "p", "-p", "2"]

    first = Popen(base_cmd, stdout=PIPE, stderr=PIPE, cwd=project.path, start_new_session=True)
    try:
        while not ((project.path / "a.count").exists() and (project.path / "b.start").exists()):
            if first.poll() is not None:  # pragma: no cover
                out, err = first.communicate()
                msg = f"first run ended early\n{out}\n{err}"
                raise AssertionError(msg)
            sleep(0.05)
        os.killpg(os.getpgid(first.pid), SIGTERM)
        first.communicate(timeout=60)
    finally:
        if first.poll() is None:  # pragma: no cover
            os.killpg(os.getpgid(first.pid), SIGKILL)

    (project.path / "b.release").write_text("")
    second = Popen(
        [*base_cmd, "--no-resume"],
        stdout=PIPE,
        stderr=PIPE,
        cwd=project.path,
        universal_newlines=True,
    )
    out, _ = second.communicate(timeout=120)

    assert second.returncode == 0, out
    assert (project.path / "a.count").read_text() == "2"  # a was executed again
    assert "  a: OK " in out
