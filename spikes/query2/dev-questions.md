# Dev Questions

The development half of the split question catalog. Together with
[`blind-questions.md`](./blind-questions.md) it forms the full catalog:
every audience has exactly four questions per tier — two here, two in the
blind set. This file is for development and iteration; the blind file is
held out for final evaluation so results can't be overfitted to questions
seen during development.

**The blind file is frozen during development.** If a class of question
needs fixing, fix it here and treat the blind half as unseen.

Answers and grading criteria live in [`dev-answers.md`](./dev-answers.md);
this file carries no hint of how a question is graded, what the right
answer is, or whether one exists.

**Register mix.** Real users don't know the conceptual model's vocabulary,
so ~80% of questions are written in natural register — everyday phrasing
("check", "duty", "what are we on the hook for") instead of schema terms
("Control", "Obligation"). The remaining ~20% deliberately keep the
canonical register as a baseline: most of Policy Manager's and Auditor's
questions (the power users who plausibly learn the tool's vocabulary) plus
one flagged canonical question per other audience. Register assignments per
question are recorded in the answers files.

## How the catalog is organized

**Audience-first.** Each question is assigned to the single audience that
most naturally owns it. Questions are never duplicated across audiences —
a question several audiences might plausibly ask is filed under its primary
owner.

**Tiered by difficulty for the asker**, not by any property of an answering
system:

| Tier | What it means |
|---|---|
| **Easy** | One well-defined fact the asker could find by opening a single known document or record — if they knew where to look. One correct value or a small fixed set. |
| **Medium** | Requires pulling together several facts, comparing, counting, or checking coverage — work a person *could* do, but that takes real effort across multiple sources. |
| **Hard** | Requires weighing many connected facts against each other, prioritizing, applying judgment, or honestly concluding the answer can't be determined from what is known. Scope ranges from a single situation (one article, one endpoint, one scenario) to the whole picture. No single correct answer. |

**IDs** follow `{AUDIENCE}-{TIER}{n}` (e.g. `LC-E1`, `SWE-H2`) and are
stable once assigned. Numbering is continuous with the sibling file: each
audience's tier contains four IDs total, split two/two across dev and
blind.

Audience abbreviations: `LC` Legal Counsel · `CO` Compliance Officer ·
`SA` Security Architect · `AU` Auditor · `RM` Risk Manager ·
`PM` Policy Manager · `SWE` Software Engineer · `SEC` Security Engineer ·
`EM` Engineering Manager

---

## Legal Counsel

### Easy

| # | Question |
|---|---|
| LC-E1 | "What's the text of CRA Article 13.1?" |
| LC-E2 | "What's the worst fine we could face under GDPR for getting the basic processing rules wrong?" |

### Medium

| # | Question |
|---|---|
| LC-M1 | "How many obligations does GDPR place on Data Processors vs. Data Controllers?" |
| LC-M2 | "We have to report both actively exploited vulnerabilities and severe incidents under the CRA — are the deadlines the same for both?" |

### Hard

| # | Question |
|---|---|
| LC-H1 | "Do CRA and NIS2 put duties on similar kinds of actors — is there something like a 'manufacturer' in both?" |
| LC-H2 | "An actively exploited vulnerability in our product turns out to be both a severe incident under the CRA and a significant incident under NIS2 — walk me through every notification we owe, to whom, and by when." |

---

## Compliance Officer

### Easy

| # | Question |
|---|---|
| CO-E1 | "Who are the different regulated parties under GDPR?" |
| CO-E3 | "Is there a minimum support period for products under the CRA, and how long is it?" |

### Medium

| # | Question |
|---|---|
| CO-M1 | "What obligations does the Manufacturer role carry under CRA?" |
| CO-M3 | "When we find out someone's actively exploiting a vulnerability in our product, what exactly do we have to report, to whom, and how fast?" |

### Hard

| # | Question |
|---|---|
| CO-H1 | "We process customer data and we ship a software product — which of GDPR, CRA, and NIS2 actually apply to us, and as what kind of actor under each?" |
| CO-H2 | "We found a vulnerability in an open-source component we bundle — is shipping our own fix enough, or does the CRA make us do more?" |

---

## Security Architect

### Easy

| # | Question |
|---|---|
| SA-E1 | "What capabilities does 'Maintain Security Logging' require?" |
| SA-E2 | "Which of our capabilities does CRA's unauthorised-access protection duty land on?" |

### Medium

| # | Question |
|---|---|
| SA-M1 | "Across CRA, NIS2, and GDPR — where do we need a security-logging-type capability?" |
| SA-M3 | "How many of our 68 capabilities are actually covered by an approved policy, as opposed to a draft or deprecated one?" |

### Hard

| # | Question |
|---|---|
| SA-H1 | "If we adopt a 'Software Bill of Materials' capability, which existing CRA/NIS2 obligations would it newly satisfy, and where are we already redundantly covered?" |
| SA-H2 | "If a single capability of ours fails, which failure endangers the most obligations — and is that even the right way to think about criticality?" |

---

## Auditor

### Easy

| # | Question |
|---|---|
| AU-E1 | "Which requirement does the 'Maintain Security Logging' obligation satisfy?" |
| AU-E3 | "What does our record of processing activities have to contain under GDPR?" |

### Medium

| # | Question |
|---|---|
| AU-M1 | "Trace the full path from CRA Art. 13.1 to whatever it ultimately requires us to be able to do." |
| AU-M2 | "Show every path from a GDPR requirement down to a Control that verifies it." |

### Hard

| # | Question |
|---|---|
| AU-H1 | "If an external auditor challenges our GDPR breach-notification compliance, what evidence trail do we have — and how much of it is actually current?" |
| AU-H2 | "Trace the CRA's actively-exploited-vulnerability reporting duty from the regulation text all the way into our internal governance — does the trail reach a check that's actually running?" |

---

## Risk Manager

### Easy

| # | Question |
|---|---|
| RM-E1 | "What security measures does NIS2 make essential and important entities implement, at minimum?" |
| RM-E2 | "When is an incident 'significant' and therefore reportable under NIS2?" |

### Medium

| # | Question |
|---|---|
| RM-M1 | "Which of our capabilities carry more than one regulatory duty?" |
| RM-M3 | "How concentrated is our compliance risk — how much of what we have to do rides on a few shared capabilities versus many single-use ones?" |

### Hard

| # | Question |
|---|---|
| RM-H1 | "Are we compliant with GDPR Article 32?" |
| RM-H2 | "If we benchmark our NIS2 Article 21 readiness against our GDPR Article 32 posture, where do we stand?" |

---

## Policy Manager

### Easy

| # | Question |
|---|---|
| PM-E1 | "What policy governs the 'Security Logging' capability?" |
| PM-E3 | "What's the status and version of the Clinical Data Integrity Policy?" |

### Medium

| # | Question |
|---|---|
| PM-M1 | "Which governed capabilities have zero implemented controls underneath, and why for each?" |
| PM-M2 | "Which of our policies have all their supporting standards in a current — implemented or reviewed — state?" |

### Hard

| # | Question |
|---|---|
| PM-H1 | "NIS2 was updated — which of our Policies are now potentially out of date?" |
| PM-H2 | "GDPR's rule that staff may only process data on instructions routes through a deprecated policy — what are my options, and the risk of each?" |

---

## Software Engineer

### Easy

| # | Question |
|---|---|
| SWE-E1 | "What's the implementation status of the Encryption-at-Rest control?" |
| SWE-E3 | "What does the CRA require of the software I ship — the essential security properties?" |

### Medium

| # | Question |
|---|---|
| SWE-M1 | "What does the CRA make me do about vulnerabilities in the third-party components I integrate?" |
| SWE-M2 | "What checks run under the Data Protection & Security Policy, and what's the status and next review date of each?" |

### Hard

| # | Question |
|---|---|
| SWE-H1 | "Is this new API endpoint, which logs access but doesn't encrypt data at rest, compliant with GDPR Article 32?" |
| SWE-H2 | "I'm building a new microservice that stores customer PII in a database — what compliance-related capabilities should I be thinking about?" |

---

## Security Engineer

### Easy

| # | Question |
|---|---|
| SEC-E1 | "Which checks exist under the Incident & Vulnerability Response Policy, and what state is each in?" |
| SEC-E2 | "Does NIS2 explicitly require multi-factor authentication?" |

### Medium

| # | Question |
|---|---|
| SEC-M1 | "Which capabilities have a policy on paper but no working check underneath?" |
| SEC-M3 | "How many regulatory duties across CRA, NIS2, and GDPR land on our access-control/MFA capability — and which regulation actually says 'multi-factor authentication'?" |

### Hard

| # | Question |
|---|---|
| SEC-H1 | "If an attacker exploited a missing MFA check today, which regulatory duties across CRA/NIS2/GDPR would we be in breach of?" |
| SEC-H3 | "If we sit on a known actively-exploited vulnerability past the CRA's reporting windows, which duties have we breached, and what's the fine exposure?" |

---

## Engineering Manager

### Easy

| # | Question |
|---|---|
| EM-E1 | "How many capabilities do we track in total, and how many have a governing policy?" |
| EM-E2 | "How many checks do we run, and what's the status breakdown?" |

### Medium

| # | Question |
|---|---|
| EM-M1 | "How many Controls are currently overdue for review?" |
| EM-M3 | "If the board asks 'what's the worst-case fine exposure across these three regulations?', what do I tell them?" |

### Hard

| # | Question |
|---|---|
| EM-H1 | "Which of our draft policies are blocking GDPR readiness?" |
| EM-H2 | "Give me a one-paragraph summary of our overall compliance posture I can bring to the board." |
