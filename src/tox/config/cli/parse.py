"""This module pulls together this package: create and parse CLI arguments for tox."""

from __future__ import annotations

import locale
import os
from contextlib import redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tox.config.source import Source, discover_source
from tox.report import HandledError, ToxHandler, setup_report

from .parser import Parsed, ToxParser

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from tox.session.state import State
    from tox.util.json_types import JsonValue


class Options(NamedTuple):
    parsed: Parsed
    pos_args: Sequence[str] | None
    source: Source
    cmd_handlers: dict[str, Callable[[State], int]]
    log_handler: ToxHandler


def get_options(*args: str) -> Options:
    pos_args: tuple[str, ...] | None = None
    try:  # remove positional arguments passed to parser if specified, they are pulled directly from sys.argv
        pos_arg_at = args.index("--")
    except ValueError:
        pass
    else:
        pos_args = tuple(args[pos_arg_at + 1 :])
        args = args[:pos_arg_at]

    guess_verbosity, log_handler, source = _get_base(args)
    parsed, cmd_handlers = _get_all(args, pos_args)
    if guess_verbosity != parsed.verbosity:
        log_handler.update_verbosity(parsed.verbosity)
    return Options(parsed, pos_args, source, cmd_handlers, log_handler)


def _get_base(args: Sequence[str]) -> tuple[int, ToxHandler, Source]:
    """First just load the base options (verbosity+color) to setup the logging framework."""
    tox_parser = ToxParser.base()
    parsed = Parsed()
    try:
        with (
            Path(os.devnull).open("w", encoding=locale.getpreferredencoding(do_setlocale=False)) as file_handler,
            redirect_stderr(file_handler),
        ):
            tox_parser.parse_known_args(args, namespace=parsed)
    except SystemExit:
        ...  # ignore parse errors, such as -va raises ignored explicit argument 'a'
    guess_verbosity = parsed.verbosity
    handler = setup_report(guess_verbosity, parsed.is_colored)
    # load the plugin system right after we set up report
    from tox.plugin.manager import MANAGER  # ruff:ignore[import-outside-top-level]

    try:
        source = discover_source(parsed.config_file, parsed.root_dir)
    except HandledError:
        if {"-h", "--help"}.intersection(args):
            source = _empty_source()
        else:
            raise

    MANAGER.load_plugins(source.path)

    return guess_verbosity, handler, source


def _empty_source() -> Source:
    from tox.config.source.tox_ini import ToxIni  # ruff:ignore[import-outside-top-level]

    return ToxIni(Path.cwd() / "tox.ini", content="")


def _get_all(
    args: Sequence[str], pos_args: Sequence[str] | None = None
) -> tuple[Parsed, dict[str, Callable[[State], int]]]:
    """Parse all the options."""
    tox_parser = _get_parser()
    try:
        import argcomplete  # ruff:ignore[import-outside-top-level]

        argcomplete.autocomplete(tox_parser)
    except ImportError:
        pass
    parsed, unknown = tox_parser.parse_known_args(args)
    parsed.remainder = unknown
    if getattr(parsed, "no_capture", False) and getattr(parsed, "result_json", None):
        tox_parser.error("argument -i/--no-capture: not allowed with argument --result-json")
    if parsed.plan is not None and getattr(parsed, "save_plan", None) is not None:
        tox_parser.error("argument --plan: not allowed with argument --save-plan")
    if parsed.plan is not None:
        _apply_plan_replay(parsed, args, pos_args)
    handlers = {k: p for k, (_, p) in tox_parser.handlers.items()}
    return parsed, handlers


def _apply_plan_replay(parsed: Parsed, args: Sequence[str], pos_args: Sequence[str] | None) -> None:
    from tox.plan.replay import load_and_verify_replay, replay_selection  # ruff:ignore[import-outside-top-level]
    from tox.session.env_select import CliEnv  # ruff:ignore[import-outside-top-level]

    try:
        plan, plan_id = load_and_verify_replay(parsed, list(args), list(pos_args or ()))
    except (TypeError, ValueError) as exc:
        raise HandledError(str(exc)) from exc
    parsed.plan_content = plan
    parsed.plan_id = plan_id
    parsed.env = CliEnv(replay_selection(plan))
    parsed.labels = []
    parsed.factors = []
    parsed.skip_env = ""
    _apply_plan_options(parsed, plan)


def _apply_plan_options(parsed: Parsed, plan: dict[str, JsonValue]) -> None:
    options = plan.get("options")
    if isinstance(options, dict):
        for key, value in options.items():
            if key == "discover" and isinstance(value, list):
                setattr(parsed, key, [Path(str(item)) for item in value])
            elif key == "force_dep" and isinstance(value, list):
                from packaging.requirements import Requirement  # ruff:ignore[import-outside-top-level]

                setattr(parsed, key, [Requirement(str(item)) for item in value])
            else:
                setattr(parsed, key, value)
    installpkg = plan.get("installpkg")
    if isinstance(installpkg, dict):
        parsed.install_pkg = Path(str(installpkg["path"]))


def _get_parser() -> ToxParser:
    tox_parser = ToxParser.core()  # load the core options
    # plus options setup by plugins
    from tox.plugin.manager import MANAGER  # ruff:ignore[import-outside-top-level]

    MANAGER.tox_add_option(tox_parser)
    tox_parser.fix_defaults()
    return tox_parser


def _get_parser_doc() -> ToxParser:
    # trigger register of tox env types (during normal run we call this later to handle plugins)
    from tox.plugin.manager import MANAGER  # pragma: no cover  # ruff:ignore[import-outside-top-level]

    MANAGER.load_plugins(Path.cwd())

    parser = _get_parser()  # pragma: no cover
    # Remove epilog message from help when formatting website docs
    parser.epilog = None
    return parser


__all__ = (
    "Options",
    "get_options",
)
