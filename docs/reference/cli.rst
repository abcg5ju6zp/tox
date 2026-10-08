.. _cli:

###############
 CLI interface
###############

******************
 Shell completion
******************

tox supports shell completion for commands, flags, and environment names via :pypi:`argcomplete`. Install tox with the
``completion`` extra:

.. code-block:: bash

    uv tool install 'tox[completion]'

Then configure your shell:

.. tab:: bash

    Add to ``~/.bashrc``:

    .. code-block:: bash

       eval "$(register-python-argcomplete tox)"

.. tab:: zsh

    Add to ``~/.zshrc``:

    .. code-block:: zsh

       autoload -U bashcompinit
       bashcompinit
       eval "$(register-python-argcomplete tox)"

.. tab:: fish

    Add to ``~/.config/fish/config.fish``:

    .. code-block:: fish

       register-python-argcomplete --shell fish tox | source

Once configured, pressing ``<TAB>`` completes subcommands (``tox r`` → ``tox run``), flags (``tox run --``), and
environment names (``tox run -e`` lists environments from your tox configuration).

**********
 Man page
**********

tox ships a Unix man page accessible via ``man tox`` (see :ref:`howto` for setup). The man page source is at
``docs/man/tox.1.rst`` and can be regenerated from the CLI parser with ``tox run -e manpage``.

******************
 Execution plans
******************

A CI pipeline can resolve a run once, review the exact result, and later execute exactly what was reviewed. The two
phases use a content-addressed execution plan and are available for ``tox run`` (``r``), ``tox run-parallel`` (``p``)
and the default legacy entry-point.

Saving a plan
=============

``--save-plan`` parses configuration and performs non-mutating interpreter discovery, then writes the plan and exits.
It never provisions, creates or modifies environments, installs packages or runs commands:

.. code-block:: bash

    tox run --save-plan .tox/plan.json

The plan freezes, with the source of every value and command:

- the selected run and packaging environments, ``env_list``/``-e``/label/factor/``--skip-env`` inputs and the expanded
  result, including environments marked unavailable;
- the ``depends`` edges and the stable topological run order;
- resolved core and per-environment configuration, including base inheritance, overrides, factor conditions, TOML
  expansion and values registered by plugins;
- ``commands_pre``/``commands``/``commands_post``/``extra_setup_commands`` and packaging build commands with their
  working directory and execution attributes;
- the base Python candidates, chosen interpreter identity and virtualenv/packaging constraints;
- the package mode (``wheel``, ``sdist``, ``sdist-wheel``, ``editable``, ``editable-legacy``, ``deps-only``, ``skip``,
  ``external``) and any ``--installpkg`` with its path, size and SHA-256;
- the main configuration and every referenced local file (``set_env`` env files, requirements/constraints,
  ``base_python_file``, ``pylock``) with role, path, size and SHA-256;
- the tox version, provision ``requires`` and the inline/internal/external plugins with their module digest and hooks.

Each plan carries an explicit schema version and a ``sha256`` identity over the canonical plan content. The file is
published through a same-directory temporary file, ``fsync`` and atomic rename, so an interrupted write can never leave
a replayable half-written plan, and replay always recomputes the identity instead of trusting the file name.

Saving requires every package declared in ``requires`` to already be importable; otherwise it fails with the missing
requirements (as with ``--no-provision``) instead of provisioning an incomplete plan.

Replaying a plan
================

``--plan`` validates the plan before provisioning or creating any environment:

.. code-block:: bash

    tox run --plan .tox/plan.json

Replay rebuilds configuration from the in-plan snapshot; current configuration files, environment variables, user
configuration or plugin hooks cannot override it. It rejects the run with a structured difference listing the expected
and current identity when:

- the plan document is truncated, tampered with, has an unsupported schema version or a mismatching digest;
- a recorded source or referenced file, an external ``--installpkg`` or the chosen interpreter is missing or changed;
- a plugin is missing or unexpectedly present, or its module content or implemented hooks changed (a plugin declared in
  ``requires`` and missing in the outer process is still allowed to be provisioned);
- a secret environment variable is missing or its value changed;
- the command mode differs, execution-affecting CLI arguments are added, or positional arguments after ``--`` differ;
- the recomputed dependency graph or a command about to execute differs from the plan.

Only reporting options such as ``-v``/``-q``, ``--colored`` and ``--result-json`` may differ from the save command.
When replay triggers provisioning, the provisioned tox subprocess receives the same ``--plan`` path, re-validates it and
runs the same frozen content; parallel workers, shared packaging environments and sequential runs share one plan. The
console reports the plan identity at start and finish, each command log records the plan id and schema version, and
``--result-json`` adds a top-level ``plan`` object with the id, schema version and path.

Secret handling
===============

Values of variables whose names look sensitive (for example ``TOKEN``, ``PASSWORD``, ``API_KEY``) referenced from
``set_env`` (including ``file|`` env files), ``pass_env``, ``{env:NAME}`` or ``TOX_*`` CLI defaults are replaced by
placeholders wherever they occur. The plan stores only the variable name, origin, source, presence, length and SHA-256
digest, never the plaintext value, and the human-readable summary is redacted the same way. Replay restores an
environment-sourced secret only when the current value has the same digest. Secrets defined in a hash-verified
configuration or ``set_env`` file are read from that file only after its recorded digest matches, so the same pinned
sources can be replayed without exporting the value. Any mismatch or absence is reported without revealing either
value.

**********************
 Command-line options
**********************

.. sphinx_argparse_cli::
    :module: tox.config.cli.parse
    :func: _get_parser_doc
