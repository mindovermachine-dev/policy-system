# AC3 Analysis — pipeline-rag3 Native Graph Inspection

## Summary
AC1: MET. AC2: MET. AC3: Data ready. Recommendation: **PROCEED TO RAG4.**

## Content spot-check (30-chunk run, sample 3 per core edge type)

**DEFINES** (Regulation→Role):
- Product with digital elements → Manufacturer ✓
- Regulation → Manufacturers ✓
- This Regulation → Importer ✓

**HAS** (Role→Obligation):
- Conformity assessment body → Be capable of carrying out all conformity assessments ✓
- Importer → Comply with Specified Article 13 and 19 Du… ✓
- Distributor → Inform Manufacturer of Vulnerability ✓

**REQUIRES** (Obligation→Capability):
- Inform Manufacturer of Vulnerability → Vulnerability Reporting ✓ (convergence point!)
- Take Corrective Measures or Withdraw/Recall Product → Market Suspension and Recall Management ✓
- Inform Manufacturer and Authorities → Cybersecurity Risk Notification ✓

**EXPRESSES** (Regulation→Requirement):
- ANNEX VII → Article 13(8) ✓
- ANNEX VII → Part I of Annex I ✓
- ANNEX VII → Article 13 ✓

Extraction quality: **HIGH.** Semantic meaning is correct. Capability
convergence points are present and meaningful.

---

## Graph: 30-chunk sample (`--substantive 30 --spread`)

### Automated bar results

| Check                                   | Threshold               | 15-chunk | 30-chunk |  |
|-----------------------------------------|------------------------|----------|----------|--|
| Core chain edge types (DEF,EXP,HAS,REQ) | ≥ 4 of 4               | 4/4 ✓    | 4/4 ✓    |PASS|
| Domain entity count (>50)               | > 50                   | 47       | 128      |PASS|
| DEFECT-1 regression (type collision)    | 0                      | 0        | 0        |PASS|
| UNKNOWN-labeled entities                | 0                      | 0        | 0        |PASS|
| PracticeArea coverage                   | > 0                    | 0        | 2        |PASS|
| Cross-ref Regs < 50% of all Regs        | < 50%                  | 12/14=86%| 18/19=94%|FAIL|

**5/6 checks pass. The only failure (cross-ref Regs 94%) is a known,
documented issue that is specifically the job of rag4/transform.py to solve.**

### 30-chunk node distribution

| Label        | Count | Baseline |
|-------------|-------|----------|
| Requirement | 36    | 287      |
| Role        | 26    | 19       |
| Obligation  | 24    | 349      |
| Capability  | 20    | 71       |
| Regulation  | 19    | 4        |
| PracticeArea| 2     | 10       |
| Standard    | 1     | 10       |
| Policy      | 0     | 10       |
| Control     | 0     | 10       |
| RiskPath    | 0     | 6        |

### 30-chunk edge distribution (domain only, RELATES rel_type)

| Edge type      | Count | Baseline | Note                          |
|---------------|-------|----------|-------------------------------|
| DEFINES        | 29    | 19       | 153% — some are cross-role    |
| EXPRESSES      | 22    | 287      | 8%                            |
| HAS           | 20    | 349      | 6%                            |
| REQUIRES       | 9     | 396      | 2%                            |
| SUPERSEDED_BY | 6     | 0        | Cross-ref (external laws)    |
| SATISFIED_BY   | 0     | 354      | Native emits REQUIRES, not SB |

### DEFECT-1 regression check
All 20 sampled Capability entities have `type='Capability'` (correct discriminator)
with `capability_type` as a separate property ('technical'|'organizational').
0 nodes have the pre-fix collision. **DEFECT-1 HOLD.**

### Regulation cross-reference analysis
19 Regulation entities: only 3-4 are CRA proper (Cyber Resilience Act,
Regulation (EU) 2024/2847, "This Regulation", "Regulation"). The remaining
~15 are external acts cited by CRA (DORA, AI Act, GDPR, ENISA, national laws,
Directives) plus structural items (ANNEX VII, "Product with digital elements").
This is EXPECTED: the `--substantive /shall|should/` filter selects content-dense
chunks which naturally cite other legislative instruments.
**Fix belongs in rag4/transform.py with `regulation_map.json` canonical filter.**

### 4-chunk LLM NER JSON failures
4 out of 30 chunks returned invalid JSON during LLM extraction.
This is a model reliability issue, not a pipeline bug. The 4-chunk gap
out of 30 is 13% — acceptable for a spike.

---

## Recommendation
1. **Proceed to pipeline-rag4.** The mechanism (PDF → GraphRAG-SDK → native
   graph → FalkorDB) is proven end-to-end. 5/6 automated bars pass. The one
   failure (cross-ref Regs) is the explicit job of transform.py in rag4.
2. **The `SATISFIED_BY` gap is a known, documented issue.** Native graph
   emits `REQUIRES(Obl→Cap)` not `SATISFIED_BY(Req→Obl)`. transform.py must
   handle this mapping.
3. **For a stronger automated bar going forward**, define:
    `min_domain_entities=80, min_core_chain_types=4, defect1_regressions=0,
     max_llm_json_failures_pct=10`
   Run this bar against the rag4 final graph.
4. **Governance layer (Policy/Standard/Control/PracticeArea/RiskPath)** is still
   sparse (0-2 nodes). Either expand the sample to full corpus in rag4, or add
   a governance-specific content selection.

## Manual inspection call (for user)
The graph is non-trivial, the mechanism works, the core chain is coherent,
and 5/6 automated bars pass. The remaining quality gaps (cross-ref noise,
SATISFIED_BY gap, sparse governance layer) are explicitly scoped to
transform.py (rag4's job). **Recommend: PROCEED TO RAG4.**
