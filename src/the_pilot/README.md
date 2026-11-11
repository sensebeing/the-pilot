# Module ownership

- `defaults.py`: universal vocabulary, `PIPELINE_STAGE_CONTRACTS`, and project-neutral templates only.
- `project.py`: pure overlay validation and composition; no filesystem access.
- `profile.py`: durable user preference normalization and preset resolution.
- `compiler.py`: pure rendering and managed-document merges; no IO.
- `distill.py`: read-only sampling and pattern distillation across recent local agent chat logs, plus scene pipeline assessment of the installed profile and scene contracts.
- `installer.py`: project discovery, installation lock and atomic file writes.
- `assets/autopilot.py`: the single runtime owner, copied unchanged on install.
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
