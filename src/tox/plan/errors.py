from __future__ import annotations


class PlanError(ValueError):
    """An execution plan is invalid or incompatible with the current runtime."""
