---
name: the-pilot-setup
description: Install, customize, distill from 7-day multi-agent chat history, upgrade, inspect or remove The Pilot through interactive four-dimension onboarding while preserving user configuration.
---

# Set up The Pilot

Read [customization and operations](references/customization-guide.md) and
[distilling scenes and shortcuts](references/distillation-guide.md).
Check Python >= 3.11 and `pilot --help`. If the CLI is not installed, install it from GitHub
(`uv tool install git+https://github.com/sensebeing/the-pilot` or
`python3 -m pip install git+https://github.com/sensebeing/the-pilot`), from a local checkout
(`python3 -m pip install /path/to/the-pilot`), or run `<the-pilot-checkout>/bin/pilot`.
To register the skills globally across coding agents, run
`npx skills add sensebeing/the-pilot --global --agent '*'` or symlink `skills/the-pilot*`.

1. Run `pilot probe --json` (add `--project <repo>` for a project overlay) and
   `pilot distill --days 7 --json`. Inspect installed preferences, available CLIs,
   and the user's last 7 days of chat history across Antigravity, Claude Code,
   Codex, and Cursor without writing files.
2. Summarize recurring user prompt patterns (`recurring_patterns` and semantic
   patterns in `sampled_prompts`) and propose concrete scene, guardrail, and
   shortcut automations (`custom_entry`, `custom_comma`, `deliver_strategy`, or
   `.pilot/project.json` overlay updates). Ask about the four dimensions, keeping
   an existing profile as the starting point: orchestration worker/review priorities;
   autopilot mode/turn limit; enabled scenes/delivery policy/notification
   preference/guardrails; entry shortcuts/custom commands/template overrides.
   Present a recommended option first. Do not assume permission to send mail or
   deploy production.
3. Apply the confirmed preferences with `pilot apply --preset recommended
   --project <repo> --profile <json-or-file> --json`. Omit the project flag for
   discovery, or use `--no-project` to explicitly choose universal defaults.
   Upgrade with `pilot apply --upgrade --json` to retain personal preferences
   and the installed project link from any directory.
4. Run `pilot status --json`; explain any drift. Give a personalized cheatsheet
   of enabled shortcuts, modes, worker selection, sync and recovery.

`apply` preserves other Hook groups, unmanaged GEMINI.md prose and custom scenes.
`pilot uninstall` removes owned files, including the installed project link.
Tests and demonstrations must pass explicit temporary `--gemini-dir` and
`--home-dir` paths and set `ORCA_AUTOPILOT_GEMINI_DIR` for runtime operations.
