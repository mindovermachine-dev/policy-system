# Backlog Triage Policy

Status: **FINAL — decisions D1–D11 made 2026-10-15.** Rationale for each rule is in the [Decision log](#decision-log).

Purpose: a lean, unambiguous policy for triaging and ordering the GitHub issue backlog, so that any triager (human or agent) applying it to the same issue arrives at the same classification and ordering. Written as input for the policy system; not yet encoded in ps.

## 1. Classification

Every issue receives three values: a **Group**, a **Priority (P)** and a **Severity (S)**.

### 1.1 Groups (order of work)

**Group 1 — Prod-Impact (label `1-Prod-Impact`).** Any issue that causes, or would cause, an **S0–S2** impact for a customer in production. This includes preventive work (e.g. a missing alert, or a known risk that would lead to an S0–S2 impact). S3 production issues are not Group 1; they are Group 2b hygiene.

**Group 1 takes precedence:** if an issue meets the Group 1 definition, it is Group 1, regardless of what else it is (toil, debt, feature, docs …).

**Group 2 — Repeating manual work.** Anything that requires manual intervention on a recurring basis. Two subgroups:

- **2a — Internal development work** (label `2a-Toil-Debt-Hygiene`).
- **2b — Customer operations work** (label `2b-CustOps-Hygiene`).

Group 2 also includes technical debt, cleanup and hygiene, which must never be allowed to accumulate while time is spent on feature work. Customer-visible hygiene (e.g. cosmetic production bugs, customer-facing docs) is 2b; internal hygiene is 2a.

**Group 3 — Backlog features (label `3-Backlog-Feature`).** New functionality not required by any Group 1 or Group 2 issue.

**Feature promotion.** A feature that is required to enable a Group 1 or Group 2 issue is promoted (this is the normal rule, not an exception, and needs no approval):

1. The feature issue must reference the issue it enables (e.g. "Enables #123"). No link, no promotion.
2. It inherits the Group and P/S of the enabled issue; if it enables several, it takes the highest-ranked one.
3. If the enabled issue is closed by other means or re-triaged lower, the feature is re-triaged too, falling back to Group 3 if nothing still depends on it.

### 1.2 Priority — breadth (how many customers are affected)

| Level | Definition |
|-------|------------|
| P0 | All customers |
| P1 | Most customers (roughly more than half) |
| P2 | A distinct subset, e.g. customers using a specific feature, configuration or integration |
| P3 | One or a handful of customers |

For preventive work, Priority counts the customers who **would be** affected if the risk materialised.

### 1.3 Severity — depth (how bad it is for an affected customer)

| Level | Definition |
|-------|------------|
| S0 | Data loss, data corruption, incorrect results or calculations |
| S1 | Customer cannot use the system or a core function; no workaround |
| S2 | Function impaired or degraded; a workaround exists or the impact is contained |
| S3 | Cosmetic or negligible |

### 1.4 Scales for Group 2a (internal development work)

Internal toil and technical debt do not affect customers directly, so for Group 2a the development team is treated as the "customer". The labels are the same; the meanings are:

| Level | Priority (2a) — how often it hurts |
|-------|------------------------------------|
| P0 | Every change: hits every build, PR or developer |
| P1 | Weekly |
| P2 | Per release or monthly |
| P3 | Rarely |

| Level | Severity (2a) — how bad each occurrence is |
|-------|--------------------------------------------|
| S0 | Corrupts or loses internal data or environments (e.g. dev/test data, CI state). A risk that reaches production is Group 1, not 2a |
| S1 | Blocks development work; no workaround |
| S2 | Slows work; a workaround exists |
| S3 | Annoyance or negligible |

Group 2b (customer operations) uses the customer-based scales in 1.2 and 1.3: P = how many customers' operations need the manual intervention; S = how bad it is when the intervention is late or done wrong.

## 2. Ordering

1. Work on Group 1 issues first, and continue until no Group 1 issues remain, or every remaining one is pending or blocked for a good reason.
2. Then work on Group 2 issues, under the same rule. Subgroups 2a and 2b form **one combined list**, ordered by the rules in step 4; when a 2a and a 2b issue rank equal, the 2b (customer operations) issue goes first.
3. Only when Groups 1 and 2 are exhausted (or all remaining items are legitimately pending/blocked) is Group 3 work taken on.
4. Within each group, Priority and Severity define the order in which issues are processed:
   - Issues that are **both** S0/S1 **and** P0/P1 (severe and widespread) always come first.
   - Inside that top band, and among all remaining issues, order by Severity first (S0 before S1 …), then by Priority (P0 before P1 …).
   - Example order: P1/S0 → P0/S1 (top band), then P3/S0 → P2/S1 → P0/S2.

## 3. Keeping debt from accumulating

1. Strict group ordering (section 2) is the enforcement mechanism: no feature work starts while unblocked Group 1 or Group 2 work exists. There is no capacity split between groups.
2. Technical debt knowingly introduced while building a feature is filed as a Group 2 issue at the time, before that feature is considered done.
3. If Group 3 is starved for a prolonged period, that is a signal that toil or debt is being created faster than it is removed. It is raised for review, not worked around.

## 4. Feature override (exception path)

Feature work may start ahead of open Group 1/2 work only through a recorded override, so the signal stays visible.

1. **Approver:** the primary maintainer. No one else can approve an override.
2. **Record:** on the feature issue itself:
   - add the `override` label;
   - add a comment stating the reason (why it cannot wait), the approver, and the open Group 1/2 issues being deferred (by number).
3. **Signal:** the `override` label filter lists every exception. It is reviewed together with feature starvation (section 3); frequent overrides mean the same as starvation: toil is outpacing capacity.

## 5. Triage process

1. **At creation:** whoever creates an issue sets its Group, P and S. The primary maintainer may correct them at any time.
2. **Untriaged issues:** an issue missing any of the three values carries `needs-triage` and must not be picked up. Before choosing the next issue to work on, triage all `needs-triage` issues first, so untriaged work cannot hide a Group 1 problem.
3. **Re-triage** when:
   - new information changes breadth or severity (more customers report it, a workaround is found or lost);
   - an issue it enables or depends on changes (see feature promotion);
   - it is unblocked after being blocked.
4. **Blocked / pending:** only for a good reason, recorded with the `blocked` label and a comment stating what it is waiting on. Good reasons:
   - waiting on an external party (customer, vendor, upstream project);
   - waiting on another issue (which must be linked);
   - waiting for a scheduled window (release, maintenance slot).

   Not good reasons: being busy, preferring other work, or the issue being unclear. An unclear issue goes back to `needs-triage`.
5. **Review:** no fixed cadence. The primary maintainer reviews starvation (section 3) and the `override` list (section 4) when Group 3 has visibly stalled or overrides accumulate.

## 6. GitHub representation

1. **Type** is carried by the issue title's conventional-commit prefix, as already required by [CONTRIBUTING.md](../../CONTRIBUTING.md) (`feat`, `fix`, `perf`, `refactor`, `docs`, `chore`, `ci`, `test`, `style`, `build`; `!` for breaking). It drives the semver bump and is the authoritative type. The organization-standard type labels (`bug`, `enhancement`, `documentation`) may also be used; they have no effect on ordering.
2. **Type and Group are independent.** A `feat:` can be Group 1 or 2 through promotion; a `refactor:` is usually Group 2a debt but is triaged like anything else; a `fix:` for an S3 cosmetic issue is Group 2b hygiene.
3. **Classification labels** (each issue carries exactly one of each):

   | Kind | Labels |
   |------|--------|
   | Group | `1-Prod-Impact`, `2a-Toil-Debt-Hygiene`, `2b-CustOps-Hygiene`, `3-Backlog-Feature` |
   | Priority | `P0`, `P1`, `P2`, `P3` |
   | Severity | `S0`, `S1`, `S2`, `S3` |

4. **Workflow labels:** `needs-triage`, `blocked`, `override`.
5. **Organization-standard labels** (status, type or call for help; no effect on ordering): `duplicate`, `invalid`, `wontfix`, `question`, `good first issue`, `help wanted`, `development`, `ad hoc`, `bug`, `enhancement`, `documentation`.
6. **Rollout:** all open issues receive `needs-triage` and are triaged per section 5.

## 7. Edge categories

1. **Security vulnerabilities** are scored on potential impact like any customer issue: exploitable exposure or alteration of data is S0; a theoretical issue with no practical exploit path may be S2/S3. "Would cause" in the Group 1 definition covers exploitability.
2. **Documentation and tests:** missing or wrong docs that lead customers to incorrect results or failed operations are scored on customer impact (possibly Group 1). Otherwise docs and test gaps are Group 2 hygiene (2b if customer-facing or causing customer-ops toil, otherwise 2a).
3. **Dependencies:** a CVE that affects us is treated as a security vulnerability. Routine upgrades are Group 2a hygiene.
4. **Questions, spikes, investigations** inherit the Group of the problem they investigate (linked). A spike with no linked problem is Group 3.
5. **Unused or pre-release functionality:** P3 (almost no customers affected); Severity is scored normally.

## Decision log

Decisions in the order they were made; the policy sections above reflect all of them.

- **D1a — Group is the strict primary order (decided 2026-10-15).** Groups are drained in order 1 → 2 → 3. A higher group is only left while all of its remaining issues are pending or blocked for a good reason. P/S never move an issue across groups; they only order issues within a group. ("Good reason" for pending/blocked to be defined under D8.)
- **D1b — Top band within a group (decided 2026-10-15).** Within a group, issues that are both S0/S1 and P0/P1 (severe *and* widespread) always come first.
- **D1c — Severity before Priority (decided 2026-10-15).** Inside the top band and among the remaining issues, sort by Severity first, Priority as tie-breaker. How bad it is outranks how many it hits.
- **D2 — Group 1 defined by Severity (decided 2026-10-15).** "High or medium criticality" is replaced by S0–S2 in production (actual or prevented). S3 production issues go to Group 2 hygiene (2b, per D11). Relies on the S1/S2 definitions in D3.
- **D3 — Middle levels defined (decided 2026-10-15).** S1 = blocked, no workaround; S2 = degraded, workaround exists. The S1/S2 test is "is there a reasonable workaround?". Priority uses qualitative bands with an approximate share (P1 ≈ more than half; P2 = a distinct subset), not absolute counts, so it does not go stale as the customer base grows. Preventive work is scored on customers who would be affected.
- **D4 — Separate scales for Group 2a (decided 2026-10-15).** For internal work the dev team is the "customer": P = frequency (every change → rarely), S = impact per occurrence (internal data/environment corruption → annoyance; see D11 for the S0 revision). Same labels, so the D1 ordering rules apply unchanged. Group 2b keeps the customer-based scales.
- **D5 — Group 2 is one combined list (decided 2026-10-15).** 2a and 2b are ordered together by the D1 rules; 2b wins ties. Rejected: "2b always before 2a", because a serious internal issue (e.g. one that corrupts CI state or blocks all development) would then wait behind every minor customer-ops chore.
- **D6a — Strict ordering is the debt enforcement (decided 2026-10-15).** No capacity split (fixed share of time per group): it would contradict D1a, add bookkeeping and hide the starvation signal. Debt created during feature work is filed as Group 2 when created. Prolonged feature starvation is accepted and treated as a signal for review.
- **D6b — Override path exists (decided 2026-10-15).** Feature work can be escalated ahead of Group 1/2 work, but every override must be recorded so the signal remains clear. Mechanics still open.
- **D6c — Override mechanics (decided 2026-10-15).** Only the primary maintainer approves. Recorded on the feature issue with the `override` label and a comment giving reason, approver and the deferred Group 1/2 issue numbers. Overrides are reviewed alongside feature starvation.
- **D7 — Feature promotion (decided 2026-10-15).** A feature that enables a Group 1/2 issue must link it, inherits its Group and P/S (highest-ranked if several), and is re-triaged when that issue changes. Promotion needs no approval: the link is the justification. Overrides (D6) remain the only path needing maintainer sign-off.
- **D8 — Minimal triage process (decided 2026-10-15).** Creator triages at creation; maintainer may correct. Untriaged issues (`needs-triage`) cannot be picked up and are triaged before the next pick. Re-triage on changed breadth/severity, changed dependency, or unblocking. Valid blocked reasons: external party, linked issue, scheduled window; recorded with `blocked` + comment. Unclear issues go back to `needs-triage`. Starvation/override review happens when noticed, no fixed cadence. Closes the "good reason" question from D1a.
- **D9 — GitHub representation (decided 2026-10-15).** Type stays in the conventional-commit title prefix (single source of truth, already drives semver; `refactor` is already a type). Type and Group are independent. Labels: group/P/S (one each) plus `needs-triage`, `blocked`, `override`; the organization-standard labels are kept. Group labels are named `1-Prod-Impact`, `2a-Toil-Debt-Hygiene`, `2b-CustOps-Hygiene`, `3-Backlog-Feature` (number first so they sort in work order; renamed from `group:N` in #334). Existing open issues get `needs-triage` at rollout.
- **D10 — Edge categories (decided 2026-10-15).** No special rules, only guidance: security and affecting CVEs scored on potential impact; docs/tests scored on customer impact, otherwise Group 2 hygiene; routine dependency upgrades are 2a; spikes inherit the group of their linked problem (else Group 3); unused/pre-release functionality is P3.
- **D11 — Final consistency fixes (decided 2026-10-15).** (a) Group 1 takes precedence over every other classification. (b) 2a S0 redefined as corruption/loss of internal data or environments; any risk reaching production is Group 1. Resolves the overlap between D2 and D4. (c) S3 production issues and other customer-visible hygiene go to 2b (customer scales); internal hygiene to 2a.
