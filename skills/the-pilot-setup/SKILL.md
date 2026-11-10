---
name: the-pilot-setup
description: Install, customize, upgrade, inspect or remove The Pilot through interactive four-dimension onboarding while preserving user configuration.
---

# Set up The Pilot

Read [customization and operations](references/customization-guide.md).
Use `pilot` from PATH, or `<the-pilot-checkout>/bin/pilot`. Python 3.11+ is required.

1. Run `pilot probe --json` (add `--project <repo>` for a project overlay).
   Inspect installed preferences and available CLIs without writing files.
2. Ask about four dimensions, keeping an existing profile as the starting point:
   orchestration worker/review priorities; autopilot mode/turn limit;
   enabled scenes/delivery policy/notification preference/guardrails;
   entry shortcuts/custom commands/template overrides. Present a recommended
   option first. Do not assume permission to send mail or deploy production.
3. Apply the confirmed preferences with `pilot apply --preset recommended
   --project <repo> --profile <json-or-file> --json`. Omit the project flag for
   discovery, or use `--no-project` to explicitly choose universal defaults.
   Upgrade with `pilot apply --upgrade --json` to retain personal preferences
   and the installed project link from any directory.
4. Run `pilot status --json`; explain any drift. Give a personalized cheatsheet
   of enabled shortcuts, modes, worker selection, sync and recovery.

`apply` preserves other Hook groups, unmanaged GEMINI.md prose and custom scenes.
`pilot uninstall` removes owned files, including the installed project link.
Tests and demonstrations must pass an explicit temporary `--gemini-dir` and
set `ORCA_AUTOPILOT_GEMINI_DIR` for runtime operations.
