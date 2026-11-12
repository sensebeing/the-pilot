# Changelog

Notable changes are recorded here. Version numbers follow [Semantic Versioning](https://semver.org/).

## Unreleased

- Renamed the runtime and installed layout to canonical `the-pilot` identifiers: `<gemini-dir>/config/the-pilot/runtime.py`, the `the-pilot-profile/v1` schema, the `pilot` profile section, the `the-pilot` hook group, `<!-- pilot: ... -->` tags, `,pilot recover`, and `THE_PILOT_*` environment variables.
- `pilot apply --upgrade` now performs a one-way migration of a legacy `config/autopilot` install (profile, project overlay, session state, custom scenes) and removes the legacy hook group; sessions opened before the rename keep working through transitional tag and recovery-command compatibility.

## 0.1.0

Initial open-source alpha release.

- Standalone `pilot` CLI (`probe`, `apply`, `status`, `uninstall`, `mode`, `worker`, `sync`, `set`, `log`, `hook`) with zero external runtime dependencies on Python 3.11+.
- Four-dimension profile customization (`orca-autopilot-profile/v1`) covering Orca Coordinator/Worker priorities, Turn Autopilot mode (`gate`, `confirm`, `off`) and turn caps, four-scene state machine (`plan-and-orchestrate`, `architecture-re-review`, `deliver-and-verify`, `closeout-and-sync`), and entry/comma shortcuts.
- Project extension overlay tier (`.pilot/project.json`, `the-pilot-project/v1`) for repository-specific shortcuts, presets, delivery strategies, and closeout instructions without forking the compiler.
- Zero-regex runtime engine (`autopilot.py`) that parses only the trailing structured JSON tag (`<!-- autopilot: {...} -->`) via `json.loads`, preserving literal Markdown and HTML comment tokens inside JSON strings.
- Mechanical delivery safety gates: synchronous/asynchronous question-settlement verification, `transcript_full.jsonl` hydration, `fcntl` session locks, ambiguous-delivery reservation (`pending`/`unknown`), and next-turn worker priority hot reload.
- Agent skills (`the-pilot-setup` and `the-pilot`), bilingual English and Simplified Chinese documentation, and `the-guard` architecture policy enforcement.
