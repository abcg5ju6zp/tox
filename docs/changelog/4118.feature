Add opt-in, content-addressed execution plans for CI review and deterministic replay. ``tox run --save-plan plan.json``
parses configuration, environment/factor selection, the dependency graph, commands and their sources, interpreter and
packaging constraints, referenced files, plugins and external packages, then writes an atomically published JSON plan
and exits without creating environments or running commands. ``tox run --plan plan.json`` validates the plan identity
and every mutable input (sources, plugins, interpreter, install package and secret environment bindings) before
provisioning or creating an environment, then executes the frozen plan; provisioned subprocesses, parallel workers,
packaging environments and sequential runs all share the same plan identity, which is also written to command logs and
``--result-json``. Secret values are never stored, only their source, length and digest. Behavior without the new flags
is unchanged.
