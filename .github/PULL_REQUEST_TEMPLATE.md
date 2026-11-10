## Problem and outcome

Describe the concrete problem and resulting behavior. Link related issues if applicable.

## Architecture and scope

Identify the decision owner (`defaults`, `project`, `profile`, `compiler`, `installer`, `cli`, or `assets/autopilot.py`), affected boundaries, and any replaced mechanisms removed.

## Verification

List commands and results, including skipped checks and unresolved findings.

- [ ] Read `AGENTS.md` and `src/the_pilot/README.md`.
- [ ] Ran `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- [ ] Ran `guard check --repo-root . --base origin/main`.
- [ ] Ran `guard review --repo-root .` on committed changes and resolved blockers or supplied evidence.
- [ ] Ran `guard check-push --repo-root .` for the reviewed patch.
- [ ] Updated both `README.md` and `README.zh-CN.md` when changing public usage.

## Risks or limitations

Describe compatibility, delivery safety, or operational implications; omit private code, transcripts, and credentials.
