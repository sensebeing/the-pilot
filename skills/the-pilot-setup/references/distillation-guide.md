# Distilling scenes and shortcuts from recent chat history

Run `pilot distill --days 7 --json` (optional `--max-sessions 80`). The command performs a read-only scan of recent conversations across Antigravity/Gemini CLI, Claude Code, Codex, and Cursor, filters out machine-generated Orca worker and independent reviewer preambles, separates confirmed Autopilot activity, and clusters recurring manual user prompts. It is evidence, not an automatic configuration write.

## Evidence review and uncertainty

1. **Check source and configuration diagnostics first**:
   Inspect `config_status` and each agent entry in `sources`. If a source reports `degraded` (`read_errors` or `malformed_lines`), `truncated_files`, or `undated_messages`, treat its counts as partial evidence and surface that limitation to the user rather than claiming complete 7-day coverage.
2. **Consume structured recommendations, verify turn role, and exclude external auto-dispatches**:
   Use the command's `recurring_patterns` and `recommendations` as the baseline. Only propose an entry shortcut when the pattern's `turn_role` is confirmed as `entry`; treat `unknown` (from truncated or history-only logs) and `followup` patterns as mid-session instructions or scene expectations. Before proposing shortcuts or scene edits, distinguish genuine human-typed prompts from external automated task or wave dispatches (such as alert, quality-report, feedback-claim, or inbox auto-dispatchers that inject multi-clause directive templates or mailbox check commands into sessions).
3. **Perform semantic clustering over `sampled_prompts`**:
   Deterministic signatures normalize IDs, URLs, paths, and numbers, while `sampled_prompts` preserves recent manual prompts whose wording varies. Read `sampled_prompts` to spot multi-turn habits that share the same intent (such as repeated architecture-review criteria, custom verification commands, or closeout reminders).
4. **Separate personal preferences from project overlays**:
   Personal mode preferences, turn limits, and personal aliases belong in `profile.json` via `pilot apply`. Team-shared entry commands, delivery wording, and repository closeout rules belong in `.pilot/project.json`.

Present the observed frequency, representative examples, and proposed profile or project overlay changes to the user and wait for confirmation before running `pilot apply`. For tests and isolated demonstrations, always pass explicit temporary `--home-dir` and `--gemini-dir` paths.
