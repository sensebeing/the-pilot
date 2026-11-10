---
name: the-pilot
description: Operate scene-based Turn Autopilot in Orca, inspect modes, switch worker priority, edit shortcuts, sync scenes and recover ambiguous delivery.
---

# Operate The Pilot

Run `pilot status --json` first. If absent, use the sibling `the-pilot-setup`
skill. Inspect `pilot --help` and subcommand help for current argument syntax.

- `,mode status|gate|confirm|off` or `pilot mode <mode>` controls automatic turns.
- `,worker [codex|claude|cc|status]` (alias `,agent`) or `pilot worker <target>`
  changes durable executor priority. Existing sessions load it on the next turn.
- `,1`, `,2`, `,3 [context]` select the visible next-step candidates.
- `pilot sync` refreshes scene guidance after editing installed scene files.
- `pilot set <shortcut> <template>` persists a shortcut template preference.
- `pilot log` inspects recent runtime events.
- `,autopilot recover` explicitly recovers an ambiguous delivery only after
  checking what the previous send actually did. Never blindly replay a send.

The model owns scene and intent decisions; the runtime owns mechanical delivery,
question settlement, turn limits and session locking. Visible suggestions are
the execution source; hidden tags cannot introduce an unseen candidate.
Do not invent authorization from a scene or change existing write safety gates.

Runtime commands select the installation with `ORCA_AUTOPILOT_GEMINI_DIR`.
For tests use a temporary root. Never use real user directories as fixtures.
Project overlays and long-term preferences are managed by `pilot apply`; use
`pilot apply --upgrade` to recompile an installation without resetting preferences.
The Pilot does not dispatch network work or run background Dealer daemons.
