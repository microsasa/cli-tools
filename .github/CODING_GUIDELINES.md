# Coding Guidelines

Standards for all contributors — human and AI — to the `copilot-usage` CLI.

## Type Safety

### Strict Static Typing

- **pyright `strict` mode is mandatory.** Every function parameter and return
  value must have an explicit type annotation.
- Use `str | None` union syntax (PEP 604), not `Optional[str]`.
- Use `Final` for module-level constants that should never be reassigned.
- Use `Literal["a", "b"]` for constrained string values — not bare `str`.

### No Duck Typing

- Do not rely on implicit structural compatibility.
- If you need a shared interface, define a `Protocol` with explicit method
  signatures.

### No Runtime Type Interrogation

- **No `getattr` / `hasattr`.** Access attributes directly through typed
  references.
- **No `isinstance` checks in business logic.** Use static typing and data
  models instead.  `isinstance` is allowed at I/O boundaries when coercing
  untyped external data (e.g., JSON values) into typed models.
- **No `assert` for type validation.** Assertions are stripped in optimised
  builds and are not a control-flow mechanism.

## Data Modelling

### Pydantic at the Boundary, Plain Python Internally

- External data (JSON files, API responses) is validated with
  **Pydantic** models.  CLI arguments are validated by **Click** at the
  boundary.
- Internal intermediate state uses **frozen `dataclasses`** with `slots=True`.
- Prefer `dataclasses.dataclass(frozen=True, slots=True)` for immutable value
  objects that never touch I/O.

### Defaults and Factories

- In `dataclasses.field`, use `default_factory=lambda: []` (not
  `default_factory=list`) for mutable defaults — this avoids a known pyright
  false-positive.
- In Pydantic `Field`, `default_factory=list` is fine — Pydantic handles
  the typing correctly.

## Naming and Structure

### Module-Level Organisation

- Private helpers that serve a single public function live in the same module,
  prefixed with `_`.
- When a module grows beyond ~250 lines or serves multiple public consumers,
  extract a `_<name>.py` private module.

### Import Conventions

- Standard library → third-party → local, separated by blank lines (enforced
  by `ruff` isort rules).
- **`TYPE_CHECKING` is banned.**  Do not use `from typing import
  TYPE_CHECKING` or `if TYPE_CHECKING:` guards.  Every import used in an
  annotation must be a real runtime import.  Circular imports are a design
  bug — fix the module graph, do not hide cycles behind `TYPE_CHECKING`.

## Error Handling

- Catch **specific** exception types. Never use bare `except:` or
  `except Exception:` unless re-raising.
- **Exception:** Top-level event loops (e.g., TUI render loops) may catch
  `Exception` without re-raising when crash recovery is intentional, provided
  `KeyboardInterrupt` is handled separately.
- Prefer early returns to reduce nesting.

## Logging

- Use **loguru**, not stdlib `logging`.
- Log at `warning` for recoverable problems, `error` for failures that affect
  output, `debug` for developer-facing tracing.

## Testing

- Tests use **pytest** with `--strict-markers`.
- Unit tests live in `tests/` and mirror the `src/` structure.
- E2E tests live in `tests/e2e/` and are excluded from the default unit run.
- Doctests are collected from `src/` via `--doctest-modules`.
- `assert` is fine inside tests (ruff rule `S101` is disabled for `tests/`).

## Security

- `flake8-bandit` (`S` rules) is enabled in ruff. Subprocess calls must use
  absolute paths (resolved via `shutil.which`).
- Hardcoded credentials are allowed only in test fixtures (`S105`/`S106`
  suppressed for `tests/`).

## Formatting

- **ruff format** with double quotes, 88-char line length, space indentation.
- Do not fight the formatter — let it own all whitespace decisions.

## Defensive Programming

- Guard clauses at the top of helper functions are acceptable even if currently
  unreachable, as long as they make the function self-contained and safe to
  call from future call sites.

## Verification Before Merge

- Run `make check` (lint → typecheck → security → test) locally before
  pushing. CI runs these checks plus diff-coverage on changed lines.
- All existing tests must pass. Coverage must remain ≥ 80 %.
