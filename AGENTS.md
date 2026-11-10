# Agent instructions

Read the engineering policy and [module boundaries](src/the_pilot/README.md)
before changing code. Keep project-specific policy in overlays, not the universal
compiler or runtime. Installed `autopilot.py` must remain a standalone standard
library script. Do not add a second runtime implementation.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and
`guard check --repo-root . --base origin/main` with Python 3.11+, then run
`guard review` before pushing to `main`. Every test must use a temporary
directory and explicit Gemini-root isolation; never read or write real user
configuration as test data. New resident tests must explain the stable
behavioral fact they protect.

## Engineering Quality Policy

<!-- the-guard-policy:start -->
```yaml
schema: the-guard-policy/v1
rules:
  - id: guard.architecture-simplicity
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Every responsibility, business state, and write decision has one explicit owner.
    details: docs/standards/rules/architecture-simplicity.md
    checker: static-plus-review
  - id: guard.single-source-of-truth
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Each business rule, state, vocabulary, classification and filter has one executable owner.
    details: docs/standards/rules/single-source-of-truth.md
    checker: static-plus-review
  - id: guard.service-boundaries
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Modules perform only the responsibilities and decisions declared in their owner documentation.
    details: docs/standards/rules/service-boundaries.md
    checker: static-plus-review
  - id: guard.code-organization
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Modules, files, classes and methods have clear cohesive responsibilities and names.
    details: docs/standards/rules/code-organization.md
    checker: static-plus-review
  - id: guard.no-silent-fallback
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Never fabricate business facts or hide failures behind defaults, bypasses or recovery paths.
    details: docs/standards/rules/no-silent-fallback.md
    checker: static-plus-review
  - id: guard.agent-skill-prompt
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Skills, prompts and agent configuration reference authoritative services and structured results; they do not copy state machines, business vocabularies or classification logic.
    details: docs/standards/rules/agent-skill-prompt.md
    checker: static-plus-review
  - id: guard.test-case-quality
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Resident tests protect current core business facts and declare what their failure means.
    details: docs/standards/rules/test-case-quality.md
    checker: static-plus-review
```
<!-- the-guard-policy:end -->
