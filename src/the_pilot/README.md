# Module ownership

- `defaults.py`: universal built-in presets, `PIPELINE_STAGE_CONTRACTS`, and project-neutral templates.
- `project.py`: pure overlay normalization and composition for explicit library callers.
- `profile.py`: durable user preference normalization and preset resolution.
- `compiler.py`: pure rendering and managed-document merges; no IO.
- `distill.py`: read-only sampling and pattern distillation across recent local agent chat logs, plus scene pipeline assessment of the installed profile and scene contracts.
- `installer.py`: the sole owner of global installation, `self-update`, installation locking and artifact writes. It removes obsolete installed project snapshots during apply and uninstall.
- `assets/runtime.py`: the standalone runtime, copied unchanged on install. It owns per-turn workspace shortcut discovery and validation, synchronous Stop submission, post-Stop turn-start observation and settlement of unconfirmed delivery. The package reuses its shortcut schema, comma command rendering and log rotation.
- `runtime.py`: subprocess adapter to the installed runtime (no second engine).
- `cli.py`: installer versus runtime command routing only.

Dependencies flow from the CLI to installer, compiler, profile, project and
defaults. The standalone asset has standard-library dependencies only. Distill
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

For the default Gemini root, the compiler renders the managed
`AGY_CLI_OSC7501` block and the installer owns startup file selection.
`apply` (including `self-update`'s upgrade) manages `~/.zshenv` and any
existing `~/.bash_profile`, `~/.bashrc`, or `~/.profile`. `status` reports
shell block drift, and `uninstall` strips the block while retaining each file,
its permissions, and any symlink. A skill root supplied to
`self-update --home-dir` leaves shell ownership tied to the default Gemini root.

The PreInvocation and eligible Stop hooks request a local engine sync before
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
