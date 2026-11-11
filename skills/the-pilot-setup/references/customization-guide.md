# Customization and operations

## Three configuration tiers

Universal templates live in the package. `.pilot/project.json` carries a project
policy overlay. Installed `profile.json` stores personal preferences; generated
`config.json` and GEMINI.md guidance are derived from those inputs.
Installed scene files can be edited for runtime use; `pilot sync` refreshes the
GEMINI.md scene summary. An upgrade recompiles scenes from the profile/overlay.

Discovery precedence is `--no-project`, explicit `--project` (root or JSON),
installed `config/autopilot/project.json`, nearest cwd ancestor's `.pilot/project.json`,
then universal defaults. Apply persists the selected normalized overlay; universal
apply deletes a previous link. Probe and status never write the link.

## Profile dimensions

- `orchestration`: ordered `worker_agents` and `review_agents`.
- `autopilot`: `mode` (`gate`, `confirm`, `off`) and `max_consecutive_auto_turns` (`1..50`).
- `scenes`: enabled scenes, delivery strategy, feedback notification preference,
  and additional guardrails. An overlay defines what the notification preference
  means; it never grants external-send permission by itself.
- `shortcuts`: enabled built-in entries, custom entries, custom comma commands,
  template overrides keyed by entry, alias or command identity.

Presets: recommended uses gate/8 turns/four scenes/four universal entries;
conservative uses confirm/4 turns; orchestration-only uses off/no scenes/no entries.
A project may override these presets. User overrides are applied last.

`pilot apply --help` lists individual override flags. `--profile` accepts inline
JSON, a file, or `@file`; `--upgrade` uses installed preferences and cannot be
combined with `--profile`. Use `pilot set` for durable template edits.

## Safety and repair

- Nonempty drift: inspect it, then apply an upgrade if regeneration is intended.
- Unmanaged scene markers: remove or relocate the obsolete manual scene block;
  installation refuses ambiguous managed-region ownership before artifact writes.
- Unknown delivery: inspect `pilot log`, then explicitly request recovery.
- Invalid overlay/profile: fix its reported contract error; nothing is installed.
- Worker priority updates are locked and persistent; isolated roots do not sync
  other real client configuration files. Existing conversations load changes at
  their next PreInvocation, including after a new compaction checkpoint.

The installer locks the profile and writes each file via same-directory atomic
replacement. This is not a multi-file transaction; rerun a valid apply after a
filesystem failure. Uninstall preserves unrelated content and runtime state.
