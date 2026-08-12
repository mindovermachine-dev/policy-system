# Narrowing Pilot — Source Questions

Five questions selected from [`dev-questions.md`](./dev-questions.md)'s
non-Helvex set (31 of the file's 54 questions — the other 23 depend on the
Helvex Policy/Standard/Control layer removed from the live graph on
2026-08-12, see `spikes/pipeline2/PROGRESS.md` D13 and the graph-reload
note), chosen to maximize test value for a simulated Socratic-narrowing
pilot of `.github/skills/policy-question/SKILL.md` steps 1-4. This fills
the open item flagged in `spikes/pipeline2/smoke-test/LEARNINGS.md`:
*"Socratic narrowing — untested, needs a different test shape (simulated
naive-user dialogue)."*

**Deliberately excludes expected answers and grading criteria** (those stay
in `dev-answers.md`, not duplicated here). A pilot's simulated-user role
must never see the answer key — if it does, narrowing "succeeds" because it
was handed the answer, not because the questions were good. Keeping this
file answer-free makes that isolation the default rather than something
someone has to remember to enforce later.

## Selection criteria

- **Natural-register preferred over canonical.** Canonical (schema-
  vocabulary) questions already use the model's own words and would narrow
  in ~0 turns — testing nothing about the skill's Socratic behavior.
- **Diversity of audience persona and difficulty tier**, not concentrated
  in one.
- **Diversity of narrowing dimension stressed**, so the 5 together exercise
  different parts of the skill's checklist (entity-type ambiguity,
  relationship/direction, scope bound, counting unit, status/lifecycle
  bound) rather than the same one five times.
- **Preference for genuine "no clean answer without asking" questions**
  over ones that are already effectively pre-scoped.

## Selected questions

### NP-001 (source: LC-H1 — Legal Counsel, Hard, natural)

> "Do CRA and NIS2 put duties on similar kinds of actors — is there
> something like a 'manufacturer' in both?"

**Why this one:** Roles aren't shared across regulations in this model
(Role identity is tied to its defining Regulation) — no structural edge
answers "similar." Stresses whether narrowing correctly identifies the
entity type (Role) while resisting the temptation to invent a
similarity edge/property the schema doesn't have, and whether it asks the
user what "similar" should mean rather than assuming.

### NP-002 (source: RM-M3 — Risk Manager, Medium, natural)

> "How concentrated is our compliance risk — how much of what we have to
> do rides on a few shared capabilities versus many single-use ones?"

**Why this one:** "Concentrated," "rides on," and "shared" are analytical
vocabulary, not schema vocabulary. Stresses counting-unit narrowing (what
threshold makes a Capability "shared" vs. "single-use"?) and scope bound,
on an open-framing question structurally similar to this session's own
"are we CRA compliant?" narrowing.

### NP-003 (source: SEC-M3 — Security Engineer, Medium, natural)

> "How many regulatory duties across CRA, NIS2, and GDPR land on our
> access-control/MFA capability — and which regulation actually says
> 'multi-factor authentication'?"

**Why this one:** A genuine compound two-part question (a count, plus a
separate specific-regulation lookup) — the same shape this session's own
NQ-001 (Role count + names) turned out to be, giving a direct comparison
point for how the skill handles compound asks. Also mildly ambiguous on
entity identity: "access-control/MFA capability," singular or two.

### NP-004 (source: SA-M1 — Security Architect, Medium, natural)

> "Across CRA, NIS2, and GDPR — where do we need a security-logging-type
> capability?"

**Why this one:** "Security-logging-type" is approximate, not a real
Capability name — stresses whether narrowing resists silently substituting
a specific Capability id and instead confirms the mapping with the user.
The real answer is asymmetric (only CRA converges on it) — a
partial-coverage case, same shape as NP-001's "no clean shared answer."

### NP-005 (source: SWE-H2 — Software Engineer, Hard, natural)

> "I'm building a new microservice that stores customer PII in a
> database — what compliance-related capabilities should I be thinking
> about?"

**Why this one:** The most open-ended question in the pool — no entity
type is named at all, and it's forward-looking (a system that doesn't
exist yet) rather than a lookup against existing data. Strongest test of
"narrow until answerable, not until trivial": can the skill converge on a
single scoped question, or should it push back that this needs a
different framing than a single Question/Entities/Edges artifact?

## Not yet done

Running these through the actual pilot (isolated skill-follower +
isolated simulated-user, per the isolation discipline in
`spikes/pipeline2/smoke-test/run-02-subagents.md`) — this file only
records the source-question selection and the rationale behind it.
