# ps-plugin skill token-cost reduction (issue #200)

Sizes are `SKILL.md` characters from `scripts/measure_skills.py`. Before = `docs/audits/ps-skills-token-baseline.md`.
The target set was fixed from the baseline: the smallest set of largest skills reaching 60% (five skills, 65.4%).

| Skill | Before | After | Change | Read on demand (`references/`) |
| --- | ---: | ---: | ---: | --- |
| `ps-author-policy` | 44926 | 34256 | -23.8% | standard-loop, control-loop, web-research |
| `ps-graph-cleanup` | 42475 | 22198 | -47.7% | release-governance, unmerge, merge-case-reference, error-reference, known-limitation, reconciliation |
| `ps-policy-lifecycle` | 23898 | 8908 | -62.7% | transition-rules, local-test-bypass, error-states, transition-output |
| `ps-assess-instrument-applicability` | 23550 | 14828 | -37.0% | example-interaction, example-degraded-path |
| `ps-ingest-regulation` | 18880 | 9884 | -47.6% | blocking-fallback, error-states |
| **Target set total** | **153729** | **90074** | **-41.4%** | |

## Safety (AC-BI-002)

For all five skills the `## Guardrails` section (the last or second-to-last section, through end of file or `## Output`) is byte-identical to `git show HEAD:<SKILL.md>`.
Reference files were extracted verbatim by line range; each states that the SKILL.md Guardrails still apply.

## `domain_concepts` on load (AC-BI-005)

| Skill | Fetched on load before | Used by its steps | Decision |
| --- | --- | --- | --- |
| `ps-author-policy` | yes | no: every entity, property and status is named inline | removed; On Load now says no fetch is made |
| other four in the set | no | n/a | none |

Outside the set (not changed): `ps-qna` and `ps-list-ingested` fetch it and use it.

## Changed test assertions (AC-BI-006)

- `test_ps_author_policy_skill_content.py`: removed `domain_concepts` from `_CALLED_TOOL_NAMES` (fetch removed); `test_never_describes_a_freshly_scaffolded_control_as_draft_implementation_status` reads the "Never describe..." sentence from `references/control-loop.md` (moved); added two `issue_200` tests (references exist and are named with a `read` condition; On Load has no `domain_concepts` call).
- `test_ps_graph_cleanup_skill_content.py`: 14 tests now read the reference file holding the text they check (release-governance, unmerge, merge-case-reference, error-reference, known-limitation, reconciliation) with assertions unchanged. One assertion is weaker: the final ordering check `index("## Merge case reference") < index("## Guardrails")` became `"## Guardrails" in _text()`, because the merge-case heading moved out of SKILL.md. Added one `issue_200` reference test.
- `test_ps_policy_lifecycle_skill_content.py`: `_skill_text()` reads SKILL.md plus the four references (error-message strings moved to `references/error-states.md`); assertions unchanged. Added one `issue_200` reference test.
- `test_ps_ingest_regulation_skill_content.py`: five skill-text tests read SKILL.md plus references via `_flat_skill_with_references()` (error strings moved to `references/error-states.md`); assertions unchanged. Added one `issue_200` reference test.
- New `test_ps_assess_instrument_applicability_skill_content.py` (the skill had no content test): frontmatter, references exist and are named, four Guardrails sentences pinned, no `domain_concepts` fetch.
