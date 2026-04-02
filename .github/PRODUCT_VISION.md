# Product Vision

## Goal

Provide instant, local-first visibility into GitHub Copilot CLI usage — the
data GitHub's usage dashboard doesn't show you (or shows with multi-day delays).

## Target Users

Developers and teams who use GitHub Copilot in the terminal and want to
understand their token consumption, premium-request spend, model breakdown, and
session activity without waiting for the GitHub billing dashboard to catch up.

## Non-Goals

- Replacing the GitHub billing dashboard — this tool reads **local** session
  data only.
- Managing Copilot subscriptions or quotas.
- Sending telemetry or usage data to any external service.
