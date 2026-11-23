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

1. Run `pilot probe --json` and
   `pilot distill --days 7 --json`. Inspect installed preferences, available CLIs,
   and the user's last 7 days of chat history across Antigravity, Claude Code,
   Codex, and Cursor without writing files.
2. Review the distillation report on two levels before proposing changes.
   Summarize recurring user prompt patterns (`recurring_patterns` and semantic
   patterns in `sampled_prompts`). Then assess the installed scene pipeline that
   the report describes: whether every stage of the flow from planning to
   closeout is enabled with a complete contract, whether each stage states an
   explicit deliverable and exit quality bar, whether stages connect without
   manual handoffs, and whether custom scenes or guardrails each protect a
   distinct invariant that the built-in contracts do not already cover. Propose concrete scene,
   guardrail, and shortcut automations (`custom_entry`, `custom_comma`,
   `deliver_strategy`, or `.pilot/project.json` overlay updates) from that
   evidence. Ask about the four dimensions, keeping an existing profile as the
   starting point: orchestration worker/review priorities; pilot mode/turn
   limit; enabled scenes/delivery policy/guardrails;
   entry shortcuts/custom commands/template overrides. Present a recommended
   option first. Do not assume permission to send mail or deploy production.
3. Apply the confirmed preferences with `pilot apply --preset recommended
   --profile <json-or-file> --json`. Global artifacts stay project-neutral.
   Put repository shortcuts in `.pilot/project.json` for in-memory resolution
   during each workspace's `PreInvocation`. Document delivery and closeout in
   the repository's guidance and runbooks. Re-running `pilot apply` preserves
   the installed profile preferences unless `--preset` or `--profile` is given.
   Use `pilot apply --upgrade --json` to recompile from the installed profile and
   remove any obsolete global project snapshot.
4. Run `pilot status --json`; explain any drift. Give a personalized cheatsheet
   of enabled shortcuts, modes, worker selection, sync and recovery.

`apply` preserves other Hook groups, unmanaged GEMINI.md prose and custom scenes.
It registers `SessionStart`, `PreInvocation`, and `Stop`. The installed runtime
owns the paired OSC 7501 activity and OSC 2 title frames used by Orca.
On the default Gemini root, `apply` manages the shell startup block in `~/.zshenv`
and existing bash or POSIX shell startup files. The block delegates interactive
`agy` startup and resume announcements to the installed runtime. `pilot status`
reports block drift.
`pilot uninstall` removes owned files, including any obsolete project snapshot.
It strips the shell block while retaining the startup files.
Tests and demonstrations must pass explicit temporary `--gemini-dir` and
`--home-dir` paths and set `THE_PILOT_GEMINI_DIR` for runtime operations.

## Automatic updates

After setup, `pilot self-update` can refresh a clean main checkout, synchronize skill links, and run `apply --upgrade` to refresh managed artifacts while preserving user preferences. Use `pilot status --json` to inspect its last result.
