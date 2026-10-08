from __future__ import annotations

from typing import TYPE_CHECKING

from tox.config.cli.parser import ToxParser

if TYPE_CHECKING:
    from pathlib import Path


def test_plan_cli_options_accepted(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    save_path = tmp_path / "saved.json"
    parsed = ToxParser.base().parse_args(["--plan", str(plan_path), "--save-plan", str(save_path)])

    assert parsed.plan == plan_path
    assert parsed.save_plan == save_path
