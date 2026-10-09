<!-- © 2026 Cartman ApS. All rights reserved. -->
# Policy System — Native Client Exploration (ps-client)

**Status:** Superseded as entry point by `ps-client-ux-design.md`, which distills the
decisions here (D-1..D-16, walkthrough, scope, Q-2 findings) into the technology-agnostic
design and decision framework. This document remains the conversation record of
2026-10-08/09 — candidate research (sections 6–7) and decision rationale live only here.
Statements marked **[UNVERIFIED]** come from prior knowledge or a single web search and
must be checked before being relied on.

---

## 1. Intent and Objective

Build a **PS-native client** that PS owns from the user interface through to the backend. It is
a domain-specific **agent harness**: the same class of software as Claude Desktop (chat/cowork
are domain-neutral, Code is code-specific), but specialised to PS compliance work.

- The experience is **chat in one pane, the scaffolded document in another**. The user converses
  while the draft Policy / Standard / Control appears and is edited live beside the chat.
- More **rubric-driven document types** will be added later and must use the same
  scaffold-and-edit pattern. This is the main reason a native client is wanted.
- The model is a **pluggable choice**. A local model is an *additional option* next to a hosted
  model (Claude), not a replacement. NobodyWho is the candidate local-inference engine.
- **Ingestion is unchanged.** The existing pipeline (Ingestion -> Domain Mapper -> Company Merge)
  stays as is. This work concerns the interactive client only.

### Why a local model is wanted

**Owning the experience** — no dependency on a third-party model provider for the interactive
path. Not cost, privacy or offline use as primary drivers (stated explicitly by the user).

### Target platforms

Cross-platform desktop **and** web. **Start with macOS.**

The user would prefer a **WASM harness** so the client can run natively in browsers.

---

## 2. Dominant Interactions

Two interactions dominate, in this order:

1. **"Do we have a gap?"** — the `ps-qna` skill. Most questions put to it are about identifying
   gaps. Mostly a graph question (e.g. a Capability with Obligations but no governing Policy),
   answered by server-side Cypher and the server's falsification-verified confidence. The model
   mainly routes the question to the right tool and phrases the result with `source_ref`
   citations intact.
2. **"Help me close the gap"** — the `ps-author-policy` skill. Once gaps are identified there
   is heavy use of authoring: Policy -> Standard(s) -> Control(s), one rubric criterion at a
   time until each document's own rubric-weighted score clears its `pass_threshold`.

Gap detection is the easier job for a small model. Gap closing is the harder one (drafting
quality text), but the server-side rubric score is an **objective verifier**, so a weaker model's
output is measurable and can be iterated against.

---

## 3. Working Assumption To Be Tested

> Most user interaction does not need an advanced model and could run on a reasonable local model.

This is an **assumption, not a fact**. It is to be tested with an eval, not argued:

- share of gap questions routed to the right tool with correct arguments
- whether `source_ref` citations survive intact in the phrased answer
- for authoring, the number of rubric iterations a drafted Policy / Standard / Control needs to
  reach `pass_threshold`, compared with hosted Claude on the same inputs

---

## 4. Direction Chosen: UX First, Technology After

The initial framing (start from NobodyWho) was reversed. Technology constraints chosen before the
experience is known are guesses. Order of work:

1. **Define the UX through concrete walkthroughs** — "Do we have a gap?" followed by "Help me
   close it", including how the document pane updates, how approvals appear, and manual edit
   versus edit-by-chat. Mock these up.
2. **Derive harness responsibilities and PS-specific contracts** from the walkthroughs.
3. **Evaluate harness options** (build / adopt / fork) against that list, with the local-model
   evaluation as one criterion.

### Generic harness responsibilities (domain-neutral)

Tool loop, streaming, context management, permissions and approvals, session persistence,
model-provider abstraction.

### What is PS-specific

Chat + live-document layout, rubric-driven document scaffolding, PS-specific approvals
(passkey-signed actions), server-held draft state, and server-held conversation records —
transcripts are not client-local; a document's authoring thread is part of its record
(D-9..D-11).

---

## 5. Design Considerations Identified (Not Decided)

| ID | Consideration |
|----|---------------|
| C-1 | **Document state lives on the server.** PS Service already holds drafts and exposes tools such as `add-standard-to-draft`, `update-control-draft`, `get-policy`. If the document pane renders from server state rather than model output, a weak model cannot corrupt the document — it can only issue tool calls. Probably the most important design choice. |
| C-2 | **Type-neutral rubric-driven document contract.** New document types must render, edit and score without client changes. Depends on whether the server exposes the rubric and score per document in a type-neutral way. Not yet checked. |
| C-3 | **Model-provider interface.** The agent loop is owned by the client, so one provider interface must be implemented by both hosted and local models. The harness must not assume a hosted model. |
| C-4 | **Auth and approvals.** PS Service passkey flows and elicitation-style approvals apply independently of the model choice. Two passkey roles must stay distinct: login passkeys at the OIDC IdP (Authentik) gate the client's Login Screen; transaction-signing passkeys at PS Service's own WebAuthn relying party sign lifecycle ceremonies (D-2). |
| C-5 | **Consistency with the ps-cli decision.** The ps-cli is REST-only and never controls container lifecycle; some commands moved onto MCP skills. A client that drives the same tools must stay consistent with that. |
| C-6 | **Constrained output.** NobodyWho builds on llguidance (structured output), which may help tool arguments come out well-formed. **[UNVERIFIED]** |

---

## 6. NobodyWho — What Is Known

**Not a client SDK.** It is an on-device inference engine, not a library for talking to a server.
A client must be built on top of it (local model calling PS Service as tools).

- Org: <https://github.com/nobodywho-ooo> (28 repos; first 10 seen).
- Main repo: <https://github.com/nobodywho-ooo/nobodywho> — Rust inference engine on llama.cpp.
- Related repos: `nobodywho-swift`, `swift-starter-example`, `flutter-starter-example`,
  `NobodyWho-Chat` (React Native), forks of `llama.cpp`, `llama-cpp-rs`, `llguidance`.
- Docs: <https://docs.nobodywho.ooo>. Listed bindings: Swift, Python, React Native, Flutter,
  Godot (sources disagree on the exact list; Kotlin also appears in a third-party index).
- Listed capabilities: tool calling, token streaming, embeddings, text-to-speech, structured
  output. **[UNVERIFIED]** — from a single web search, not from reading the code or docs.

### Browser feasibility — researched 2026-10-08 (desktop reading of READMEs, no code run)

**NobodyWho has no browser build.** The repo README states there is no web export, tracked by
[issue #111](https://github.com/nobodywho-ooo/nobodywho/issues/111) (opened 2025-02-18 as a
Godot HTML5 request; still open, no assignee, no linked PR, no maintainer reply). Listed
targets: Kotlin, Swift, React Native/Expo, Flutter, Python, Godot. No MCP support is
mentioned; tool calling is (grammar generated from function signatures). Licence is
**EUPL-1.2** (copyleft on modified versions of NobodyWho itself; use in proprietary projects is
permitted) — to be reviewed by whoever owns licensing before any embedding.

Consequence: NobodyWho cannot be the local-model option for the web client. For a web-tech
desktop shell it would need a sidecar process.

**Browser-capable local inference engines exist** (second-hand; none has been run by us):

| Engine | Basis | Notes |
|--------|-------|-------|
| [wllama](https://github.com/ngxson/wllama) | llama.cpp compiled to WASM, MIT | WebGPU offload in v3.1 (Firefox degraded); runs in a worker; 2 GB ArrayBuffer cap, so split GGUF into chunks of at most 512 MB; multithreading needs COOP/COEP headers; tool calling listed as a v3 feature; grammar support not mentioned |
| WebLLM (MLC) | WebGPU, not llama.cpp | OpenAI-compatible API, web-worker support; needs a modern WebGPU browser |
| Transformers.js (Hugging Face) | JS reimplementation, WebGPU | Pipelines for embeddings, speech, vision |
| llama-cpp-wasm (tangledgroup) | llama.cpp to WASM | Single- and multi-thread builds; small models |

---

## 7. Harness SDKs — Researched 2026-10-08

**Reframing.** The stated need is a client that runs natively in browsers. WASM is one means to
that, not the goal. A TypeScript harness already runs in a browser's JavaScript engine without
WASM. WASM matters for (a) local inference (llama.cpp) and (b) a possible Rust/other-language
core. No general-purpose Rust agent framework (loop, model adapter, tool registry, history)
that targets `wasm32-unknown-unknown` was found; browser agent projects found were demos
(for example Mozilla.ai wasm-agents on Pyodide, hwclass/wasm-browser-agents-blueprint with
WebLLM).

### Pi — [earendil-works/pi](https://github.com/earendil-works/pi) (MIT, TypeScript monorepo)

- Packages seen: `agent`, `ai`, `chord`, `client`, `codemode`, `coding-agent`, `durable`, `env`,
  `evals`, `mcp`, `protocol`, `server`, `telemetry`, `tui`. No web-UI package is present.
- **`pi-ai`** (unified multi-provider LLM API): its README has a *Browser Usage* section stating
  browser environments are supported and that the core entrypoint and provider factories are
  side-effect free and bundle cleanly. API keys must be passed explicitly (or via a
  `CredentialStore`, for example localStorage-backed). **Amazon Bedrock and OAuth login flows
  are Node-only.**
- **`pi-agent-core`** (agent runtime: transport abstraction, state management, attachments):
  runtime dependencies are only `pi-ai` and `typebox`; its README mentions browser apps that
  proxy through a backend. The `engines` field says Node >= 22.19 (a build/dev constraint, not
  proof of a runtime dependency).
- It is deliberately minimal and ships with no built-in permission system. Extension is by
  TypeScript modules, skills and packages. It is a coding-agent harness first; the web client
  would be built on the core packages, not on the shipped product.
- **Fit:** the strongest candidate found for a small, browser-capable core. Unverified: that
  `pi-agent-core` actually runs in a page; whether a local-model provider can be plugged into
  `pi-ai`.

### DeepSeek Harness — [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) (MIT)

- "Everything is a plugin", built on the Cordis microkernel; model adapter, tool registry,
  session log and the agent loop itself are plugins. TypeScript/pnpm.
- **Developer preview; the README warns of compatibility-breaking changes.**
- Large package set, including `core`, `llm`, `mcp`, `sdk`, `client`, `web`, `session`,
  `storage`, `sandbox`, `interaction`, `document`, `deliverables`, `hooks`, `guard`,
  `credentials`, `identity`, `browser-use`, `computer-use`, `lsp`, `shell`, `ssh`.
  Several of these (shell, ssh, lsp, sandbox, subprocess) are coding-agent oriented.
- `dsh web` starts a **local Node server** with a web UI. Whether the kernel and agent loop run
  in a browser without Node was **not established**.
- **Fit:** the plugin architecture matches the "same harness, domain-specific" idea, but it is
  large, moving fast, and coding-oriented. Treat as a reference design or a fork candidate, not
  an obvious dependency.

Reports about star counts and capabilities from third-party blogs conflict with each other and
were not relied on.

---

## 8. UX Definition — 2026-10-08 (second session)

The `refine-strategic-intent` skill is the reference experience: it already encodes the
interaction model in prose form (scaffold early, work top-to-bottom, edit the live document
after every answer, show content before asking for improvement, score silently against a
rubric, name contradictions). The native client gives that model the UI it wants.

### Two-pane layout

- **Left pane — chat.** Bottom-up scroll, Claude-Desktop-style. Socratic, one question at a
  time. The conversation is the primary editing instrument.
- **Right pane — the document.** Scaffolded with its full structure from the start (empty or
  pending sections visible), filled and revised **section by section, top to bottom** as
  agreement is reached in chat.

### The document pane's three jobs

1. **Scaffolding against the blank page.** Users have a hard time starting from scratch; the
   structure appears immediately with a drafted starting point, so the user reacts and refines
   rather than creates from nothing.
2. **External memory.** As the discussion progresses, users cannot remember what they answered
   earlier. The visible earlier sections let the user — not only the model — spot where a new
   answer contradicts or diverges from what is already written.
3. **Visible verification.** Rubric scoring runs in the background and surfaces in the document
   pane, attached per section (pass / partial / fail), not as chat chatter. The chat discusses;
   the document shows the state of the work.

### Decisions (supersede "nothing decided" for these points)

| ID | Decision |
|----|----------|
| D-1 | **The user can edit the document pane directly.** Minor changes should not have to be expressed via chat, and an inspired user may write a section by hand. Manual edits flow into the same server-held draft state as chat-driven edits, are rubric-scored the same way, and must be visible to the model so the conversation stays grounded in the actual document. Refines C-1: the invariant is "the draft lives on the server and every mutation goes through its contract", not "only model tool calls mutate the draft". |
| D-2 | **Passkey approvals only at lifecycle boundaries** — publish draft as proposed, approve, delete, and similar irreversible or outward transitions, consistent with the existing Policy Lifecycle gates. Draft-mode section updates, from either pane, never trigger a passkey ceremony. |
| D-3 | **The right pane is a structured-artifact viewport, not only a draft editor.** Any structured answer to a user question renders there as a read-only report (e.g. a gap report, or "obligations with no policy, grouped by capability"), with or without report templates and with `source_ref` citations intact; AI/user commentary stays in chat. Two artifact classes share one type-neutral contract: read-only reports and editable rubric-scored drafts — so the contract (C-2) must declare per type whether an artifact is editable and whether it is scored. |
| D-4 | **Artifacts accumulate as tabs/a stack in the pane**, so the gap report stays one click away while authoring. The authoring scaffold is pre-wired from the report's context: the draft opens already linked to the Capability, with the uncovered Obligations carried in as its starting scope. |
| D-5 | **Edits are implicit, progression is explicit.** Section content updates live as discussion converges — no per-edit approval ceremony. The checkpoint is the rubric: when a section's score crosses to pass, the AI states it plainly and asks whether to move on. The user owns the pace; the system never advances them. The score is both quality feedback and the section's progression gate. |
| D-6 | **Attention-following with a bookmark.** On a manual edit the AI clearly says it detected the change, re-scores, and comments on its effect (including contradictions with earlier sections) — then follows the user's new focus, holding its place in the top-to-bottom flow, and steers back once that thread is done. Delicate, never scolding. Collisions resolve the same way: the user's typing always wins; an AI mid-write on that section yields and re-grounds on what the user wrote. |
| D-7 | **The score badge is the drill-down.** Clicking a section's badge renders the rubric as a table — each criterion, pass/fail, and a one-line why — so the document pane is self-explanatory without the chat, even on a section revisited long after the conversation moved on. Server consequence (feeds Q-2): the scoring surface must return per-criterion results with reasons, type-neutrally, not just an aggregate. |
| D-8 | **Drafts are durable and resumable; lifecycle events are deliberate.** A draft can be left at any point and picked up later — the server holds the document and its scores, the client restores both and chat recaps the state. Publishing as proposed (or any lifecycle transition) is always an explicit user action, and that action triggers the passkey when the transition warrants one (D-2). Defaults: lifecycle controls live on the document in the right pane (chat may suggest readiness, the click is the user's), and the signing summary binds to exactly what is signed — the Policy tree, its final scores, and a digest — per the Graph Cleanup challenge-binding precedent. |
| D-9 | **Returning to a previous discussion has two entry points that converge.** The user can come back in through the conversation list or through the document/artifact list; either route restores the same state — the discussion with its artifacts, or the document with its discussion. Consequence: the transcript is server-held (it follows the user across desktop and web, like the draft does) and the session ↔ artifact link is a first-class relation. |
| D-10 | **One continuous thread per document; the conversation is part of the draft's record.** Every return, from either entry point, continues the same accumulating discussion. The dialogue that produced a policy becomes a governance asset beside its audit trail. |
| D-11 | **Pure Q&A conversations (no draft produced) persist first-class too**, with their report artifacts restored on reopen. One rule for everything: every conversation persists; some are anchored to a document. |
| D-12 | **Client surfaces and naming** — see the table below. The User Pane aggregates the user's personal surfaces: an **Inbox** of assigned work, a **Library** of conversations and documents (D-9's two entry points), and identity/logout. An Inbox item is a third entry point that converges like the other two: opening an assignment lands in a conversation anchored to the relevant artifact, and an approval assignment ends in the D-2 passkey ceremony. |
| D-13 | **Notifications versus Inbox.** Notifications say "something you were watching finished"; the Inbox says "something needs your decision." A completed pipeline run is a notification on its conversation in the Library; a near-miss surfaced by that run is an Inbox item. Assignments therefore have two sources: human-assigned (a CO assigns authoring) and system-generated (a run mints near-miss work) — both feed Q-12. |
| D-14 | **Review/resolve flows reuse the D-8 pattern.** Near-miss review and graph cleanup render as a comparison artifact — the candidate pair side by side with the merge effect previewed explicitly — with the decision controls on the artifact and a signing summary bound to the exact pair and state, matching Graph Cleanup's existing server-side challenge binding. |
| D-15 | **ps-client is the complete product surface** — every role, admin included. Aspiration: retire ps-cli's operator-facing commands, which requires the client to absorb local-file intake (internal intake JSON today, PDF when that pipeline exists). The MCP-bridge plumbing is not retired — it serves PS Question Skill in other hosts (Claude Desktop, VS Code). |
| D-16 | **v1 scope is the evaluator's journey** (section 11): Login Screen, invite, role assignment and Library from day one, then ingest/restore with run artifacts and completion notifications, near-miss review and graph cleanup (minimal Inbox included), gap Q&A, and authoring through propose. |

### Client surfaces (naming)

| Surface | Name | Role |
|---------|------|------|
| Auth gate | **Login Screen** | Shown when unauthenticated; passkey login via the OIDC IdP (Authentik); disappears on authentication, returns on logout |
| Personal | **User Pane** | **Inbox**: work assigned to the user (e.g. author policies for a newly ingested instrument; approve a proposed policy change). **Library**: the user's conversations and documents/reports. Plus identity and logout |
| Conversation | **Chat Pane** | The dialogue; bottom-up scroll, Socratic, one question at a time |
| Artifacts | **Artifact Pane** | The structured-artifact viewport: tabs of rubric-scored drafts and read-only reports (D-3..D-7) |

Two passkey roles stay distinct behind one user gesture: **login** passkeys live at the OIDC
IdP (Authentik), while **transaction-signing** passkeys belong to PS Service's own WebAuthn
relying party (C-4) — the Login Screen uses the former, the D-2 lifecycle ceremonies the
latter.

## 9. Gap-to-Closed Walkthrough (Q-1)

The scripted reference session, beat by beat. A Compliance Officer; a DORA incident-reporting
gap; left pane = chat, right pane = artifact viewport.

1. **Open and ask.** The officer opens ps-client and asks: *"Do we have a gap for DORA
   incident reporting?"* The AI routes the question to the gap query (`ps-qna` path). The
   right pane renders a **gap report** artifact: the Capability, its Obligations, the missing
   governing Policy, `source_ref` citations. Chat carries a short phrased answer and asks what
   the officer wants to do.
2. **Pivot to authoring.** The officer: *"Help me close this gap."* The AI creates a Policy
   draft on the server (`create-policy-draft`); a new tab opens in the pane showing the full
   scaffold — every section visible, pre-filled where the gap context allows, pending markers
   otherwise — already linked to the Capability with the uncovered Obligations as its scope
   (D-4). The gap report remains one tab away.
3. **Section loop, top to bottom.** Per section: the AI asks one Socratic question; the
   officer answers; the section updates live in the document (server tool call); the badge
   re-scores. On pass, the AI says so plainly and asks whether to move on (D-5). A section
   stuck after 2–3 passes gets a passing example shown, per the refine-skill escalation
   pattern.
4. **Manual-edit interlude.** Mid-flow, the officer types directly into a later section. The
   AI announces it detected the change, re-scores, comments on the effect — then helps with
   what is now top of mind, bookmarks the main flow, and returns when done (D-6).
5. **Badge drill-down.** The officer scrolls back to an earlier *partial* section and clicks
   its badge: a criteria table shows what passes, what fails, and why (D-7). They fix it by
   hand or via chat; either route re-scores identically (D-1).
6. **Descend the tree.** The same loop produces Standard(s) and Control(s)
   (`add-standard-to-draft`, `add-control-to-draft`, `update-*-draft`), each with its own
   rubric and badges.
7. **Leave and resume.** The officer closes the client mid-draft. Days later they return
   through either entry point — the conversation list or the document list (D-9) — and land in
   the same place: the draft from server state, scores intact, the one continuous discussion
   thread continued (D-10); chat recaps where things stand (D-8).
8. **Propose.** All sections pass; holistic rescore holds; the AI suggests the tree is ready.
   The officer clicks **Propose** on the document. A signing summary shows the exact Policy
   tree, final scores, and digest; the passkey ceremony runs; the lifecycle moves
   draft → proposed; the transition is audited (D-2, D-8).

## 10. Server Contract Reality Check (Q-2) — read 2026-10-09

What the repo actually provides against the pane contract (C-2, D-3, D-7), from
`ps-skills/ps-plugin/rubrics/`, `ps_service.policy_lifecycle.service`,
`ps_service.domain_schema`, and the `ps-author-policy` skill.

### What exists and is already type-neutral

- **A uniform scoring model** (`rubrics/scoring-model.md`): every per-type rubric shares one
  criterion structure — stable `id`, `dimension`, `weight` (sums to 1.0), uniform 0/1/2 scale
  with per-anchor guidance text — one aggregation formula (0–100) and a per-type
  `pass_threshold` (80 everywhere today). Criterion ids are governed as stable; template
  sections map 1:1 to criterion ids. Conceptually this *is* the D-7 contract.
- **A typed domain-schema registry** (#199, `ps_service.domain_schema`): per-node `Property`
  with type/presence/write-ownership flags. The draft tools' patchable-field allowlists are
  *derived* from it (`patchable_fields("Policy")`), and the rubric-paired content fields
  (`scope_in`, `normative_commitments`, …) are real schema properties. The `domain_concepts`
  MCP tool serves a slim rendered form (text, not structured data).
- **A schema-derived, field-level write path**: `update-policy-draft` /
  `update-standard-draft` / `update-control-draft` PATCH individual content fields on a
  draft, owner-or-elevated, draft-status-gated — exactly the "every mutation through the
  draft contract" D-1/D-5 assume.

### What is missing

| Gap | Detail |
|-----|--------|
| G-1 **No server-side scoring at all** | Nothing in `ps_service` scores; the `ps-author-policy` *skill* (the model) scores against markdown rubrics shipped in the plugin. Scorecards are explicitly future work ("once scorecards exist as persisted records", scoring-model.md §6). **This contradicts section 2's premise that "the server-side rubric score is an objective verifier" — today the verifier is the client model itself.** |
| G-2 **Read path is skeleton-only** | `get-policy`'s `PolicyView` returns id/title/status/version/owner plus the Standard/Control tree skeleton — none of the content fields. A client cannot render document sections from the lifecycle read surface; today's skill works around it via raw `cypher`. |
| G-3 **Rubrics are not served** | Rubric definitions (criteria, weights, guidance, threshold) live only as plugin markdown. C-2's goal — new document types without client changes — needs rubric-as-data served per document type. |
| G-4 **No artifact-type capability flags** | Nothing declares per type whether an artifact is editable/scored (D-3). Trivial to add once G-3's surface exists. |

### Consequence

The conceptual model is ready; the serving is not. The client-presumed server capabilities
from the UX decisions (D-9/D-10 transcripts, Q-12 assignments) gain four more from this
check: a scoring execution + persisted-scorecard surface (G-1), a full-content document read
(G-2), rubric-as-data (G-3), and type capability flags (G-4). **Open decision: where does
scoring run** — a server-side scoring component (via LLM Interface; restores the "objective
verifier" premise and keeps weak local client models honest) versus client-model scoring
with server-persisted scorecards.

## 11. Product Surface Scope — sweep 2026-10-09

ps-client is the complete product surface (D-15). The fourteen product skills group into
five interaction classes; every class renders through the four surfaces of D-12 without new
machinery beyond the decisions above.

| Class | Skills | UX treatment |
|-------|--------|--------------|
| Q&A → report | ps-qna, ps-list-ingested, ps-get-catalog-listing, ps-list-audit-events, ps-assess-instrument-applicability | D-3: structured answers render as report artifacts; commentary in chat. Assess-applicability is interview → report — the authoring loop's shape with a read-only output. |
| Authoring + lifecycle | ps-author-policy, ps-policy-lifecycle | D-1..D-8; walkthrough beats 2–8. |
| Long-running runs | ps-ingest-regulation, ps-check-regulations, ps-restore-instrument | A live **run artifact** in the Artifact Pane: per-stage progress (Ingestion → Domain Mapper → Company Merge), timings, terminal summary, persisting as the run's record. Completion is a notification; surfaced near-misses are Inbox items (D-13). |
| Review/resolve | ps-near-miss-review, ps-graph-cleanup | D-14: comparison artifact, effect preview, decision controls on the artifact, passkey summary bound to pair + state. |
| Admin | ps-invite-user, ps-manage-access-roles | In scope (D-15): chat-driven with report artifacts (role listings as D-3 reports); low ceremony. |

### v1 — the evaluator's journey (D-16)

A freshly installed system must be usable end to end through the client alone:
log in → invite users → assign roles → ingest or restore instruments → clean up
(near-misses, duplicate Capabilities/Obligations) → identify gaps → author policies to
close them → propose. That means from day one: Login Screen, User Pane (Library; minimal
Inbox for near-miss work), invite + role assignment, run artifacts with completion
notifications, comparison artifacts with passkey ceremonies, report artifacts, and the
full authoring loop.

### Out of v1

- Amendment monitoring UX (`ps-check-regulations` sweep) — same run-artifact pattern, later.
- `ps-assess-instrument-applicability` — interview → report, no new machinery, later.
- Audit-events browsing (`ps-list-audit-events`) — D-3 report, later.
- Local-file intake (internal intake JSON, future PDF pipeline) — prerequisite for retiring
  ps-cli's operator-facing commands (D-15), not for the evaluator journey.
- ps-cli retirement itself — follows file intake.

## 12. Open Questions

| ID | Question | Needed for |
|----|----------|-----------|
| Q-1 | **Answered (sections 8–9):** two-pane layout, direct editing (D-1), approval granularity (D-2), artifact viewport and tabs (D-3, D-4), implicit edits / explicit progression (D-5), manual-edit handling (D-6), badge drill-down (D-7), resumable drafts and deliberate lifecycle events (D-8), plus the scripted walkthrough. Remaining: a visual mockup. | Step 1 |
| Q-2 | **Answered (section 10):** the scoring model and domain schema are already type-neutral, but the server serves none of it — no scoring execution, skeleton-only `get-policy`, rubrics only as plugin markdown (G-1..G-4). Open follow-on: where scoring runs (server component vs client model + persisted scorecards). | Contract design |
| Q-3 | **Partly answered (section 7).** Is the real requirement "runs in a browser" (a TypeScript harness satisfies it) or "compiled to WASM" (implies a non-JS core)? | Harness evaluation |
| Q-4 | **Answered (section 6):** NobodyWho has no web build (issue #111 open). Browser local inference would use wllama or WebLLM. Open: does a realistic local model do the dominant tasks in a browser at acceptable speed and size? | Local-model option |
| Q-5 | **Partly answered (section 7).** Pi's `pi-ai` documents browser use; DeepSeek Harness's core browser-readiness is unestablished. Open: hands-on spike of either. | Harness evaluation |
| Q-8 | Should one local-inference engine (wllama or WebLLM in the webview) serve both desktop and web, instead of a separate NobodyWho path on desktop? | Local-model option |
| Q-9 | **Partly answered (CUC-01):** auth uses a backend-for-frontend on web (IETF browser-based-apps BCP), so the browser holds no tokens. The BFF carries all PS Service traffic on web (so no CORS) and is a separate deployable beside PS Service on an off-the-shelf OIDC proxy. Open: spike confirmation (SP-3, SP-5). | C-4 |
| Q-6 | **Partly answered (CUC-01):** login uses a backend-for-frontend on web (tokens never in the page) and RFC 8252 on desktop. Open: how passkey signing approvals work against PS Service's own WebAuthn RP from a browser-hosted client (SP-4). | C-4 |
| Q-7 | macOS packaging: web tech in a shell (Tauri / Electron) versus native Swift. Interacts with the WASM goal. | Platform |
| Q-10 | A colleague with access opens a draft that has an authoring thread (D-10): do they join the one thread, read it read-only, or something else? Multi-user semantics of a shared thread are undesigned. | D-10 |
| Q-11 | Does a document's thread freeze when the document leaves draft (proposed/approved)? And what retention/compaction applies to long-lived threads, given the conversation is part of a governance record (GDPR exposure included)? | D-10, server contract |
| Q-12 | The Inbox (D-12) presumes a **work-assignment model that PS Service does not have**: lifecycle approval today is role-gated (any PolicyManager, minus self-approval), not assignment-gated, and nothing models "assign a Policy Officer to author policies for this instrument". Needs domain design: work-item states, assigner/assignee, **two creation sources (human-assigned and system-generated, e.g. near-misses from a run — D-13)**, relation to AccessRoles and no-self-approval, notification. | Inbox / server contract |

---

## 13. Suggested Next Steps

Superseded: next steps now live in `ps-client-ux-design.md` §7.3 (evidence plan) and
`ps-client-use-cases/README.md` (use cases to write).

1. ~~Mock the two-pane UX visually~~ — done: `ps-client-mockup/index.html`.
2. Read the Policy Lifecycle and `ps-author-policy` surfaces to answer Q-2.
3. Hands-on spike: bundle `pi-ai` and `pi-agent-core` into a bare page, call a hosted model,
   then drive one PS MCP tool (Q-5, Q-9). Separately, load a small GGUF in wllama and measure
   gap-routing accuracy and speed (Q-4, Q-8).
4. Decide the "browser-runnable vs WASM" question (Q-3) before choosing a harness.
