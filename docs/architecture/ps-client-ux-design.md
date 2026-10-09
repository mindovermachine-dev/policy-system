<!-- © 2026 Cartman ApS. All rights reserved. -->
# ps-client UX Design

**Status:** Draft
**Project Name:** Policy System
**Last Updated:** October 9th 2026

---

## 1. Purpose & Scope

This document is the technology-agnostic definition of **ps-client** — the Policy System's
native client — and the frame for choosing the technologies that realize it. It defines the
experience (§3), the use cases and v1 scope (§4), the contracts any implementation must
satisfy (§5), the non-functional constraints (§6), and the decision framework that turns
those into a technology selection (§7).

It deliberately makes **no technology choice**. Candidates, research notes, and the
conversation record that produced these decisions live in
`ps-native-client-exploration.md`; this document is the entry point going forward.

Individual use cases live in `ps-client-use-cases/` (one document per CUC); §4 indexes
them.

## 2. Product Context

ps-client is a **domain-specific agent harness**: the same class of software as a general
chat/coding agent client, specialized to PS compliance work. It is the **complete product
surface** — every role, admin included, works here. The aspiration is to retire ps-cli's
operator-facing commands once the client absorbs local-file intake; ps-cli's MCP-bridge
plumbing is not retired (it serves PS Question Skill in other hosts).

Two interactions dominate, in order:

1. **"Do we have a gap?"** — graph questions answered server-side with `source_ref`
   citations (CUC-08).
2. **"Help me close the gap"** — authoring Policy → Standard(s) → Control(s) against
   per-document rubrics until each clears its `pass_threshold` (CUC-09, CUC-10).

**Working assumption, to be tested, not argued:** most interaction does not need an
advanced model and could run on a reasonable local model, *provided the rubric verifier is
independent of the client model*. The model is a pluggable choice — a local model is an
additional option next to a hosted one (owning the experience is the driver, not cost or
privacy). The eval criteria are in §7.3 (SP-2).

## 3. User Experience Definition

### 3.1 Client surfaces

| Surface | Name | Role |
|---------|------|------|
| Auth gate | **Login Screen** | Shown when unauthenticated; passkey login via the OIDC IdP; disappears on authentication, returns on logout |
| Personal | **User Pane** | **Inbox**: work needing the user's decision or action. **Library**: the user's conversations and documents/reports. Plus identity and logout |
| Conversation | **Chat Pane** | The dialogue; bottom-up scroll; Socratic, one question at a time |
| Artifacts | **Artifact Pane** | The structured-artifact viewport: tabs of rubric-scored drafts, read-only reports, runs, comparisons |

### 3.2 Interaction principles

- **Chat is the instrument; the document is the state.** Structured content renders in the
  Artifact Pane; commentary stays in chat. Document state lives on the server — the model
  only issues tool calls, so a weak model cannot corrupt a document.
- **Scaffold against the blank page.** A document's full structure appears immediately,
  with a drafted starting point; the user reacts and refines rather than creates from
  nothing.
- **The pane is external memory.** Users cannot remember earlier answers; visible earlier
  sections let the user — not only the model — spot contradictions.
- **Visible verification.** Rubric scoring runs in the background and surfaces per section
  in the pane. Clicking a score badge renders the rubric as a table — each criterion,
  pass/partial/fail, a one-line reason — so the document explains itself without the chat.
- **Edits are implicit; progression is explicit.** Sections update live as discussion
  converges, with no per-edit ceremony. When a section passes, the AI says so and asks
  whether to move on. The user owns the pace.
- **Direct editing is first-class.** The user can type into the document; manual edits go
  through the same draft contract, are scored the same way, and are visible to the model.
  On a manual edit the AI acknowledges, re-scores, comments, follows the user's new focus
  — and returns to its bookmarked place when done. The user's typing always wins a
  collision.

### 3.3 Artifact model

Two artifact classes share one type-neutral contract: **read-only reports** (structured
answers to questions, citations intact) and **editable rubric-scored drafts**
(Policy/Standard/Control today; more rubric-driven document types later, with no client
changes). Long-running pipeline runs render as a third shape — a live **run artifact**
(per-stage progress, terminal summary, persisting as the run's record) — and
review/resolve flows as a **comparison artifact** (candidate pair side by side, effect
previewed explicitly). Artifacts accumulate as tabs; source context (e.g. the gap report
behind an authoring session) stays one click away. The contract declares per type whether
an artifact is editable and whether it is scored.

### 3.4 Conversations & resumption

- Every conversation persists, first-class; some are anchored to a document.
- **One continuous thread per document** — the conversation is part of the draft's record,
  a governance asset beside its audit trail.
- **Three converging entry points**: the Library's conversation list, the Library's
  document list, and an Inbox item. Each restores the same state — the discussion with its
  artifacts. Transcripts are server-held and follow the user across desktop and web.
- Drafts are durable: leave at any point, return days later, scores intact, chat recaps.

### 3.5 Approvals & lifecycle

- **Passkey ceremonies only at lifecycle boundaries** — propose, approve, delete, and
  similar irreversible or outward transitions. Draft-mode edits never trigger one.
- Lifecycle events are **deliberate user actions**: the controls live on the document in
  the Artifact Pane; chat may suggest readiness, the click is the user's. The signing
  summary binds to exactly what is signed — the tree, its final scores, a digest.
- Two passkey roles stay distinct behind one gesture: **login** passkeys live at the OIDC
  IdP; **transaction-signing** passkeys belong to PS Service's own WebAuthn relying party.
- **Notifications versus Inbox:** notifications say "something you were watching
  finished" (a run completing); the Inbox says "something needs your decision" (a
  near-miss, an assignment). Assignments have two sources — human-assigned and
  system-generated.

### 3.6 Reference walkthrough

CUC-09 (`ps-client-use-cases/cuc-09-author-policy-tree.md`) is the reference use case;
`ps-client-mockup/index.html` is its clickable mockup, covering CUC-08 → CUC-09
→ CUC-10 end to end.

## 4. Use Cases & v1 Scope

The use-case index in `ps-client-use-cases/README.md` is the scope ledger. v1 is **the
evaluator's journey** — a freshly installed system usable end to end through the client
alone: log in → invite users → assign roles → ingest or restore instruments → clean up →
identify gaps → author policies → propose (CUC-01..CUC-12, all v1).

Out of v1: amendment-sweep UX, applicability assessment, audit browsing, local-file intake
(the prerequisite for retiring ps-cli's operator commands), and ps-cli retirement itself.

Fourteen product skills group into five interaction classes — Q&A → report, authoring +
lifecycle, long-running runs, review/resolve, admin — and every class renders through the
§3.1 surfaces with no additional machinery.

## 5. Required Contracts

Every contract is technology-agnostic: a requirement on an interface, not a component
design. §7's evaluation criteria reference these ids.

### 5.1 Generic harness responsibilities

| ID | Responsibility |
|----|----------------|
| H-1 | **Tool loop** — the client owns the agent loop: model proposes tool calls, client executes against PS Service, results return to the model |
| H-2 | **Streaming** — token streaming into the Chat Pane; progressive artifact updates |
| H-3 | **Context management** — conversation history, artifact state, and recaps within model context limits |
| H-4 | **Permissions & approvals** — the elicitation-style approval surface (previews, acknowledgments) independent of model choice |
| H-5 | **Session persistence** — restore a conversation with its artifact tabs from server state |
| H-6 | **Model-provider abstraction** — one provider interface implemented by hosted and local models alike; the harness must not assume a hosted model |

### 5.2 PS-specific contracts

| ID | Contract | Requirement |
|----|----------|-------------|
| PSC-1 | Document read | Full content per document: every section/field, the tree, type-neutrally — enough to render the Artifact Pane without raw Cypher |
| PSC-2 | Document write | Field-level PATCH on a draft, owner-or-elevated, draft-status-gated; the single mutation path for chat-driven and manual edits alike |
| PSC-3 | Rubric-as-data | Per document type: section structure, criterion ids, weights, guidance anchors, `pass_threshold` — served, so new document types need no client change |
| PSC-4 | Scoring | Per-criterion results with one-line reasons plus the aggregate, re-evaluated on edit; scorecards persisted |
| PSC-5 | Document sync | An edit from any source (this client's chat, its pane, another session) becomes visible to the pane and the model without manual refresh |
| PSC-6 | Conversation persistence | Server-held threads; one accumulating thread per document; session ↔ artifact links; first-class unanchored conversations |
| PSC-7 | Notifications & Inbox | Work items with two creation sources (human-assigned, system-generated); completion notifications on watched conversations |
| PSC-8 | Run progress | Per-stage status for a submitted pipeline run, observable after the submitting conversation closes |
| PSC-9 | Auth flows | OIDC code+PKCE login against the IdP from desktop and browser; transaction-signing ceremonies against PS Service's own WebAuthn RP, embeddable in the client |
| PSC-10 | Artifact type registry | Per-type capability flags (editable? scored?) alongside PSC-3's structure |
| PSC-11 | File intake *(post-v1)* | Local intake documents (JSON, later PDF) submitted through the client — the ps-cli retirement prerequisite |
| PSC-12 | Caller's own grants | The authenticated caller can read their own AccessRole grants (any authenticated user, no admin role needed), so the client shapes affordances and the identity display up front instead of trying actions and rendering denials |

### 5.3 Server capability register

What PS Service provides today versus must grow. "Missing" entries are requirements on PS
Service regardless of which client technology is chosen.

| Contract | Today | Gap |
|----------|-------|-----|
| PSC-1 | **Partial** — `get-policy` returns the tree skeleton only (ids, titles, statuses); no content fields | Full-content read surface |
| PSC-2 | **Exists** — `update-policy-draft` / `update-standard-draft` / `update-control-draft`, allowlists derived from the domain schema | — |
| PSC-3 | **Missing as a surface** — rubrics are plugin markdown; the scoring model (stable criterion ids, weights, 0/1/2 anchors, thresholds) and the typed domain-schema registry are already type-neutral | Serve rubric + structure as data |
| PSC-4 | **Missing** — no server-side scoring exists; the authoring skill's model scores today, contradicting the objective-verifier premise | Scoring execution + persisted scorecards; location open (§8) |
| PSC-5 | **Missing** — no eventing/sync channel | New capability |
| PSC-6 | **Missing** — no transcript storage; conversations currently live in whatever host runs the skill | New capability |
| PSC-7 | **Missing** — no work-assignment model; lifecycle approval is role-gated, not assignment-gated | Domain design needed (§8, Q-12) |
| PSC-8 | **Partial** — Ingestion Runs persists status/result for polling | Subscribe/notify path; observe-after-close UX |
| PSC-9 | **Partial** — OIDC validation and the Passkey Signing RP (companion-browser pages) exist | Client-side flows: PKCE in a shell/browser, ceremony embedding (§7.3 SP-4/SP-5) |
| PSC-10 | **Missing** — trivially derivable once PSC-3 exists | With PSC-3 |
| PSC-11 | **Exists in ps-cli** — the local-file commands that stayed behind in the sunset | Client absorption, post-v1 |
| PSC-12 | **Missing** — `list-access-roles` is SystemAdmin/SystemOwner-gated; no self-read exists | Self-scoped grants read |

## 6. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | **Platforms:** cross-platform desktop and web; macOS desktop first. The same UX definition applies to both |
| NFR-2 | **Responsiveness:** token streaming without perceptible stall; section re-score feedback fast enough to keep the edit-score loop conversational (no number committed — load-test before relying on one, per the solution architecture's SLA posture) |
| NFR-3 | **Degraded states fail closed and visibly:** an unreachable scoring or document surface greys the affected badges/sections rather than showing stale state as current; consistent with PS Service's fail-closed culture |
| NFR-4 | **Accessibility:** the Artifact Pane's state (badges, criteria tables) must be readable without color alone; keyboard navigation across panes |
| NFR-5 | **Licensing:** every embedded dependency's license reviewed before adoption (the EUPL-1.2 copyleft note on NobodyWho is the standing example) |
| NFR-6 | **Single-tenant posture preserved:** the client adds no multi-tenant assumptions; all data access through PS Service's authenticated surfaces — never direct data-store access |

## 7. Technology Determination Framework

### 7.1 Decision dimensions

| ID | Dimension | The question |
|----|-----------|--------------|
| TD-1 | Harness core | Build, adopt, or fork an agent-harness core (candidates and research in the exploration doc §7) |
| TD-2 | UI shell & packaging | Web tech in a desktop shell versus native, for macOS first with web to follow |
| TD-3 | Local inference engine | Which engine serves the local-model option, and whether one engine serves desktop and web |
| TD-4 | Browser-runnable vs WASM core | Is the requirement "runs in a browser" (a TypeScript harness satisfies it) or "compiled to WASM" (implies a non-JS core)? Decide before TD-1 |
| TD-5 | Client-server topology | **Decided for auth (CUC-01): backend-for-frontend on web** — the IETF BCP *OAuth 2.0 for Browser-Based Applications* BFF pattern, tokens never in page JavaScript; desktop uses RFC 8252 (system browser, OS keychain). On web the BFF carries all PS Service traffic (REST, MCP, sync) and is a separate deployable beside PS Service built on an off-the-shelf OIDC proxy, serving the client's static files under one host (signing-RP origin, SP-4); desktop calls PS Service directly. Open: spike confirmation (SP-3, SP-5) |

### 7.2 Evaluation criteria

Each criterion is traceable to a contract or NFR; a candidate that cannot satisfy a
**gate** criterion is out regardless of other merits.

| Criterion | Traces to | Gate? |
|-----------|-----------|-------|
| Implements or cleanly hosts the tool loop, streaming, context management | H-1..H-3 | gate |
| Provider interface accepts a local model, not only hosted APIs | H-6, §2 assumption | gate |
| Runs in the chosen TD-4 answer's environment (browser page or WASM runtime) | NFR-1 | gate |
| Can render the two-pane layout with live artifact updates | §3.2, PSC-5 | gate |
| Supports the PSC-9 auth flows (PKCE; embeddable signing ceremony) | PSC-9 | gate |
| License compatible | NFR-5 | gate |
| Extension model fits PS-specific surfaces (panes, approvals) without forking | §3.1, H-4 | weighted |
| Maturity / churn risk (breaking-change cadence, maintainer base) | — | weighted |
| Effort to v1 (the evaluator journey, CUC-01..12) | §4 | weighted |
| Local-model quality on dominant tasks at acceptable speed/size | §2, SP-2 | weighted (informs TD-3 only — the hosted path must work regardless) |

### 7.3 Evidence plan

| ID | Spike | Answers | Pass signal |
|----|-------|---------|-------------|
| SP-1 | Bundle the leading harness-core candidate into a bare page; call a hosted model; drive one PS MCP tool end to end | TD-1, TD-4, TD-5 | Tool round-trip from a browser page with streaming |
| SP-2 | Local-model eval: small model routes gap questions to the right tool with correct arguments; `source_ref` survives phrasing; rubric-iteration count to `pass_threshold` versus hosted Claude on identical inputs | TD-3, §2 assumption | Measured deltas small enough that the §2 assumption survives |
| SP-3 | Document-sync feasibility: a manual edit in one surface appears in another session's pane and model context | PSC-5, TD-5 | Sub-second propagation without polling the full document |
| SP-4 | Passkey signing ceremony inside the candidate shell/webview against PS Service's RP | PSC-9, TD-2 | Ceremony completes without leaving the client |
| SP-5 | OIDC code+PKCE login from the candidate shell and from a plain browser page | PSC-9, TD-2, TD-5 | Login completes on both; token lands in the client securely |

### 7.4 Decision gates

1. **TD-4 first** — browser-runnable vs WASM bounds the TD-1 candidate set; decide on
   argument plus SP-1.
2. **TD-1 and TD-5 together** — SP-1/SP-3/SP-5 give the evidence; BFF is now
   decided for web auth (CUC-01), which changes the harness's placement.
3. **TD-2** — after TD-1; SP-4/SP-5 must pass inside the chosen shell.
4. **TD-3 last** — SP-2's numbers; the hosted-model path must already work, so this
   dimension can close late (or provisionally) without blocking v1.

A dimension closes only when its named evidence exists; "we like it" closes nothing.

## 8. Open Questions Carried Forward

| ID | Question |
|----|----------|
| Q-10 | A colleague with access opens a draft that has an authoring thread: join it, read-only, or something else? Multi-user thread semantics are undesigned |
| Q-11 | Does a document's thread freeze when the document leaves draft? What retention/compaction applies, given the conversation is part of a governance record (GDPR included)? |
| Q-12 | The work-assignment model behind the Inbox (PSC-7): work-item states, assigner/assignee, two creation sources, relation to AccessRoles and no-self-approval, notification |
| Q-13 | Scoring location (PSC-4): a server-side scoring component (restores the objective-verifier premise, keeps weak client models honest) versus client-model scoring with server-persisted scorecards |

---

**End of Document**
