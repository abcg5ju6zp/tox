from __future__ import annotations

from tox.plan.secrets import bind_plan_secrets, redact_plan, secret_binding


def test_secret_binding_round_trip() -> None:
    binding = secret_binding("environment", "API_TOKEN", "secret")
    plan = {"command": ["curl", "secret"], "metadata": {"nested": "token secret"}}
    expected_placeholder = "__TOX_PLAN_SECRET_environment:API_TOKEN:"

    redact_plan(plan, {binding.identifier: binding})

    assert plan["command"][1].startswith(expected_placeholder)
    assert plan["metadata"]["nested"] == "token " + plan["command"][1]
    assert "secret" not in str(plan)
    restored = bind_plan_secrets(plan, {binding.identifier: "secret"})
    assert restored == {"command": ["curl", "secret"], "metadata": {"nested": "token secret"}}
