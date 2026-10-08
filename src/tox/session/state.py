from __future__ import annotations

import sys
from itertools import chain, tee
from typing import TYPE_CHECKING, cast

from tox.config.main import Config
from tox.journal import Journal
from tox.plugin import impl
from tox.plugin.manager import MANAGER

from .env_select import EnvSelector

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tox.config.cli.parse import Options
    from tox.config.cli.parser import ToxParser
    from tox.util.json_types import JsonValue


class State:
    """Runtime state holder."""

    def __init__(self, options: Options, args: Sequence[str]) -> None:
        (extended_envs,) = tee(chain.from_iterable(MANAGER.tox_extend_envs()), 1)
        plan_snapshot = (
            cast("dict[str, JsonValue]", options.parsed.plan_content["config"]) if options.parsed.plan_content else None
        )
        self.conf = Config.make(
            options.parsed,
            options.pos_args,
            options.source,
            extended_envs,
            plan_snapshot=plan_snapshot,
        )
        self.conf.core.add_constant(
            keys=["on_platform"],
            desc="platform we are running on",
            value=sys.platform,
        )
        self._options = options
        self.args = args
        self._journal: Journal = Journal(
            options.parsed.result_json is not None,
            plan_id=options.parsed.plan_id,
            plan_path=str(options.parsed.plan) if options.parsed.plan is not None else None,
        )
        self._selector: EnvSelector | None = None

    @property
    def envs(self) -> EnvSelector:
        """:returns: provides access to the tox environments"""
        if self._selector is None:
            self._selector = EnvSelector(self)
        return self._selector


@impl
def tox_add_option(parser: ToxParser) -> None:
    from tox.tox_env.register import REGISTER  # ruff:ignore[import-outside-top-level]

    parser.add_argument(
        "--runner",
        dest="default_runner",
        help="the tox run engine to use when not explicitly stated in tox env configuration",
        default=REGISTER.default_env_runner,
        choices=list(REGISTER.env_runners),
    )
