# Security policy

## Supported versions

Security fixes target the latest release and `main`. The project is currently in the 0.1.x alpha series; older releases do not have a separate security maintenance commitment.

## Report a vulnerability privately

Use GitHub's **Report a vulnerability** option in this repository's [Security tab](https://github.com/sensebeing/the-pilot/security). If private reporting is unavailable, open a minimal issue asking maintainers for a private reporting channel, without vulnerability details. Do not disclose exploits, credentials, private code, or sensitive transcripts in public issues.

Include the affected version or commit, operating system, reproduction steps, expected trust boundary, and potential impact. Redact secrets and use a minimal synthetic configuration or transcript when possible. Coordinate disclosure with maintainers while they investigate and prepare a fix.

## Trust boundaries

`the-pilot` compiles local agent instructions and invokes `orca terminal send` for the bound session terminal when lifecycle hooks (`PreInvocation` and `Stop`) fire.
- Package installation does not modify user configuration automatically; only an explicit `pilot apply` installs managed files under the selected Gemini configuration directory.
- Scene routing and `gate=auto|pause` classifications are developer-side workflow controls, not an OS sandbox or remote authorization gate. Never rely on prompt text alone to enforce production deployment or external messaging permissions.
