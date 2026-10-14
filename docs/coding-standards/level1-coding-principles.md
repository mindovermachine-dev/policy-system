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
- Default to Detroit-style (classicist) unit tests: exercise real collaborators and assert on resulting state/output. Reserve mocks/stubs for true boundaries the test can't run for real (network, external services, clock, randomness) — not for internal collaborators of the unit under test. Don't assert on interactions (call counts/args) unless the interaction itself is the behavior being specified.

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

## Documentation

- Document every public function, class and module boundary so a reader can use it without reading its body.
- No provenance. Code text (docstrings, comments and test text) never cites acceptance-criteria identifiers, issue or pull request numbers, plan, spike, handoff or tracker documents, or narrates history ("added for", "previously", "replaces"). Readers cannot open those references and the text goes stale; that context belongs in the tracker and in commit messages.
- Score each docstring against the rubric below. Each row is scored 0 (absent), 1 (present but incomplete or vague) or 2 (complete and precise).

| Criterion      | Requirement               | What a score of 2 means                                                                  |
| -------------- | ------------------------- | ---------------------------------------------------------------------------------------- |
| Purpose        | Mandatory                 | States what the unit is for and why it exists, in domain terms.                          |
| Summary        | If applicable             | A leading line that stands alone as a description.                                       |
| Parameters     | Mandatory                 | Each input: meaning, units, allowed values and constraints.                              |
| Return Value   | Mandatory                 | What is returned, its shape, and what the empty or absent case means.                    |
| Exceptions     | Mandatory when applicable | Each error raised and the condition that triggers it.                                    |
| Side Effects   | Mandatory when applicable | Every state change, I/O and external call the unit performs.                             |
| Business Rules | If applicable             | The domain rules and invariants the unit enforces.                                       |
| Examples       | If applicable             | A short usage example whose output is correct.                                           |
| Accuracy       | If applicable             | Every statement matches the current behavior of the code.                                |
| Readability    | If applicable             | Plain wording, short sentences, no filler; cheap for a human or an LLM to read.          |

- This rubric is guidance for authors and reviewers, not a hard gate: a low score is a review finding, not an automatic failure. The no-provenance rule is the exception and is checked mechanically.
- Summary, Accuracy and Readability have no fixed floor; apply them where they help the reader.
- Tests are documented by their names. Only the no-provenance rule applies to tests; the Parameters and Return Value rows do not apply.
