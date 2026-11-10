# Changelog

Notable changes are recorded here. Version numbers follow [Semantic Versioning](https://semver.org/).

## 0.1.0

Initial open-source alpha release.

- Standalone `pilot` CLI (`probe`, `apply`, `status`, `uninstall`, `mode`, `worker`, `sync`, `set`, `log`, `hook`) with zero external runtime dependencies on Python 3.11+.
- Four-dimension profile customization (`orca-autopilot-profile/v1`) covering Orca Coordinator/Worker priorities, Turn Autopilot mode (`gate`, `confirm`, `off`) and turn caps, four-scene state machine (`plan-and-orchestrate`, `architecture-re-review`, `deliver-and-verify`, `closeout-and-sync`), and entry/comma shortcuts.
- Project extension overlay tier (`.pilot/project.json`, `the-pilot-project/v1`) for repository-specific shortcuts, presets, delivery strategies, and closeout instructions without forking the compiler.
- Zero-regex runtime engine (`autopilot.py`) that parses only the trailing structured JSON tag (`<!-- autopilot: {...} -->`) via `json.loads`, preserving literal Markdown and HTML comment tokens inside JSON strings.
- Mechanical delivery safety gates: synchronous/asynchronous question-settlement verification, `transcript_full.jsonl` hydration, `fcntl` session locks, ambiguous-delivery reservation (`pending`/`unknown`), and next-turn worker priority hot reload.
- Agent skills (`the-pilot-setup` and `the-pilot`), bilingual English and Simplified Chinese documentation, and `the-guard` architecture policy enforcement.
