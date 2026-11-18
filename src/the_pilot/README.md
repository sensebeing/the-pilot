# Module ownership

- `defaults.py`: universal vocabulary, `PIPELINE_STAGE_CONTRACTS`, and project-neutral templates only.
- `project.py`: pure overlay validation and composition; no filesystem access.
- `profile.py`: durable user preference normalization, project-switch shortcut migration, and preset resolution.
- `compiler.py`: pure rendering and managed-document merges; no IO.
- `distill.py`: read-only sampling and pattern distillation across recent local agent chat logs, plus scene pipeline assessment of the installed profile and scene contracts.
- `installer.py`: the sole owner of project discovery, workspace overlay synchronization, `self-update`, installation locking and atomic file writes.
- `assets/runtime.py`: the single runtime owner, copied unchanged on install; it only forwards `--workspace` to the installer CLI.
- `runtime.py`: subprocess adapter to the installed runtime (no second engine).
- `cli.py`: installer versus runtime command routing only.

Dependencies flow from the CLI to installer, compiler, profile, project and
defaults. Distill reuses profile, project and compiler helpers for read-only
validation and scene parsing. Runtime operations execute the installed standalone
asset. Package bootstrap can load code from a verified checkout using Git
provenance and update metadata. Project discovery remains an installer operation.

`project=None` means universal defaults. Library callers pass an overlay to both
`resolve_profile(base, project=overlay)` and `compile_bundle(profile, project=overlay)`.
Only installer operations perform discovery. Inputs are copied, never mutated.

### Automatic updates

`pilot self-update` serializes source updates, global skill symlink synchronization,
and runtime recompilation while preserving `profile.json` and `project.json`.
The CLI can use a newer clean `~/the-pilot` main checkout immediately. Persisted
`installed_sha` and `installed_tree_digest` keep the uv tool bundle aligned with
the checkout, including after an external pull or a missing launcher.

The PreInvocation and eligible Stop hooks request a local project sync before
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
