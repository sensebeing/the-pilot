# Contributing to the-pilot

Contributions that make scene-based turn automation and Orca Coordinator/Worker governance clearer, simpler, and safer are welcome.
For a substantial behavior change, open an issue describing the invariant, owner, and observable outcome before implementation. Report vulnerabilities privately using [SECURITY.md](SECURITY.md).

## Set up

Use Python 3.11+ and Git on macOS or Linux:

```sh
git clone https://github.com/sensebeing/the-pilot.git
cd the-pilot
python3 -m pip install -e .
git switch -c your-topic
```

The CLI and runtime have zero external runtime dependencies. Independent architecture review via [the-guard](https://github.com/sensebeing/the-guard) additionally needs a locally installed and authenticated `codex`, `claude`, or `agy`; inspect availability with `guard probe`.

## Develop and verify

Read [AGENTS.md](AGENTS.md) and the [package boundaries](src/the_pilot/README.md) before editing.
We dogfood `the-guard` engineering quality rules:
- One owner per decision (`guard.single-source-of-truth`) and unidirectional module imports (`cli -> installer -> compiler -> profile -> project -> defaults`, with `assets/runtime.py` standalone).
- Zero regular expressions (`import re` is forbidden across `src/the_pilot/`): trailing pilot tags are parsed exclusively via `json.loads`.
- Preserve real safety boundaries: settled-question checks, `fcntl` session locks, turn caps, and explicit `--gemini-dir` test isolation.
- Keep [README.md](README.md) and [README.zh-CN.md](README.zh-CN.md) aligned when changing public usage.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
guard check --repo-root . --base origin/main
```

Commit on your topic branch and run the independent architecture review before opening the PR:

```sh
guard review --repo-root .
guard check-push --repo-root .
```

Resolve blocking findings at their architectural root, or supply code and owner-contract evidence with `guard review --repo-root . --note "<file:line and owner contract evidence>"`. Commit fixes and review again.

## Submit a pull request

This repository uses GitHub PR mode (`review.integration_mode=github-pr`): changes must be committed on a topic branch, pushed to `origin`, and merged through GitHub rather than pushed directly to `main`. Open the PR and publish the independent review to it:

```sh
gh pr create --fill
guard review --repo-root . --github-pr sensebeing/the-pilot#<number>
```

The review is published as `the-guard-reviewer[bot]`; resolve its findings or supply `--note` evidence and rerun until `GO`. Describe the concrete problem, resulting behavior, and verification results. Disclose checks that could not run and unresolved findings. Never include credentials or private session transcripts/logs. Merge the reviewed PR with `gh pr merge`; the local `pre-push` hook blocks direct pushes to `main`.
