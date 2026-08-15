# Engineering Practices Regulation

**Identifier:** ENGPRAC-3.0
**Title:** Engineering Practices Regulation
**Source type:** Internal (organizationally-authored Business Regulation, governed through internal engineering governance rather than official-source ingestion)
**Effective date:** 2026-08-01
**Version:** 3.0
**Status:** Active

This is a narrative prose rewrite of `test-data/engineering-practices/engineering-practices-seed.json`, written for GraphRAG-SDK ingestion in `spikes/pipeline-rag`. GraphRAG-SDK ingests unstructured text/PDF/Markdown, not pre-structured JSON, so this document restates the same facts as prose — the same test CRA/NIS2/GDPR get, extracted from real narrative text rather than handed a pre-built graph. Section numbers below are chosen to match the `source_ref` values in the original JSON so extraction quality can be checked against the same anchors.

---

## Section 3 — Roles

**3.1 Service Owner.** The Service Owner is the accountable owner of a production service and its compliance posture.

**3.2 Engineering Manager.** The Engineering Manager owns team execution and adherence to engineering policy.

**3.3 Security Engineer.** The Security Engineer implements and verifies security controls across the software development lifecycle.

**3.4 SRE Lead.** The SRE Lead owns reliability engineering standards and operational readiness.

**3.5 QA Lead.** The QA Lead owns quality gates and release test confidence.

**3.6 Data Protection Officer.** The Data Protection Officer owns privacy engineering governance and data protection obligations.

---

## Section 4 — Requirements

### 4.1 Policy Governance and Exception Management

Engineering managers shall maintain approved policy governance and controlled exception handling for all production services.

This requirement is satisfied by the Engineering Manager's obligation to **Maintain Policy Governance and Exception Management** (an organizational obligation).

Fulfilling this obligation requires the **Policy and Exception Governance** capability — the capacity to manage policy ownership, review cadence, and controlled exceptions. This capability is classified under the **Engineering Governance and Exceptions** practice area (owned by the eng-governance-board), and mitigates the **Traceability and Auditability** risk path (the risk of non-verifiable engineering practice due to weak evidence trails).

The Policy and Exception Governance capability is governed by the **Engineering Policy Governance** policy (owned by eng-governance-board, status: approved), which must define governance scope and review boundaries — out-of-scope product exceptions require documented approval — and shall require annual review and measurable adherence reporting by policy owners.

That policy is supported by the **Policy Lifecycle and Exception Workflow Standard** (implemented), which defines policy lifecycle steps, policy template requirements, and exception review workflow with quarterly governance sign-off.

That standard is implemented by a manual control: the **Quarterly Policy Governance Board Review** (implemented, executed quarterly, last tested 2026-07-15, next review 2026-10-15, evidence at `evidence://governance/policy-board/2026-q3`), which manually verifies policy lifecycle completion, exception approvals, and overdue policy remediation. This control also serves as verification evidence for the Traceability and Auditability risk path.

### 4.2 Secure SDLC Practices

Service teams shall apply secure SDLC practices including threat-informed design and secure coding standards.

This requirement is satisfied by the Security Engineer's obligation to **Execute Secure SDLC Practices** (a technical obligation).

Fulfilling this obligation requires the **Secure Development Lifecycle** capability, classified under the **Secure Development Lifecycle** practice area (owned by security-engineering), and mitigates the **Secure Build and Release** risk path (the risk of insecure build, weak release controls, or compromised deploy artifacts).

This capability is governed by the **Secure SDLC Policy** (owned by security-engineering, status: approved), which must enforce secure design and coding controls for all internet-facing services — out-of-scope prototypes are prohibited in production — and shall require threat model review before release and measurable secure defect trend reporting.

That policy is supported by the **Threat Modeling and Secure Coding Standard** (implemented), which defines required threat modeling steps, secure coding checks, and release entry criteria for high-risk changes.

That standard is implemented by an automated control: the **Pre-Release Threat Model Enforcement Check** (implemented, executed on every deploy, last tested 2026-08-05, next review 2026-11-05, evidence at `evidence://ci/secure-sdlc-gate/latest`), an automated pipeline gate that verifies the required threat model and secure coding checklist for qualifying releases. This control also verifies the Secure Build and Release risk path.

### 4.3 Identity and Access Enforcement

Engineering systems shall enforce strong authentication, least privilege, and periodic access review.

This requirement is satisfied by the Security Engineer's obligation to **Enforce Engineering Identity and Access Controls** (a technical obligation).

Fulfilling this obligation requires the **Access Control and Authentication** capability, classified under the **Identity and Access for Engineering Systems** practice area (owned by security-engineering), and mitigates the Secure Build and Release risk path.

This capability is governed by the **Access Control for Engineering Tooling** policy (owned by security-engineering, status: approved), which must enforce least privilege and MFA on engineering systems — break-glass use requires approved exceptions — and shall require quarterly access reviews and measurable stale-access reduction outcomes.

That policy is supported by the **MFA, RBAC, and Access Review Standard** (implemented), which defines MFA enforcement, role-based access control procedures, and quarterly access recertification checkpoints.

That standard is implemented by an automated control: the **MFA and RBAC Configuration Audit** (implemented, executed weekly, last tested 2026-08-04, next review 2026-09-04, evidence at `evidence://ci/mfa-rbac-audit/latest`), which automatically audits MFA enforcement and least-privilege role assignments for engineering systems. This control also verifies the Secure Build and Release risk path.

### 4.4 Release and Change Safety

Code changes shall follow controlled release processes with rollback readiness and risk-based approvals.

This requirement is satisfied by the Service Owner's obligation to **Ensure Release and Change Safety** (a technical obligation).

Fulfilling this obligation requires the **Release and Change Control** capability, classified under the **Change, Release, and Deployment Safety** practice area (owned by platform-engineering), and mitigates the Secure Build and Release risk path.

This capability is governed by the **Change and Release Control** policy (owned by platform-engineering, status: approved), which must classify release risk and enforce approval boundaries — emergency bypasses require documented exceptions — and shall require rollback readiness evidence and measurable release-failure-rate objectives.

That policy is supported by the **Release Risk and Rollback Standard** (implemented), which defines release risk scoring, approval rules, and tested rollback procedures for production deployments.

That standard is implemented by an automated control: the **Release Risk and Rollback Readiness Check** (implemented, executed on every deploy, last tested 2026-08-06, next review 2026-11-06, evidence at `evidence://ci/release-rollback-check/latest`), an automated gate that verifies release risk approval and tested rollback plan evidence before deployment. This control also verifies the Secure Build and Release risk path.

### 4.5 Software Supply Chain Assurance

Software components shall be tracked, verified, and license-compliant across the supply chain.

This requirement is satisfied by the Security Engineer's obligation to **Assure Software Supply Chain Integrity** (a technical obligation).

Fulfilling this obligation requires the **Component Inventory and SBOM Management** capability, classified under the **Software Supply Chain and Dependencies** practice area (owned by security-engineering), and mitigates the **Third-Party and Supply Chain Risk** risk path (the risk introduced by external dependencies, licenses, and artifact integrity gaps).

This capability is governed by the **Third-Party Component and License Policy** (owned by security-engineering, status: approved), which must maintain dependency provenance and license compliance boundaries — prohibited licenses require approved exceptions — and shall require periodic SBOM review and measurable critical-dependency exposure reduction.

That policy is supported by the **Dependency Provenance and License Review Standard** (reviewed status), which defines SBOM generation, provenance attestation checks, and license compliance review checkpoints for all releases.

That standard is implemented by an automated control: the **SBOM Provenance and License Compliance Scan** (reviewed status, executed daily, last tested 2026-08-03, next review 2026-09-03, evidence at `evidence://ci/sbom-license-scan/latest`), which automatically scans dependency provenance attestations and license policy conformance for release artifacts. This control also verifies the Third-Party and Supply Chain Risk risk path.

### 4.6 Quality Gate Enforcement

Releases shall satisfy quality gates with automated and risk-based test evidence before deployment.

This requirement is satisfied by the QA Lead's obligation to **Enforce Test and Quality Gates** (a technical obligation).

Fulfilling this obligation requires the **Quality Gate Assurance** capability, classified under the **Quality Engineering and Test Assurance** practice area (owned by quality-engineering), and mitigates the Secure Build and Release risk path.

This capability is governed by the **Test and Quality Gate Policy** (owned by quality-engineering, status: approved), which must enforce mandatory quality gates by release risk class — waiver requests require approved exceptions — and shall require regression coverage review and measurable escaped-defect reduction.

That policy is supported by the **Risk-Based Quality Gate Standard** (implemented), which defines mandatory test suites by risk class and release gating criteria with waiver escalation procedures.

That standard is implemented by an automated control: the **Risk-Based Test Gate Verification** (implemented, executed on every deploy, last tested 2026-08-06, next review 2026-11-06, evidence at `evidence://ci/quality-gates/latest`), an automated release gate that verifies required test suite pass conditions for the assigned release risk class. This control also verifies the Secure Build and Release risk path.

### 4.7 Reliability and Operational Readiness

Production services shall maintain reliability objectives, observability baselines, and recovery readiness.

This requirement is satisfied by the SRE Lead's obligation to **Maintain Reliability and Operational Readiness** (a technical obligation).

Fulfilling this obligation requires the **Service Reliability Management** capability, classified under the **Reliability and Service Operations** practice area (owned by sre), and mitigates the **Reliable Service Operation** risk path (the risk of service instability, degraded reliability, or failed operational readiness).

This capability is governed by the **Reliability and Service Operations Policy** (owned by sre, status: approved), which must define SLO and on-call boundaries for production services — temporary target changes require approved exceptions — and shall require monthly reliability review and measurable error-budget compliance.

That policy is supported by the **SLO and Operational Readiness Standard** (reviewed status), which defines SLO baseline requirements, on-call readiness checks, and resilience validation steps before major releases.

That standard is implemented by a manual control: the **Monthly SLO and Readiness Review** (reviewed status, executed monthly, last tested 2026-08-01, next review 2026-09-01, evidence at `evidence://ops/slo-readiness/2026-08`), a manual review that verifies SLO adherence, incident trends, and operational readiness checklist completion. This control also verifies the Reliable Service Operation risk path.

### 4.8 Vulnerability and Incident Response

Security vulnerabilities and incidents shall be triaged, remediated, and escalated within defined SLAs.

This requirement is satisfied by the Security Engineer's obligation to **Operate Vulnerability and Incident Response** (a technical obligation).

Fulfilling this obligation requires the **Vulnerability Management** capability, classified under the **Vulnerability and Incident Management** practice area (owned by security-operations), and mitigates the **Incident and Recovery Readiness** risk path (the risk of ineffective incident handling and delayed recovery outcomes).

This capability is governed by the **Vulnerability Management and Remediation Policy** (owned by security-operations, status: approved), which must define triage and remediation SLAs by severity boundaries — SLA extension requests require approved exceptions — and shall require weekly remediation review and measurable high-severity aging control.

That policy is supported by the **Vulnerability Triage and Patch SLA Standard** (implemented), which defines triage workflow, patch SLA windows by severity, and verification steps for remediation closure.

That standard is implemented by an automated control: the **Severity-Based Patch SLA Compliance Check** (implemented, executed daily, last tested 2026-08-06, next review 2026-09-06, evidence at `evidence://ci/vulnerability-sla/latest`), which automatically checks that vulnerability remediation closure times meet severity-based SLA thresholds. This control also verifies the Incident and Recovery Readiness risk path.

### 4.9 Data Handling and Privacy

Data handling and privacy safeguards shall be implemented proportionate to data sensitivity and legal obligations.

This requirement is satisfied by the Data Protection Officer's obligation to **Implement Data Protection and Privacy Engineering** (a technical obligation).

Fulfilling this obligation requires the **Data Protection by Design and Default** capability, classified under the **Data Protection and Privacy Engineering** practice area (owned by privacy-office), and mitigates the **Data Protection and Privacy** risk path (the risk of privacy breach or insufficient protection of regulated data).

This capability is governed by the **Data Handling and Privacy Engineering Policy** (owned by privacy-office, status: approved), which must classify data handling boundaries and privacy safeguards by sensitivity tier — processing exceptions require an approved approval path — and shall require periodic privacy review and measurable sensitive-data exposure reduction.

That policy is supported by the **Data Classification and Privacy Safeguard Standard** (implemented), which defines data classification classes, privacy safeguard controls, and evidence requirements for regulated processing.

That standard is implemented by a manual control: the **Quarterly Privacy Safeguard Conformance Review** (implemented, executed quarterly, last tested 2026-07-20, next review 2026-10-20, evidence at `evidence://privacy/conformance/2026-q3`), a manual review that verifies data classification controls and privacy safeguard implementation evidence. This control also verifies the Data Protection and Privacy risk path.

### 4.10 Logging and Audit Traceability

Access and security logging shall be retained and reviewable to support traceability and audit evidence.

This requirement is satisfied by the SRE Lead's obligation to **Maintain Logging and Audit Traceability** (a technical obligation).

Fulfilling this obligation requires the **Security Logging** capability, classified under the **Observability, Logging, and Audit Evidence** practice area (owned by sre), and mitigates the Traceability and Auditability risk path.

This capability is governed by the **Logging and Audit Retention Policy** (owned by sre, status: approved), which must retain audit logs with clear retention boundaries — retention deviations require approved exceptions — and shall require an evidence review cadence and measurable audit retrieval completeness.

That policy is supported by the **Structured Logging and Audit Retention Standard** (implemented), which defines the structured log schema, retention windows, and immutable evidence retrieval procedures for audits.

That standard is implemented by an automated control: the **Structured Log Retention and Retrieval Check** (implemented, executed daily, last tested 2026-08-06, next review 2026-09-06, evidence at `evidence://ci/log-retention-retrieval/latest`), which automatically checks required log retention and successful retrieval of immutable audit evidence samples. This control also verifies the Traceability and Auditability risk path.

**Note on cross-regulation convergence:** the Security Logging capability described in Section 4.10 is the same capacity CRA Art. 11 and GDPR's logging-related provisions require (see `docs/artifacts/ps-domain-concepts.md`'s worked example). Whether this narrative rewrite's extraction actually lands on a Capability node that converges with the CRA/GDPR extractions — versus minting a separate, differently-worded node — is one of the things this spike measures (see README.md "Convergence approach").
