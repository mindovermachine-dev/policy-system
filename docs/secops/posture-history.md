# Posture History

The rolling posture, one row per completed run. Drives the trend arrow and the triage signal.
**Appended by the `ps-pentest` skill after each run.** No runs yet.

## Legend

- **Posture A** — primary score (Scenario A, unauth/external). Start `100`, minus per open finding by
  severity (`critical −20, high −10, medium −4, low −1, info −0.5`), floored at `0`.
- **Authz-integrity B** — sub-signal (Scenario B): how well a low-priv token is confined.
- **Trend** — vs the *previous* run: `▲ improved` · `▪ held` · `▼ regressed`.
- **Triage** — `needs attention` iff any of: (1) new critical/high · (2) regression · (3) coverage
  shrank/phase skipped. A triage flag is a **prompt for human review**, not a pass/fail.
- **Open counts** — open findings by severity at this run.

## History

| Run | Date | Posture A | Trend | Trend arrow | Authz B | Open (C/H/M/L/I) | Triage | Notes |
|-----|------|-----------|-------|-------------|---------|------------------|--------|-------|
| — | — | — | — | — | — | — | — | no runs yet; first run seeds this table |

> Each new run appends a row here and updates `baseline.yaml` (`findings`, `runs`). A report's
> per-run detail lives in the `run-report-template.md` shape; this table is the cross-run view.
