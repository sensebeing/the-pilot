# Module ownership

- `defaults.py`: universal vocabulary, `PIPELINE_STAGE_CONTRACTS`, and project-neutral templates only.
- `project.py`: pure overlay validation and composition; no filesystem access.
- `profile.py`: durable user preference normalization and preset resolution.
- `compiler.py`: pure rendering and managed-document merges; no IO.
- `distill.py`: read-only sampling and pattern distillation across recent local agent chat logs, plus scene pipeline assessment of the installed profile and scene contracts.
- `installer.py`: the sole owner of project discovery, workspace overlay synchronization, `self-update`, installation locking and atomic file writes.
- `assets/autopilot.py`: the single runtime owner, copied unchanged on install; it only forwards `--workspace` to the installer CLI.
- `runtime.py`: subprocess adapter to the installed runtime (no second engine).
- `cli.py`: installer versus runtime command routing only.

Dependencies flow from the CLI to installer, compiler, profile, project and
defaults; distill reuses profile, project and compiler helpers for read-only
validation and scene parsing. Runtime operations execute the installed standalone
asset. Importing the package does not discover a project, read a user
configuration or initialize a runtime.

`project=None` means universal defaults. Library callers pass an overlay to both
`resolve_profile(base, project=overlay)` and `compile_bundle(profile, project=overlay)`.
Only installer operations perform discovery. Inputs are copied, never mutated.

### Automatic updates

`pilot self-update` serializes source updates, global skill symlink synchronization, and runtime recompilation while preserving `profile.json` and `project.json`. The runtime invokes it in the background after a six hour cooldown and forwards the workspace path for project overlay synchronization; `pilot status --json` exposes the persisted result.
