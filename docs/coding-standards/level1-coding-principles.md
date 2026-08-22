<!-- © 2026 Cartman ApS. All rights reserved. -->
# Level 1: Universal Coding Principles

These principles apply to ALL code regardless of language or framework.
They are enforced during code implementation and impact assessment.

---

## Single Responsibility

- A function/method does ONE thing. If it needs an "and" to describe it, split it.
- A class/module has ONE reason to change.
- Refactor to meet this — do not leave multi-purpose functions.

## Clarity Over Cleverness

- Code is read far more than it is written. Optimize for readability.
- Use descriptive names that reveal intent — avoid abbreviations.
- Prefer explicit over implicit behavior.

## Naming Semantics

- Names reveal intent — describe **what** or **why**, never **how**.
  Test: imagine a different implementation; the name should still work.
- Functions and methods use verb phrases (`calculate_total`, `fetchUser`).
  Types and classes use noun phrases (`InvoiceLine`, `UserProfile`).
- Booleans read as questions with prefixes: `is`, `has`, `should`, `can`
  (`isValid`, `has_permissions`). Prefer positive form (`isEnabled` over `isDisabled`).
- Avoid meaningless names: `data`, `info`, `temp`, `result`, `manager`, `handler`, `process`.
  Every name must answer "what specifically?"
- Names must be pronounceable and searchable — no acronym soup or single-letter
  names outside tight loop counters.
- Do not encode type information in names (`accountList` → `accounts`, no Hungarian notation).
- One word per concept, used consistently: pick `fetch` OR `get` OR `retrieve`
  — not all three in the same codebase.
- Use the project's domain vocabulary (ontology/ubiquitous language) in code.
  If a domain term exists, use it — do not invent synonyms.
- Consistent paired opposites: `open`/`close`, `start`/`stop`, `begin`/`end`
  — never mix pairs (`begin`/`stop`).
- Name length should be proportional to scope: short names for 5-line blocks,
  descriptive names for module-level or public API symbols.
- Abbreviation policy: only universally understood abbreviations are allowed
  (`URL`, `HTTP`, `ID`, `DNS`, `API`, `SQL`, `HTML`, `CSS`).
  If in doubt, spell it out. Casing rules for abbreviations are defined in L2 per language.

## Fail Fast at Boundaries

- Validate inputs at system boundaries (API handlers, event consumers, public interfaces).
- Do not validate deep inside business logic — trust what passed the boundary.
- Return clear, actionable error messages.
- Exception: security-critical sinks (query/command construction, file paths, subprocess
  arguments, prompt interpolation) get a second validation layer at point-of-use —
  defense-in-depth, not a substitute for boundary validation.

## Don't Repeat Yourself

- Extract shared logic into a single location.
- But: prefer duplication over the wrong abstraction. Two similar things may diverge.

## Dependency Inversion

- Depend on abstractions, not concretions.
- Business logic must not depend on infrastructure details.
- Use dependency injection to wire implementations.

## Composition Over Inheritance

- Prefer composing behavior from small, focused components.
- Use inheritance only for genuine "is-a" relationships.

## Test-Driven Quality

- Write tests that verify behavior, not implementation.
- Each test should have a single reason to fail.
- Test names describe the scenario and expected outcome — specific naming conventions are defined in L2 per language.

## Immutability by Default

- Prefer immutable data structures where practical.
- Minimize mutable state — it is the primary source of bugs.

## Error Handling

- Handle errors at the appropriate level — not everywhere.
- Never swallow exceptions silently.
- Use domain-specific error types, not generic ones.

## Security by Design

- Never trust external input — validate and sanitize at boundaries.
- Never log secrets, tokens, or personally identifiable information.
- Apply least privilege to all access controls.

## Cyclomatic complexity

- Keep it as low as possible and never more than 8
