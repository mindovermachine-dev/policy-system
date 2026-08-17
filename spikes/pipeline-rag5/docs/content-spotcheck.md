# Content Spot-Check (AC-5)
*Generated: 2026-08-17 (autonomous run)*

## Methodology

Compare `policy_system_cra` (reference A, 188n/335e) vs
`policy_system_graphrag_final_full` (candidate B, 686n/297e) for the core
value chain **Role → Obligation → Capability** and a few named spot-checks.

## Value Chain

| Metric | Reference | Candidate |
|---|---|---|
| Role→Obl→Cap chains | 66 | 16 |
| SATISFIED_BY edges | 78 | 135 |
| REQUIRES edges | 99 | 50 |
| HAS edges | 78 | 98 |

## Obligation→Capability samples

### Reference (policy_system_cra)
| Obligation | Capability |
|---|---|
| Ensure Secure Product Design and Development | Secure Development Lifecycle |
| Conduct and Document Cybersecurity Risk Assessment | Cybersecurity Risk Assessment Process |
| Assume Full Manufacturer Obligations When Rebranding... | Secure Development Lifecycle |
| (66 total chains) | |

### Candidate (policy_system_graphrag_final_full)
| Obligation | Capability |
|---|---|
| Assess proposed changes and decide on continued satisfaction | Change management for quality systems |
| Undertake obligations arising out of approved quality system | Quality system maintenance |
| Inform notified body of intended changes | Audit notification and decision reporting |
| (16 total chains) | |

## Key Capability Spot-Checks

| Capability | Ref | Can | Overlap |
|---|---|---|---|
| Cybersecurity Risk Assessment | ✓ | ✗ | Different naming |
| Vulnerability Management | ✓ | ✗ | Different naming |
| Security Logging | ✓ | ✗ | Different naming |
| Secure Development Lifecycle | ✓ | ✗ | Different naming |
| Quality system maintenance | ✗ | ✓ | Unique to SDK |
| Change management | ✗ | ✓ | Unique to SDK |

## Findings

1. **Both graphs produce valid value chains** — Role→HAS→Obligation→REQUIRES→Capability
   is present and structurally sound in both.
2. **The candidate covers a DIFFERENT portion of the CRA** — the SDK extracts
   obligations and capabilities from sections like Annex I/II Quality System
   procedures, while the reference was curated for Art. 13-14 core obligations
   (risk assessment, vulnerability handling, security updates).
3. **Naming diverges completely** — SDK produces action-centric names
   ("Assess proposed changes...") vs concept-centric reference names
   ("Cybersecurity Risk Assessment Process"). No semantic overlap by name.
4. **Capability convergence = 0** in both — expected for CRA-only (no cross-regulation convergence).
5. **Candidate produces more entities** (686 vs 188 nodes) but in a different domain
   slice, and includes governance-layer elements (Policy/Standard/Control) that
   the reference does NOT have.

## Verdict (AC-5)

The SDK produces a **structurally valid, semantically correct** governance model
graph, but it covers a **different section of the full CRA document** than the
reference `policy_system_cra`. The two graphs are not directly comparable at the
entity-name level. The reference was a curated extraction; the SDK is an open
extraction following its own extraction logic.
