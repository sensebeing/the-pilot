# The Pilot

**Turn a completed agent turn into a safe, contextual next step.**

The Pilot provides scene-based turn automation and Orca coordinator/worker
governance. Python 3.11+, zero external runtime dependencies; macOS and Linux
(`fcntl` locking). Orca and an Antigravity-compatible lifecycle Hook host are
required for actual terminal delivery. Compiler and installer tests run offline.

English | [简体中文](README.zh-CN.md)

## Get started in 30 seconds — paste to your agent

```text
Set up The Pilot while preserving my existing configuration.
Use my trusted checkout at ~/the-pilot (Python >=3.11), or obtain the project
from https://github.com/sensebeing/the-pilot when published.
Read skills/the-pilot-setup/SKILL.md and run bin/pilot probe --json.
Ask me about worker priorities, turn mode/limit, scenes/delivery, and shortcuts.
If this repository has .pilot/project.json, use --project <repo>.
Apply my confirmed preferences, check status for no drift, and give a cheatsheet.
Do not treat setup as authorization to send mail or deploy production.
```

## Local installation

```sh
# No pip install is required for the source launcher.
~/the-pilot/bin/pilot probe --no-project --json
~/the-pilot/bin/pilot apply --no-project --preset recommended --json
~/the-pilot/bin/pilot status --json
# Alternatively: python3 -m pip install /path/to/the-pilot
```

Register `skills/the-pilot` and `skills/the-pilot-setup` with your agent's skill
loader. The setup skill guides customization; the operations skill handles daily
mode switching, shortcuts and recovery. No package installation auto-writes user
configuration: only an explicit `pilot apply` installs the Hook runtime.

## Architecture and ownership

```text
Universal specifications + .pilot/project.json + personal profile.json
                       ↓ pure compilation
config.json + scenes/*.md + GEMINI.md managed block + Hook group
                       ↓ standalone autopilot.py
PreInvocation → shortcut/worker reload → agent turn → Stop → Orca terminal
```

The coordinator owns intent, architecture and review. Workers implement in their
assigned branches and report completion. The runtime mechanically gates delivery
on answered questions, visible candidates, turn limits and session locks. It
never replaces the agent's semantic judgment or grants deployment/send authority.
Pending/unknown delivery remains reserved until a new user turn or explicit
recovery; ambiguous effects are never automatically replayed.

Module boundaries are documented in [src/the_pilot/README.md](src/the_pilot/README.md).
The Guard owns engineering review; The Dealer owns collaboration transport and
cross-device dispatch. The Pilot owns this conversation's turn progression.

## Four-scene state machine

| Scene | Trigger | Next responsibility |
|---|---|---|
| plan-and-orchestrate | Agreed design or diagnosis requires implementation | Coordinate workers against explicit contracts |
| architecture-re-review | Implementation or fixes need review | Review the whole task and simplest architecture |
| deliver-and-verify | Review is clear, delivery obligations remain | Complete authorized delivery and verification |
| closeout-and-sync | Delivery is complete, cleanup obligations remain | Sync docs and release owned resources |

Review findings return to implementation. Awaiting an answer or completing all
obligations yields `scene=NONE`. `gate=auto` may submit in gate mode; `gate=pause`
only prefills. Confirm mode always requires Enter; off disables end-turn prefill.
Real E2E and production actions retain their explicit authorization boundaries.

## Project extension tier

Copy [examples/project.example.json](examples/project.example.json) to
`<repo>/.pilot/project.json`. Schema `the-pilot-project/v1` supports `projectId`,
`description`, `entry_order`, `entry_specs`, `presets`, `scene_specs`,
`deliver_content` and `feedback_closeout`. Overrides merge into universal
specifications, not mutable process globals. A new entry requires its key in
`entry_order`; all entry identities must be unique. Built-in scene filenames are
fixed to prevent writes outside the managed scene directory. Unknown fields fail
validation. The example overrides closeout without copying the whole compiler.

```sh
pilot apply --project /path/to/repo --preset recommended --json
pilot apply --upgrade --json  # preserves installed overlay from any directory
pilot apply --no-project --json  # replaces project link with universal defaults
```

Discovery: explicit `--no-project` → explicit `--project` root/JSON → installed
project.json → nearest cwd ancestor overlay → universal defaults. Apply saves a
copy under the selected Gemini root; later source overlay edits require explicit
`--project` refresh. Status compares against the installed copy. Universal mode
has only `r`, `rt`, `d`, `pull`; projects can add domain shortcuts and closeout text.
Personal preferences remain in `profile.json`; an overlay never replaces them.

## CLI cheatsheet

| Command | Purpose |
|---|---|
| `pilot probe --json` | Read-only environment and preference inspection |
| `pilot apply --preset conservative --json` | Compile and install |
| `pilot apply --upgrade --json` | Refresh engine/artifacts, preserve preferences |
| `pilot status --json` | Installation and drift report |
| `pilot uninstall --json` | Remove owned artifacts and project link |
| `pilot worker codex` / `,worker codex` | Persist and hot-load worker priority |
| `pilot mode confirm` / `,mode confirm` | Change turn mode |
| `pilot hook --event PreInvocation` | Lifecycle Hook entry (also Stop) |
| `pilot sync` | Refresh guidance from installed scene files |
| `pilot set r 'Review {arg}'` | Persist a shortcut template |
| `pilot log` | Inspect delivery events |
| `,1`, `,2`, `,3` | Select visible candidates |
| `,autopilot recover` | Explicit recovery from ambiguous delivery |

Installer commands accept `--gemini-dir`; runtime commands use
`ORCA_AUTOPILOT_GEMINI_DIR`. Other Hook groups, unmanaged GEMINI.md content and
custom scene files are preserved. Writes are atomic per file under the profile
lock; rerun apply after filesystem interruption. Installed scenes are editable
runtime inputs, while an upgrade recompiles them from the durable preferences.

## Verification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
guard check --repo-root . --policy-only
guard check --repo-root . --base origin/main
guard review --repo-root .
```

Tests use temporary roots, fake Orca and explicit isolation. The imported
regression suite covers question settlement, transcript hydration, delivery
locking, candidate parsing and executor hot reload. Frozen pre-extraction
hashes verify all artifacts across presets, delivery strategies, notification
preferences and custom shortcuts. They are evidence, not another compiler.

MIT licensed. See [LICENSE](LICENSE).
