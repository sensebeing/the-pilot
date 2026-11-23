# Changelog

Notable changes are recorded here. Version numbers follow [Semantic Versioning](https://semver.org/).

## Unreleased

- `the-pilot` now builds wheels through a zero-dependency `src/the_pilot/packaging.py` PEP 517/660 backend (`requires = []`) that enumerates only the controlled package files and never reads or writes `build/`, `dist/` or `*.egg-info`; `setuptools`, `setup.py` and the `build/` cleanup patch are retired.
- `pilot self-update` builds and digest-verifies the wheel in-process before a single `uv tool install --force <wheel>`, so a failed build can no longer delete the current `~/.local/bin/pilot`, and an installed tree whose digest matches its checkout no longer force-reinstalls the same commit every 30 seconds.
- `subprocess_run` raises `SetupError` with the failed command's exit status or timeout plus the tail of its `stderr` (or `stdout`), so `auto_update.json` and `auto_update.log` name the real cause instead of only `returned non-zero exit status 2`.
- Added `OSC 7501` (`terminal.program-status.v1`) terminal status emission from the `PreInvocation` and `Stop` hooks (`working` / `done` / `blocked:kind=question`), resolving the owning `agy` TTY through up to three shell wrapper hops or the explicit `THE_PILOT_OSC_TARGET` override, and draining transient non-blocking PTY backpressure with a bounded `select` loop instead of dropping frames.
- Codified the two-tier Orca orchestration contract: Tier-1 top-level workspaces always run the native AGY coordinator (`agy --dangerously-skip-permissions`, `trustedWorkspaces` registration, never `claude`/`codex`), while Tier-2 child workers run through `orca orchestration worker-start` and report via `worker_done`; cross-repo callbacks use `--worktree new-child --repo path:<root>`. The profile accepts a normalized `orchestration.coordinator_agent` (`agy`, alias `antigravity`) and defaults legacy profiles to `agy`.
- A default `$HOME/.gemini` install now owns a `<!-- BEGIN/END THE_PILOT_ORCHESTRATION -->` block in `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, keeping `pilot apply`, `pilot status` drift, `uninstall`, and `,worker` hot-switching in sync; installs whose resolved root differs from `$HOME/.gemini` never touch those files. Upgrade migrates one pre-existing unmanaged orchestration section into the managed block, fails closed on duplicate legacy sections, and preserves the user file's symlink target, mode, and existence.
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
