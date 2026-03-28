# Copilot Instructions

Instructions for GitHub Copilot and Copilot-powered agents working in this
repository.

## Coding Standards

Read and follow [`.github/CODING_GUIDELINES.md`](CODING_GUIDELINES.md) for
all code changes. Key points:

- **pyright strict** — every parameter and return value must be annotated.
- **No duck typing** — no `getattr`, `hasattr`, or runtime type interrogation
  in business logic. `isinstance` is allowed at I/O boundaries only.
- **No `assert` for validation** — assertions are not control flow.
- **Pydantic at the boundary**, frozen dataclasses internally.
- **loguru** for logging, not stdlib `logging`.
- **ruff** for linting and formatting — do not fight the formatter.

## Workflow Rules

- **Never push to `main`.** Always create a branch and open a PR.
- **Never merge without maintainer approval.**
- **Run `make check` before pushing.** It runs lint, typecheck, security, and
  tests — the same checks CI runs.

## Repository Layout

```
src/copilot_usage/   – Main package (CLI, parser, models, reports)
tests/               – Unit tests (mirrors src/ structure)
tests/e2e/           – End-to-end tests (excluded from unit run)
.github/workflows/   – CI and agent pipeline workflows
.github/agents/      – Agentic workflow agent definitions
```
