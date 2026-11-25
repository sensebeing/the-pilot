# Module ownership

- `defaults.py`: universal built-in presets, `PIPELINE_STAGE_CONTRACTS`, and project-neutral templates.
- `project.py`: pure overlay normalization and composition for explicit library callers.
- `profile.py`: durable user preference normalization and preset resolution.
- `compiler.py`: pure rendering and managed-document merges; no IO.
- `distill.py`: read-only sampling and pattern distillation across recent local agent chat logs, plus scene pipeline assessment of the installed profile and scene contracts.
- `installer.py`: the sole owner of global installation, `self-update`, installation locking and artifact writes. It removes obsolete installed project snapshots during apply and uninstall.
- `packaging.py`: the zero-dependency PEP 517/660 backend leaf (`requires = []`). It owns the controlled package file enumeration, the source and wheel tree digests, and wheel, editable and sdist builds outside the checkout. It never reads or writes `build/`, `dist/` or `*.egg-info`; only `installer.py` imports it.
- `assets/runtime.py`: the standalone runtime, copied unchanged on install. It owns per-turn workspace shortcut discovery and validation, terminal lifecycle status and `agy` launch argument semantics, including pane resolution and the paired OSC 7501 activity and OSC 2 title write. It owns the single synchronous Stop submission, post-Stop turn-start observation from the session and transcript, and settlement of unconfirmed delivery in the original pane. The package reuses its worker vocabulary, model preference validation and launch flag formatting, universal entry catalog, shortcut schema, comma command rendering and log rotation.
- `runtime.py`: subprocess adapter to the installed runtime (no second engine).
- `cli.py`: installer versus runtime command routing only.

Dependencies flow from the CLI to installer, compiler, profile, project and
defaults. `packaging.py` is a standard-library-only leaf that only `installer.py`
consumes. The standalone asset has standard-library dependencies only. Distill
validates the installed pipeline against the universal catalog. Package bootstrap
can load code from a verified checkout using Git provenance and update metadata.

Global artifacts are compiled with universal defaults. On `PreInvocation`, the
runtime reads the nearest `.pilot/project.json` above the active workspace and
adds its entries and comma aliases to a copy of the installed shortcut catalog.
Profile template overrides apply to matching keys. A malformed overlay leaves
the base catalog available and records `project_overlay_invalid`. Legacy scene,
delivery and preset fields are tolerated by the workspace reader.

### Architecture workflow

`defaults.py` defines the universal pipeline contracts and scene templates.
Planning derives the post-change minimal domain model from the business goal,
including a single owner, authoritative entities and their relationships, one
state machine per lifecycle, target control loops and lanes, atomic writes, and
direct read projections. Before finalizing table mergers, retirements, or
lifecycle changes, inspect the actual schema and migration constraints
read-only. Check primary and unique key granularity, foreign key `ON DELETE`
behavior, and independent erasure or billing lifecycles. An extension
belongs in the authoritative entity's lifecycle or read projection. A
simplification or repair removes redundant tables, split state machines, and
cross-table side effects at their source. Moving a child table wholesale into
prefixed parent columns and a secondary status column or trimming only
peripheral tables leaves the core complexity intact.

`compiler.py` renders the built-in scenes and Orca handoff instructions for
installation. Delivery and closeout use the configured strategy and follow the
active repository's guidance, runbooks, ledgers and authorized skills. Workers may adapt
file organization and must preserve the reviewed model. Approval freezes the
blueprint and budgets for the Coordinator and Workers. A new physical
constraint requires the Coordinator to obtain explicit user approval through
`ask_question` before changing the blueprint, relaxing a target, removing a
planned retirement, or deferring in-scope work.
The Coordinator uses `ask`/`reply` to resolve architecture disagreements without
accepting dual tracks, sidecar tables or state machines, or Python wrappers and
formatting as substitutes for structural convergence. Tests tied to obsolete
process tables must move to domain entrypoints and authoritative entity state.

On `worker_done`, the Coordinator reviews the diff and the resulting module.
The `architecture-re-review` scene requires rereading Anchor 0 (the initial
review, original requirements, or design), inventorying the current schema,
state machines, subpackage responsibilities, triggers, and read/write paths,
then checking each against the business goal from first principles. Compare
the implementation and budget changes against the approved blueprint, Anchor 0
and the `main` baseline to catch unauthorized changes. Reject table-count
reductions that widen parent tables or add secondary status columns while AST
statements, state enums, control loops, and lanes fail to shrink. Send
structural gaps back for rework before delivery.

Before merging or rolling out migrations that backfill data, tighten
constraints, or drop tables or columns, run a read-only check for orphan rows,
deferred foreign keys, and historical enum or version values in the target
environment. Record the unverified scope when that environment is inaccessible.

### Automatic updates

`pilot self-update` serializes source updates, global skill symlink synchronization,
and project-neutral artifact recompilation while preserving `profile.json` preferences.
The CLI can use a newer clean `~/the-pilot` main checkout immediately. Persisted
`installed_sha` and `installed_tree_digest` keep the uv tool bundle aligned with
the checkout, including after an external pull or a missing launcher.

A uv tool update is a two-stage transaction. `installer.py` first builds a wheel
in-process from the checkout's controlled files and requires its package tree
digest to equal the checkout digest; no subprocess touches the launcher during
that stage, so an unbuildable or inconsistent checkout leaves the live
`~/.local/bin/pilot` intact. Only the verified wheel reaches the single
`uv tool install --force <wheel>` call, after which the launcher and installed
package digest are checked again. A failed command raises `SetupError` carrying
its exit status or timeout plus the tail of its `stderr` (or `stdout`), so
`auto_update.json` and `auto_update.log` record the real cause.

For the default Gemini root, the compiler renders the managed shell startup
block and the installer owns startup file selection. The block defaults
`AGY_CLI_OSC7501` to `off` and forwards interactive `agy` arguments to the
installed runtime's internal `announce-boot` entry point before launching it.
An initial prompt emits `working`, an explicit resume without a prompt emits
`done`, and a fresh promptless launch emits no frame. Orca can then wait for
`agy` to take the foreground before delivering the initial input for a fresh
session. One-shot commands and `AGY_CLI_OSC7501=on` skip the boot announcement.
Account-switch recovery can call the same entry point before invoking `env agy`.
`apply` (including `self-update`'s upgrade) manages `~/.zshenv` and any
existing `~/.bash_profile`, `~/.bashrc`, or `~/.profile`. `status` reports
shell block drift, and `uninstall` strips the block while retaining each file,
its permissions, and any symlink. A skill root supplied to
`self-update --home-dir` leaves shell ownership tied to the default Gemini root.

`SessionStart` announces the initial pane state. The PreInvocation and eligible
Stop hooks request a local engine sync before
evaluating the turn. This sync can repair drifted installed artifacts even when
the runtime matches the checkout. If the engine changes, the hook runs the
updated standalone `runtime.py` once for the current event. A detached
`--local-only` repair preserves the six-hour remote-check timestamp.
`pilot status --json` exposes the persisted result.

The installer alone schedules maintenance. Successful install and runtime
mutations can request it, as can eligible hook syncs. Inspection commands
read the current install and update status. A redirected Gemini root can
repair its own artifacts and Gemini skill links. Home-wide tool and skill
maintenance requires the default Gemini root or an explicit
`self-update --home-dir` override.

Runtime and updater events go to `state/pilot.log` and `state/auto_update.log`.
Each writer serializes rotation and append and keeps one `.1` archive.
