"""Common functionality shared across multiple type of runs."""

from __future__ import annotations

import logging
import os
import time
from argparse import Action, ArgumentError, ArgumentParser, Namespace
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from fnmatch import fnmatchcase
from pathlib import Path
from signal import SIGINT, Handlers, signal
from threading import Event, Thread
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from colorama import Fore

from tox.config.types import EnvList
from tox.execute import Outcome
from tox.journal import write_journal
from tox.report import HandledError
from tox.session.cmd.run.single import ToxEnvRunResult, run_one
from tox.session.status import SESSION_STATE_FILE, EnvRecord, EnvStatus, SessionStatus
from tox.tox_env.errors import Fail
from tox.util.graph import stable_topological_sort
from tox.util.spinner import MISS_DURATION, Spinner
from tox.util.typing_compat import override
from tox.util.venv_redirect import record_venv_redirect, venv_redirect_path

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import FrameType

    from tox.session.state import State
    from tox.tox_env.api import ToxEnv
    from tox.tox_env.runner import RunToxEnv


class SkipMissingInterpreterAction(Action):
    @override
    def __call__(
        self,
        parser: ArgumentParser,
        namespace: Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        value = "true" if values is None else values
        if value not in {"config", "true", "false"}:
            raise ArgumentError(self, f"value must be 'config', 'true', or 'false' (got {value!r})")
        setattr(namespace, self.dest, value)


class InstallPackageAction(Action):
    @override
    def __call__(
        self,
        parser: ArgumentParser,
        namespace: Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if not values:
            raise ArgumentError(self, "cannot be empty")
        raw = cast("str", values)
        path = self._resolve_path(raw)
        if not path.exists():
            raise ArgumentError(self, f"{path} does not exist")
        if not path.is_file():
            raise ArgumentError(self, f"{path} is not a file")
        setattr(namespace, self.dest, path)

    @staticmethod
    def _resolve_path(raw: str) -> Path:
        """Convert a raw string (possibly a ``file:`` URI) to an absolute :class:`~pathlib.Path`."""
        if raw.startswith("file:"):
            parsed = urlparse(raw)
            path = Path(url2pathname(unquote(parsed.path)))
        else:
            path = Path(raw)
        return path.absolute()


def env_run_create_flags(
    parser: ArgumentParser, mode: Literal["run", "run-parallel", "legacy", "devenv", "config", "depends", "exec"]
) -> None:
    if mode not in {"devenv", "depends"}:
        parser.add_argument(
            "-s",
            "--skip-missing-interpreters",
            default="config",
            metavar="v",
            nargs="?",
            action=SkipMissingInterpreterAction,
            help="don't fail tests for missing interpreters: {config,true,false} choice",
        )
    if mode not in {"devenv", "config", "depends"}:
        parser.add_argument(
            "-n",
            "--notest",
            dest="no_test",
            help="do not run the test commands",
            action="store_true",
        )
        parser.add_argument(
            "-b",
            "--pkg-only",
            "--sdistonly",
            action="store_true",
            help="only perform the packaging activity",
            dest="package_only",
        )
        parser.add_argument(
            "--installpkg",
            help="use specified package for installation into venv, instead of packaging the project",
            default=None,
            of_type=Path | None,
            action=InstallPackageAction,
            dest="install_pkg",
        )
        parser.add_argument(
            "--fail-fast",
            action="store_true",
            default=False,
            dest="fail_fast",
            help="stop execution after the first environment failure",
        )
        parser.add_argument(
            "--no-resume",
            action="store_false",
            default=True,
            dest="resume",
            help="do not reuse environments already confirmed successful in an earlier interrupted session; run all"
            " selected environments again",
        )
    if mode not in {"devenv", "depends"}:
        parser.add_argument(
            "--develop",
            action="store_true",
            help="install package in development mode",
            dest="develop",
        )
    if mode != "depends":
        parser.add_argument(
            "--no-recreate-pkg",
            dest="no_recreate_pkg",
            help="if recreate is set do not recreate packaging tox environment(s)",
            action="store_true",
        )
    if mode not in {"devenv", "config", "depends"}:
        parser.add_argument(
            "--skip-pkg-install",
            dest="skip_pkg_install",
            help="skip package installation for this run",
            action="store_true",
        )
        parser.add_argument(
            "--skip-env-install",
            dest="skip_env_install",
            help="skip dependency and package installation, reuse existing environment",
            action="store_true",
        )


def report(
    start: float, runs: list[ToxEnvRunResult], *, is_colored: bool, verbosity: int, fail_fast: bool = False
) -> int:
    def _print(color_: str, message: str) -> None:
        if verbosity:
            print(f"{color_ if is_colored else ''}{message}{Fore.RESET if is_colored else ''}")  # ruff:ignore[print]

    successful, skipped = [], []
    for run in runs:
        successful.append(run.code == Outcome.OK or run.ignore_outcome or run.unavailable)
        skipped.append(run.skipped)
        duration_individual = [o.elapsed for o in run.outcomes] if verbosity >= 2 else []  # ruff:ignore[magic-value-comparison]
        extra = f"+cmd[{','.join(f'{i:.2f}' for i in duration_individual)}]" if duration_individual else ""
        setup = run.duration - sum(duration_individual)
        msg, color = _get_outcome_message(run)
        out = f"  {run.name}: {msg} ({run.duration:.2f}{f'=setup[{setup:.2f}]{extra}' if extra else ''} seconds)"
        _print(color, out)

    duration = time.monotonic() - start
    all_good = all(successful) and not all(skipped)
    if all_good:
        _print(Fore.GREEN, f"  congratulations :) ({duration:.2f} seconds)")
        return Outcome.OK
    _print(Fore.RED, f"  evaluation failed :( ({duration:.2f} seconds)")
    if len(runs) == 1:
        return runs[0].code if not runs[0].skipped else 1
    if fail_fast:
        # under fail fast the run stops at the first failure, whose code is the documented overall exit code
        first_failed = next(
            (r for r in runs if not r.skipped and not r.ignore_outcome and not r.unavailable and r.code != Outcome.OK),
            None,
        )
        if first_failed is not None:
            return first_failed.code
    return 1


def _get_outcome_message(run: ToxEnvRunResult) -> tuple[str, str]:
    if run.unavailable:
        msg, color = "NOT AVAILABLE", Fore.YELLOW
    elif run.skipped:
        msg, color = "SKIP", Fore.YELLOW
    elif run.code == Outcome.OK:
        msg, color = "OK", Fore.GREEN
    elif run.ignore_outcome:
        msg, color = f"IGNORED FAIL code {run.code}", Fore.YELLOW
    else:
        msg, color = f"FAIL code {run.code}", Fore.RED
    return msg, color


logger = logging.getLogger(__name__)


def _warn_unused_config(state: State) -> None:
    from tox.config.cli.parser import DEFAULT_VERBOSITY  # ruff:ignore[import-outside-top-level]

    if state.conf.options.verbosity <= DEFAULT_VERBOSITY:
        return
    is_colored = state.conf.options.is_colored
    for name in state.envs.iter():
        if unused := state.envs[name].conf.unused():
            _print_unused(is_colored, f"[testenv:{name}]", unused)
    if unused := state.conf.core.unused():
        _print_unused(is_colored, "[tox]", unused)


def _print_unused(is_colored: bool, section: str, unused: list[str]) -> None:  # ruff:ignore[boolean-type-hint-positional-argument]
    msg = f"  {section} unused config key(s): {', '.join(unused)}"
    print(f"{Fore.YELLOW if is_colored else ''}{msg}{Fore.RESET if is_colored else ''}")  # ruff:ignore[print]


def execute(  # ruff:ignore[complex-structure]
    state: State,
    max_workers: int | None,
    has_spinner: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
    live: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
) -> int:
    interrupt, done = Event(), Event()
    results: list[ToxEnvRunResult] = []
    future_to_env: dict[Future[ToxEnvRunResult], ToxEnv] = {}
    state.envs.ensure_only_run_env_is_active()
    to_run_list: list[str] = list(state.envs.iter())
    for name in to_run_list:
        cast("RunToxEnv", state.envs[name]).mark_active()
    _venv_redirect_enabled(state, _env_dirs(state))  # reject a conflicting setting before any environment runs

    # environments a previous, interrupted session already confirmed are not run again unless asked to
    resume = bool(getattr(state.conf.options, "resume", True))
    status_path = state.conf.core.get("work_dir", Path) / SESSION_STATE_FILE
    session_status = SessionStatus.open(status_path, resume=resume)
    reused = session_status.reusable(to_run_list)
    for name, record in reused.items():
        logger.info("reusing result of environment %s from the previous session", name)
        results.append(_reused_result(name, record))
    pending = [name for name in to_run_list if name not in reused]
    session_status.begin(pending, reused=reused)

    scheduler_error: list[BaseException] = []

    def _run_thread() -> tuple[Callable[[int, FrameType | None], Any] | int | Handlers | None, bool]:
        spinner = ToxSpinner(has_spinner, state, len(pending))
        thread = Thread(
            target=_queue_and_wait,
            name="tox-interrupt",
            args=(
                state,
                to_run_list,
                pending,
                results,
                future_to_env,
                interrupt,
                done,
                max_workers,
                spinner,
                live,
                scheduler_error,
                session_status,
            ),
        )
        thread.start()
        try:
            while thread.is_alive():
                thread.join(timeout=1)
        except KeyboardInterrupt:
            previous = signal(SIGINT, Handlers.SIG_IGN)
            spinner.print_report = False  # no need to print reports at this point, final report coming up
            logger.error("[%s] KeyboardInterrupt - teardown started", os.getpid())  # ruff:ignore[error-instead-of-exception]
            interrupt.set()
            # cancel in reverse order to not allow submitting new jobs as we cancel running ones
            for future, tox_env in reversed(list(future_to_env.items())):
                canceled = future.cancel()
                # if cannot be canceled and not done -> still runs
                if canceled is False and not future.done():  # pragma: no branch
                    tox_env.interrupt()
            done.wait()
            thread.join()
            return previous, True
        return None, False

    previous, has_previous = None, False
    try:
        previous, has_previous = _run_thread()
        if scheduler_error:
            raise scheduler_error[0]
    finally:
        ordered_results = _order_results(state, results, to_run_list)
        # a finished session has nothing to resume; an interrupted one stays on disk unless the user opted out
        if not has_previous or not resume:
            session_status.discard()
        # write the journal
        write_journal(state.conf.options.result_json, state._journal)  # ruff:ignore[private-member-access]
        # let editors discover an environment
        _record_venv_redirect(state)
        # warn about unused config keys
        _warn_unused_config(state)
        # report the outcome
        exit_code = report(
            state.conf.options.start,
            ordered_results,
            is_colored=state.conf.options.is_colored,
            verbosity=state.conf.options.verbosity,
            fail_fast=state.conf.options.fail_fast
            or any(cast("RunToxEnv", state.envs[env]).conf["fail_fast"] for env in to_run_list),
        )
        if has_previous:
            signal(SIGINT, previous)
    return exit_code


def _reused_result(name: str, record: EnvRecord) -> ToxEnvRunResult:
    return ToxEnvRunResult(
        name=name,
        skipped=record.skipped,
        code=record.exit_code if record.exit_code is not None else Outcome.OK,
        outcomes=[],
        duration=record.duration if record.duration is not None else MISS_DURATION,
        ignore_outcome=record.ignore_outcome,
    )


def _env_dirs(state: State) -> dict[str, Path]:
    return {name: state.envs[name].env_dir for name in state.envs.iter(only_active=False)}


def _venv_redirect_enabled(state: State, env_dirs: dict[str, Path]) -> bool:
    redirect = venv_redirect_path(state.conf.core.get("tox_root", Path))
    at_redirect = [name for name, env_dir in env_dirs.items() if env_dir == redirect]
    if (setting := state.conf.core.get_optional("venv_redirect", bool)) is None:
        return not at_redirect  # unset: an environment living at .venv is the project's .venv
    if setting and at_redirect:
        msg = (
            f"venv_redirect is true, but tox environment {at_redirect[0]} lives at {redirect}, where the redirect file"
            " goes; set venv_redirect to false or leave it unset"
        )
        raise HandledError(msg)
    return setting


def _order_results(state: State, results: list[ToxEnvRunResult], to_run_list: list[str]) -> list[ToxEnvRunResult]:
    name_to_run = {r.name: r for r in results}
    ordered: list[ToxEnvRunResult] = [
        name_to_run.get(env, ToxEnvRunResult(name=env, skipped=True, code=-2, outcomes=[], duration=MISS_DURATION))
        for env in to_run_list
    ]
    # add results for unavailable environments
    ordered.extend(
        ToxEnvRunResult(name=env_name, skipped=False, code=0, outcomes=[], duration=MISS_DURATION, unavailable=True)
        for env_name in state.envs.unavailable_envs()
        if env_name not in name_to_run
    )
    return ordered


def _record_venv_redirect(state: State) -> None:
    env_dirs = _env_dirs(state)
    if not _venv_redirect_enabled(state, env_dirs) or (target := _venv_redirect_target(state, env_dirs)) is None:
        return
    core = state.conf.core
    work_dir, ours = core.get("work_dir", Path), set(env_dirs.values())
    record_venv_redirect(core.get("tox_root", Path), target, lambda path: path in ours or path.is_relative_to(work_dir))


def _venv_redirect_target(state: State, env_dirs: dict[str, Path]) -> Path | None:
    # the pick depends on the configuration alone, so it stays the same whichever environments ran
    if (pinned := state.conf.core.get_optional("venv_redirect_env", str)) is not None and pinned not in env_dirs:
        logger.warning("venv_redirect_env names %s, which is not a tox environment", pinned)
        return None
    develop = next((name for name in env_dirs if _installs_develop(state.envs[name])), None)
    name = pinned or ("dev" if "dev" in env_dirs else develop)
    return env_dirs[name] if name is not None and (env_dirs[name] / "pyvenv.cfg").exists() else None


def _installs_develop(env: ToxEnv) -> bool:
    return "package" in env.conf and env.conf["package"] in {"editable", "editable-legacy"}


class ToxSpinner(Spinner):
    def __init__(self, enabled: bool, state: State, total: int) -> None:  # ruff:ignore[boolean-type-hint-positional-argument]
        stream = state._options.log_handler.stdout  # ruff:ignore[private-member-access]
        super().__init__(
            # animation frames and erase sequences belong to an interactive terminal, never to redirected output
            enabled=enabled and stream.isatty(),
            colored=state.conf.options.is_colored,
            stream=stream,
            total=total,
        )

    def update_spinner(self, result: ToxEnvRunResult, success: bool) -> None:  # ruff:ignore[boolean-type-hint-positional-argument]
        done = (self.skip if result.skipped else self.succeed) if success else self.fail
        done(result.name)


def _next_completed(
    future_to_env: dict[Future[ToxEnvRunResult], ToxEnv],
    interrupt: Event,
) -> Future[ToxEnvRunResult] | None:
    while True:
        done_futures, _ = wait_futures(list(future_to_env), timeout=1, return_when=FIRST_COMPLETED)
        if done_futures:
            return done_futures.pop()
        if interrupt.is_set():
            return None


def _queue_and_wait(  # ruff:ignore[too-many-arguments]
    state: State,
    to_run_list: list[str],
    pending: list[str],
    results: list[ToxEnvRunResult],
    future_to_env: dict[Future[ToxEnvRunResult], ToxEnv],
    interrupt: Event,
    done: Event,
    max_workers: int | None,
    spinner: ToxSpinner,
    live: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
    error: list[BaseException],
    session_status: SessionStatus,
) -> None:
    try:
        try:
            _do_queue_and_wait(
                state,
                pending,
                results,
                future_to_env,
                interrupt,
                max_workers,
                spinner,
                live,
                session_status,
            )
        except BaseException as exception:  # ruff:ignore[blind-except] # re-raised in the main thread
            error.append(exception)
    finally:
        try:
            for name in to_run_list:
                _tear_down(state.envs[name])
        finally:
            done.set()


def _tear_down(tox_env: ToxEnv) -> None:
    try:
        tox_env.teardown()
    except (Fail, OSError):  # one environment failing to clean up must not leak the others' resources
        logger.warning("failed to tear down environment %s", tox_env.conf.name, exc_info=True)


def _do_queue_and_wait(  # ruff:ignore[complex-structure, too-many-arguments, too-many-branches]
    state: State,
    pending: list[str],
    results: list[ToxEnvRunResult],
    future_to_env: dict[Future[ToxEnvRunResult], ToxEnv],
    interrupt: Event,
    max_workers: int | None,
    spinner: ToxSpinner,
    live: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
    session_status: SessionStatus,
) -> None:
    options = state._options  # ruff:ignore[private-member-access]
    with spinner:  # ruff:ignore[too-many-nested-blocks]
        # an unbounded pool (-p all) sizes to the selection; keep at least one worker so an empty
        # selection does not raise ValueError from ThreadPoolExecutor
        max_workers = max(1, len(pending)) if max_workers is None else max_workers
        order, todo = run_order(state, pending)
        remaining: set[str] = set(pending)
        stop_scheduling = False
        fail_fast_enabled = options.parsed.fail_fast or any(
            cast("RunToxEnv", state.envs[env]).conf["fail_fast"] for env in pending
        )
        signalled: set[Future[ToxEnvRunResult]] = set()

        def _run(tox_env: RunToxEnv) -> ToxEnvRunResult:
            spinner.add(tox_env.conf.name)
            return run_one(
                tox_env,
                options.parsed.no_test or options.parsed.package_only,
                suspend_display=live is False,
            )

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tox-driver") as executor:
            while True:
                # a user interrupt cancels every environment that has not started running yet
                if interrupt.is_set():
                    _cancel_queued_envs(
                        session_status,
                        order,
                        remaining,
                        results,
                        state,
                        spinner,
                        live,
                        exit_code=-2,
                        skipped=False,
                        reason="interrupted",
                        tear_down=True,
                    )
                # a failed or cancelled dependency deterministically cancels every successor (transitively)
                for name, reason in session_status.propagate(todo):
                    _record_cancelled(
                        name, -2, skipped=True, reason=reason, results=results, state=state, spinner=spinner, live=live
                    )
                    remaining.discard(name)
                # fail fast keeps already running environments going but never starts a new one
                if stop_scheduling:
                    _cancel_queued_envs(
                        session_status,
                        order,
                        remaining,
                        results,
                        state,
                        spinner,
                        live,
                        exit_code=-2,
                        skipped=True,
                        reason="fail fast",
                        tear_down=False,
                    )

                ready = ready_to_run(session_status, order, todo, remaining)
                if interrupt.is_set() or stop_scheduling:
                    ready = []
                elif max_workers == 1 and fail_fast_enabled:
                    # sequential fail-fast runs one environment at a time so it can stop before queuing the rest
                    ready = ready[:1]
                for env in ready:
                    tox_env_to_run = cast("RunToxEnv", state.envs[env])
                    future = executor.submit(_run, tox_env_to_run)
                    session_status.mark_running(env)
                    future_to_env[future] = tox_env_to_run

                result: ToxEnvRunResult | None = None
                if future_to_env:
                    completed_future = _next_completed(future_to_env, interrupt)
                    if completed_future is None:
                        # interrupt: stop futures the executor has not started, signal the rest, then keep
                        # waiting so every in-flight environment reaches a terminal state instead of hanging
                        _signal_in_flight(future_to_env, signalled)
                    else:
                        signalled.discard(completed_future)
                        result, failed_fast = _consume_completed_future(
                            future_to_env,
                            completed_future,
                            interrupt,
                            options.parsed.fail_fast,
                            session_status,
                        )
                        if failed_fast:
                            stop_scheduling = True
                        results.append(result)
                        remaining.discard(result.name)

                final_run = not remaining and not future_to_env
                if final_run:  # disable report on final env
                    spinner.print_report = False
                if result is not None:
                    _handle_one_run_done(result, spinner, state, live)
                if final_run:
                    break


def _signal_in_flight(
    future_to_env: dict[Future[ToxEnvRunResult], ToxEnv], signalled: set[Future[ToxEnvRunResult]]
) -> None:
    """Cancel futures not yet started and interrupt the rest, each at most once."""
    for waiting_future, waiting_env in list(future_to_env.items()):
        if waiting_future in signalled:
            continue
        if not waiting_future.cancel() and not waiting_future.done():
            waiting_env.interrupt()
        signalled.add(waiting_future)


def _consume_completed_future(
    future_to_env: dict[Future[ToxEnvRunResult], ToxEnv],
    completed_future: Future[ToxEnvRunResult],
    interrupt: Event,
    cli_fail_fast: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
    session_status: SessionStatus,
) -> tuple[ToxEnvRunResult, bool]:
    """Turn a completed future into its run result, updating the session state.

    :returns: the run result and a flag indicating a fail-fast triggering failure

    """
    tox_env_done = future_to_env.pop(completed_future)
    try:
        result = completed_future.result()
    except CancelledError:
        tox_env_done.teardown()
        was_interrupted = interrupt.is_set()
        result = ToxEnvRunResult(
            name=tox_env_done.conf.name,
            skipped=not was_interrupted,
            code=-3 if was_interrupted else -2,
            outcomes=[],
            duration=MISS_DURATION,
        )
        session_status.cancel(
            result.name,
            exit_code=result.code,
            skipped=result.skipped,
            reason="interrupted" if was_interrupted else "fail fast",
        )
        return result, False
    status = session_status.finish(
        result.name,
        exit_code=result.code,
        skipped=result.skipped,
        ignore_outcome=result.ignore_outcome,
        duration=result.duration,
        interrupted=interrupt.is_set(),
    )
    failed_fast = status is EnvStatus.FAILED and (cli_fail_fast or result.fail_fast)
    if failed_fast:
        # do not schedule new work; running environments are allowed to finish (cancel only stops futures the
        # executor has not started yet)
        for waiting_future in list(future_to_env.keys()):
            waiting_future.cancel()
    return result, failed_fast


def _cancel_queued_envs(  # ruff:ignore[too-many-arguments]
    session_status: SessionStatus,
    order: list[str],
    remaining: set[str],
    results: list[ToxEnvRunResult],
    state: State,
    spinner: ToxSpinner,
    live: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
    *,
    exit_code: int,
    skipped: bool,
    reason: str,
    tear_down: bool,
) -> None:
    for name in session_status.cancel_queued(order, exit_code=exit_code, skipped=skipped, reason=reason):
        if tear_down:  # the environment never runs, so its normal run-time teardown does not happen either
            _tear_down(cast("RunToxEnv", state.envs[name]))
        _record_cancelled(name, exit_code, skipped, reason, results, state, spinner, live)
        remaining.discard(name)


def _record_cancelled(  # ruff:ignore[too-many-arguments]
    name: str,
    code: int,
    skipped: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
    reason: str,
    results: list[ToxEnvRunResult],
    state: State,
    spinner: ToxSpinner,
    live: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
) -> None:
    logger.warning("%s: cancelled (%s)", name, reason)
    result = ToxEnvRunResult(name=name, skipped=skipped, code=code, outcomes=[], duration=MISS_DURATION)
    results.append(result)
    _handle_one_run_done(result, spinner, state, live)


def _handle_one_run_done(
    result: ToxEnvRunResult,
    spinner: ToxSpinner,
    state: State,
    live: bool,  # ruff:ignore[boolean-type-hint-positional-argument]
) -> None:
    success = result.code == Outcome.OK
    spinner.update_spinner(result, success)
    tox_env = cast("RunToxEnv", state.envs[result.name])
    if tox_env.journal:  # add overall journal entry
        tox_env.journal["result"] = {
            "success": success,
            "exit_code": result.code,
            "duration": result.duration,
            "skipped": result.skipped,
        }
    if live is False and state.conf.options.parallel_live is False:  # teardown background run
        out_err = tox_env.close_and_read_out_err()  # sync writes from buffer to stdout/stderr
        pkg_out_err_list = []
        for package_env in tox_env.package_envs:
            pkg_out_err = package_env.close_and_read_out_err()
            if pkg_out_err is not None:  # pragma: no branch
                pkg_out_err_list.append(pkg_out_err)
        if not success or tox_env.conf["parallel_show_output"] or state.conf.options.list_dependencies:
            for pkg_out_err in pkg_out_err_list:
                state._options.log_handler.write_out_err(pkg_out_err)  # pragma: no cover  # ruff:ignore[private-member-access]
            if out_err is not None:  # pragma: no branch # first show package build
                state._options.log_handler.write_out_err(out_err)  # ruff:ignore[private-member-access]


def ready_to_run(status: SessionStatus, order: list[str], todo: dict[str, set[str]], remaining: set[str]) -> list[str]:
    """Collect queued environments whose dependencies all succeeded, in stable execution order."""
    return [
        name
        for name in order
        if name in remaining
        and status.status_of(name) is EnvStatus.QUEUED
        and all(status.status_of(dep) is EnvStatus.SUCCESS for dep in todo[name])
    ]


def run_order(state: State, to_run: list[str]) -> tuple[list[str], dict[str, set[str]]]:
    to_run_set = set(to_run)
    todo: dict[str, set[str]] = {}
    for env in to_run:
        run_env = cast("RunToxEnv", state.envs[env])
        depends = set(run_env.conf.get("depends", EnvList).envs)
        todo[env] = {name for dep in depends for name in to_run_set if fnmatchcase(name, dep)} - {env}
    try:
        order = stable_topological_sort(todo)
    except ValueError as exception:
        msg = f"circular dependency detected between environments: {exception}"
        raise HandledError(msg) from exception
    return order, todo
