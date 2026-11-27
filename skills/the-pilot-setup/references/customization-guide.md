# Customization and operations

## Three configuration tiers

Universal templates live in the package. Installed `profile.json` stores
personal preferences. The installer compiles global `config.json`, scene files,
and agent guidance from these inputs. `pilot sync` refreshes the GEMINI.md scene
summary after a scene edit. Upgrade recompiles the built-in scenes from the
installed profile and removes any obsolete installed project snapshot.

Put shared repository shortcuts in `.pilot/project.json`. PreInvocation finds
the nearest overlay from the active workspace and resolves its `entry_order`
and `entry_specs` for that turn. A built-in entry can override individual fields
and inherit the rest from the universal catalog. Use the repository's `AGENTS.md` or `CLAUDE.md`,
runbooks, and authorized skills for delivery, notifications, and closeout rules.
The project overlay stays in the repository.

## Profile dimensions

- `orchestration`: Tier-1 `coordinator_agent` (fixed to native `agy`, accepting
  the `antigravity` alias), ordered `worker_agents` and `review_agents`, and
  optional `worker_models`. Tier-2 `worker_agents` accepts `agy` (alias
  `antigravity`), `codex`, and `claude`. Orca launches AGY with `--agent antigravity`.
  `worker_models` stores a `model` and optional `effort` by agent, including
  agents outside the active order.
- `pilot`: `mode` (`gate`, `confirm`, `off`) and `max_consecutive_auto_turns` (`1..50`).
- `scenes`: enabled scenes, delivery strategy, and additional guardrails.
  Repository runbooks and current authorization govern external sends.
- `shortcuts`: enabled built-in entries, custom entries, custom comma commands,
  template overrides keyed by entry, alias or command identity. Reserved built-in
  comma commands (`,1`–`,3`, `,mode`, `,worker`, `,agent`, `,pilot`, `,autopilot`,
  `,rr`, `,loop`) cannot be overridden by personal profiles or `.pilot/project.json`.
  `,rr [focus]` runs a single-wave Anchor 0 re-review; `,loop [1-5] [goal]` arms
  bounded multi-wave convergence (default `3`, controlled via `,loop status` / `,loop off`).

The recommended preset enables gate mode, eight turns, four scenes, and four
universal entries. The conservative preset uses confirm mode and four turns.
The orchestration-only preset switches Pilot off and disables scenes and entry
shortcuts, including workspace entries. User overrides are applied last.

`pilot apply --help` lists individual override flags. `--profile` accepts inline
JSON, a file, or `@file`, and explicitly replaces installed preferences;
`--preset` explicitly resets them to that preset. Otherwise apply reuses an
installed `profile.json`. `--upgrade` requires an installed `profile.json` and
cannot be combined with `--preset` or `--profile`. Run `pilot set` inside the
workspace to change a repository shortcut's personal template. That preference
applies when the workspace supplies the shortcut. Use `pilot distill --days 7 --json` (see [distillation-guide.md](distillation-guide.md))
to discover repeated user prompt patterns across local coding agents before
proposing custom shortcuts or scene guardrails; distillation filters machine-generated
orchestration, account-switch recovery, and Dealer inbox messages and counts built-in
`,rr` / `,loop` usage under `pilot_activity`.

Set worker launch preferences with repeatable `--worker-model` flags. For example,
`pilot apply --worker-agents agy,codex,claude --worker-model agy=gemini-4-neon-high:high`
sets AGY first and selects its model and effort. Use `<agent>=<model>` to set a
model and clear its saved effort. Use `<agent>=` or `<agent>=none` to remove the
agent's model override. The JSON `worker_models` form accepts model IDs with
colons and uses `null` to clear an agent's override.

## Safety and repair

- Nonempty drift: inspect it, then apply an upgrade if regeneration is intended.
- Unmanaged scene markers: remove or relocate the obsolete manual scene block;
  installation refuses ambiguous managed-region ownership before artifact writes.
- Unknown delivery: inspect `pilot log`, then explicitly request recovery.
- Invalid workspace overlay: check `pilot log` for `project_overlay_invalid`.
  The hook keeps the universal catalog available. An invalid profile must be
  repaired before installation.
- Worker priority updates are locked and persistent; isolated roots do not sync
  other real client configuration files. A default `$HOME/.gemini` install also
  maintains the managed `THE_PILOT_ORCHESTRATION` block in `~/.claude/CLAUDE.md`
  and `~/.codex/AGENTS.md`; a resolved install root other than `$HOME/.gemini`
  never touches them. Existing conversations load changes at their next
  PreInvocation, including after a new compaction checkpoint.

The installer locks the profile and writes each file via same-directory atomic
replacement. This is not a multi-file transaction; rerun a valid apply after a
filesystem failure. Uninstall preserves unrelated content and runtime state.
