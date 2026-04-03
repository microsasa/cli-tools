# Product Vision

## Goal

Give GitHub Copilot CLI users **instant, local visibility** into their token
usage, premium-request consumption, and per-model cost breakdown — data that
GitHub's usage dashboard either doesn't show or reports with multi-day delays.

## Target Users

- **Individual developers** who use the Copilot CLI daily and want to
  understand how their quota is being consumed across sessions and models.
- **Power users** running long-lived or resumed sessions who need accurate,
  real-time stats without waiting for the billing dashboard to catch up.

## Core Principles

1. **Local-first** — parse `~/.copilot/session-state/` files directly; never
   depend on a remote API for core functionality.
2. **Accuracy over estimation** — prefer exact metrics from `session.shutdown`
   events; fall back to per-message estimation only for active sessions.
3. **Zero configuration** — work out-of-the-box with sensible defaults; all
   options are optional overrides.
4. **Fast feedback loop** — results appear in < 1 s for typical workloads;
   caching avoids redundant parsing.

## Non-Goals

- Billing or payment integration — this tool reports raw counts, not invoices.
- Remote data collection — no telemetry is sent anywhere.
- Editor plugin — the CLI is the interface; VS Code log parsing is a
  convenience add-on, not the primary path.
