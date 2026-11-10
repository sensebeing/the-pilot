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
- One owner per decision (`guard.single-source-of-truth`) and unidirectional module imports (`cli -> installer -> compiler -> profile -> project -> defaults`, with `assets/autopilot.py` standalone).
- Zero regular expressions (`import re` is forbidden across `src/the_pilot/`): trailing autopilot tags are parsed exclusively via `json.loads`.
- Preserve real safety boundaries: settled-question checks, `fcntl` session locks, turn caps, and explicit `--gemini-dir` test isolation.
- Keep [README.md](README.md) and [README.zh-CN.md](README.zh-CN.md) aligned when changing public usage.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
guard check --repo-root . --base origin/main
```

Commit on your topic branch before independent architecture review:

```sh
guard review --repo-root .
guard check-push --repo-root .
```

Resolve blocking findings at their architectural root, or supply code and owner-contract evidence with `guard review --repo-root . --note "<file:line and owner contract evidence>"`. Commit fixes and review again.

## Submit a pull request

Describe the concrete problem, resulting behavior, and verification results. Disclose checks that could not run and unresolved findings. Never include credentials or private session transcripts/logs. Maintainers review contributions and decide when to integrate them; a local review receipt does not replace that decision or grant permission to push to `main`.
