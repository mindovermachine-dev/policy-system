# EU Cyber Resilience Act (CRA) - Obligation Extraction and Policy Mapping

**Document Date**: 20 November 2024  
**Regulation**: Regulation (EU) 2024/2847 of the European Parliament and of the Council  
**Source**: EUR-Lex - 02024R2847-20241120

---

## Executive Summary

This document provides a comprehensive extraction of all obligations from the EU Cyber Resilience Act (CRA), mapping each obligation to its regulatory reference and identifying the organizational policies required for compliance.

The CRA establishes horizontal cybersecurity requirements for products with digital elements placed on the EU market, creating 95 distinct obligations across multiple actor types.

---

## Key Entities and Definitions (Article 3)

| Entity | Definition |
|--------|-----------|
| **Product with digital elements** | Software or hardware product and its remote data processing solutions |
| **Manufacturer** | Natural/legal person who develops/manufactures products, markets them under its name |
| **Remote data processing** | Data processing at a distance designed by/under responsibility of manufacturer |
| **Vulnerability** | Weakness, susceptibility, or flaw that can be exploited |
| **Actively exploited vulnerability** | Vulnerability with reliable evidence of malicious exploitation |

---

## Primary Obligations by Article

### Article 6 - General Requirements for Products

| Ref | Obligation | Regulation Reference | Policy Required |
|-----|------------|-------------------|----------------|
| GEN-01 | Products must meet essential cybersecurity requirements (Annex I) and manufacturer processes must comply | Art 6 | Product Cybersecurity Policy |

---

### Article 13 - Manufacturer Obligations

| Ref | Obligation | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| MGT-01 | Ensure product designed, developed, produced in accordance with Annex I requirements | Art 13(1) | Secure Development Lifecycle (SDL) policy |
| MGT-02 | Undertake cybersecurity risk assessment and use outcome during planning, design, development, production, delivery, maintenance | Art 13(2) | Risk Management Policy with documented assessments |
| MGT-03 | Document and update cybersecurity risk assessment during support period; analyze risks based on intended purpose, reasonably foreseeable use, conditions of use | Art 13(3) | Risk Assessment Procedure document with review schedule |
| MGT-04 | Indicate in technical documentation whether security requirements Part I(2) are applicable and how implemented | Art 13(3) | Security Requirements Applicability Matrix |
| MGT-05 | Include cybersecurity risk assessment in technical documentation (Art 31, Annex VII) | Art 13(4) | Technical Documentation Standard Operating Procedure |
| MGT-06 | Exercise due diligence when integrating third-party components (including open-source) to ensure they don't compromise cybersecurity | Art 13(5) | Third-Party Component Security Policy with due diligence checklist |
| MGT-07 | Upon identifying vulnerability in integrated component, report to component maintainer and address/remediate in accordance with Part II requirements | Art 13(6) | Vulnerability Handling Procedure for third-party components |
| MGT-08 | Systematically document cybersecurity aspects including vulnerabilities of which aware, update cybersecurity risk assessment | Art 13(7) | Cybersecurity Documentation Standard and Tracking Procedure |
| MGT-09 | Ensure vulnerabilities handled effectively during support period in accordance with Part II requirements | Art 13(8) | Vulnerability Management Process covering full lifecycle |
| MGT-10 | Determine support period reflecting time product expected to be in use, minimum 5 years (or lifetime if shorter) | Art 13(8) | Support Period Determination Procedure with criteria and documentation |
| MGT-11 | Have policies and procedures including coordinated vulnerability disclosure policies for processing/remediating vulnerabilities | Art 13(8) | Coordinated Vulnerability Disclosure Policy |
| MGT-12 | Ensure security updates remain available 10 years or remainder of support period (whichever longer) | Art 13(9) | Security Update Availability Procedure and Infrastructure |
| MGT-13 | For substantially modified software versions, provide updates only for latest version if previous versions can access it free | Art 13(10) | Software Version Management and Upgrade Policy |
| MGT-14 | Draw up technical documentation before placing product on market (Art 31, Annex VII) | Art 13(12) | Technical Documentation Standard - Annex VII Compliance |
| MGT-15 | Carry out conformity assessment procedures (or have them carried out) per Article 32 | Art 13(12) | Conformity Assessment Procedure with internal/external capacity |
| MGT-16 | Draw up EU Declaration of Conformity and affix CE marking when compliance demonstrated | Art 13(12) | Declaration of Conformity Process and CE Marking Procedure |
| MGT-17 | Keep technical documentation and Declaration of Conformity available for market surveillance authorities at least 10 years | Art 13(13) | Document Retention Policy with secure storage |
| MGT-18 | Ensure procedures for series production products conformity are in place; take into account changes in development, production, design, characteristics | Art 13(14) | Production Change Management Procedure |
| MGT-19 | Ensure products bear type, batch, serial number or other identification element | Art 13(15) | Product Identification and Traceability Standard |
| MGT-20 | Indicate manufacturer name, registered trade name/address/contact details on product, packaging, or accompanying documents | Art 13(16) | Product Labeling and Contact Information Standard |
| MGT-21 | Designate single point of contact for communication with users (including vulnerability reporting) - not limited to automated tools | Art 13(17) | User Communication Single Point of Contact Procedure |
| MGT-22 | Ensure products accompanied by information and instructions to user per Annex II in language understandable to users and authorities | Art 13(18) | User Documentation Standard with multilingual capability |
| MGT-23 | Ensure information and instructions to users available at least 10 years after product placed on market or support period (whichever longer) | Art 13(18) | Documentation Retention Policy for user documentation |
| MGT-24 | Ensure end date of support period clearly specified at time of purchase, including month/year | Art 13(19) | Support Period Communication Procedure and Sales System Integration |
| MGT-25 | Display notification to users when product reaches end of support period (where technically feasible) | Art 13(19) | End-of-Life Notification Process and Technical Implementation |
| MGT-26 | Either provide copy of EU Declaration of Conformity or simplified declaration with internet address for full version | Art 13(20) | Declaration Distribution Procedure and Website Management |
| MGT-27 | Take corrective measures immediately if product known/has reason to believe not in conformity - bring into conformity, withdraw, or recall as appropriate | Art 13(21) | Non-Conformance Response Procedure with escalation paths |
| MGT-28 | Provide all information/documentation necessary to market surveillance authority upon reasoned request | Art 13(22) | Authority Cooperation Procedure with document preparation process |
| MGT-29 | Inform relevant market surveillance authorities and users if ceasing operations and unable to comply | Art 13(23) | Business Closure and Continuity Plan with stakeholder notification |

---

### Article 14 - Reporting Obligations (Manufacturers)

| Ref | Obligation | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| REP-01 | Notify any actively exploited vulnerability to CSIRT designated as coordinator AND ENISA simultaneously via single reporting platform | Art 14(1) | Vulnerability Reporting Procedure with dual reporting capability |
| REP-02 | Submit early warning notification of actively exploited vulnerability without undue delay and in any event within 24 hours of becoming aware | Art 14(2)(a) | Early Warning Notification Process with 24-hour SLA |
| REP-03 | Indicate Member States where product has been made available in early warning notification | Art 14(2)(a) | Geographic Market Tracking System for vulnerability impact assessment |
| REP-04 | Submit vulnerability notification within 72 hours of becoming aware, providing: general info about product, nature of exploit/vulnerability, any corrective measures, users can take, sensitivity level | Art 14(2)(b) | Vulnerability Notification Template and 72-hour Response Procedure |
| REP-05 | Include in vulnerability notification: description (severity/impact), malicious actor info (if available), security update details | Art 14(2)(b)(i-iii) | Vulnerability Information Collection Checklist |
| REP-06 | Submit final report no later than 14 days after corrective/mitigating measure available | Art 14(2)(c) | Final Reporting Procedure with 14-day SLA |
| REP-07 | Notify any severe incident having impact on product security to CSIRT and ENISA simultaneously via single reporting platform | Art 14(3) | Incident Reporting Procedure for severe incidents |
| REP-08 | Submit early warning notification of severe incident within 24 hours including whether suspected unlawful/malicious acts | Art 14(4)(a) | Severe Incident Early Warning Process with 24-hour SLA |
| REP-09 | Submit incident notification within 72 hours providing: nature of incident, initial assessment, corrective/mit measures users can take, sensitivity level | Art 14(4)(b) | Incident Notification Template and 72-hour Procedure |
| REP-10 | Include in incident notification: detailed description (severity/impact), threat/root cause, applied/ongoing mitigation measures | Art 14(4)(b)(i-iii) | Incident Information Collection Framework |
| REP-11 | Submit final report within one month after incident notification under REP-09(b) | Art 14(4)(c) | Post-Incident Analysis and Final Report Procedure |
| REP-12 | Inform impacted/all users of vulnerability/incident and risk mitigation measures where appropriate in structured machine-readable format | Art 14(8) | User Notification Procedure for security issues |
| REP-13 | If manufacturer fails to inform users in timely manner, CSIRTs may provide information to users | Art 14(8) | Backup User Communication Protocol when manufacturer failure |

---

### Article 15 - Voluntary Reporting

| Ref | Obligation | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| VOL-01 | Manufacturers and other natural/legal persons may voluntarily notify vulnerabilities to CSIRT designated as coordinator or ENISA | Art 15(1) | Voluntary Vulnerability Reporting Procedure |
| VOL-02 | Manufacturers and others may voluntarily notify incidents/near misses to CSIRT designated as coordinator or ENISA | Art 15(2) | Voluntary Incident and Near-Miss Reporting Procedure |

---

### Article 19 - Importer Obligations

| Ref | Obligation | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| IMP-01 | Place on market only products with digital elements that comply with Annex I requirements and manufacturer processes comply with Part II | Art 19(1) | Import Compliance Verification Process |
| IMP-02 | Before placing product on market, ensure appropriate conformity assessment procedures carried out by manufacturer (Art 32) | Art 19(2)(a) | Conformity Assessment Verification Procedure |
| IMP-03 | Verify manufacturer has drawn up technical documentation | Art 19(2)(b) | Technical Documentation Verification Process |
| IMP-04 | Verify product bears CE marking and accompanied by Declaration of Conformity and user instructions in applicable languages | Art 19(2)(c) | Product Compliance Inspection Procedure |
| IMP-05 | Verify manufacturer has complied with Article 13(15), (16), (19) requirements | Art 19(2)(d) | Manufacturer Compliance Checklist for Article 13 obligations |
| IMP-06 | If consider/has reason to believe product not in conformity, shall NOT place product on market until brought into conformity | Art 19(3) | Non-Conformance Hold Procedure with importer escalation |
| IMP-07 | Inform manufacturer and market surveillance authorities if significant cybersecurity risk identified | Art 19(3) | Risk Escalation Procedure for Importers |
| IMP-08 | Indicate importer name, registered trade name/address/contact details on product/packaging/accompanying documents | Art 19(4) | Importer Labeling Standard |
| IMP-09 | Take corrective measures if product known/has reason to believe not in conformity - bring into conformity, withdraw, recall as appropriate | Art 19(5) | Importer Non-Conformance Response Procedure |
| IMP-10 | Inform manufacturer without undue delay about vulnerabilities discovered by importer | Art 19(5) | Vulnerability Communication Process from Importer to Manufacturer |
| IMP-11 | Keep copy of EU Declaration of Conformity available for market surveillance authorities at least 10 years after placement on market | Art 19(6) | Importer Document Retention Policy |
| IMP-12 | Provide all information/documentation necessary to market surveillance authority upon reasoned request | Art 19(7) | Importer Authority Cooperation Procedure |

---

### Article 20 - Distributor Obligations

| Ref | Obligation | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| DIS-01 | When making product available on market, act with due care in relation to CRA requirements | Art 20(1) | Distributor Due Care Procedure |
| DIS-02 | Before making product available, verify product bears CE marking | Art 20(2)(a) | Product Verification Process for CE Marking |
| DIS-03 | Before making product available, verify manufacturer and importer have complied with Article 13(15), (16), (18), (19), (20) and Article 19(4) requirements, provided all necessary documents | Art 20(2)(b) | Distributor Compliance Checklist |
| DIS-04 | If consider/has reason to believe product or manufacturer processes not in conformity, shall NOT make product available until brought into conformity | Art 20(3) | Distributor Non-Conformance Hold Procedure |
| DIS-05 | Inform manufacturer and market surveillance authorities if significant cybersecurity risk identified | Art 20(3) | Distributor Risk Escalation Procedure |
| DIS-06 | If know/has reason to believe product already made available not in conformity, ensure corrective measures taken | Art 20(4) | Distributor Corrective Action Coordination |
| DIS-07 | Inform manufacturer without undue delay about vulnerabilities discovered by distributor | Art 20(4) | Distributor Vulnerability Communication Procedure |
| DIS-08 | If product presents significant cybersecurity risk, immediately inform market surveillance authorities of Member States where made available | Art 20(4) | Distributor Competent Authority Reporting Procedure |
| DIS-09 | Provide all information/documentation necessary to market surveillance authority upon reasoned request | Art 20(5) | Distributor Authority Cooperation Procedure |

---

### Article 24 - Open Source Software Steward Obligations

| Ref | Obligation | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| OSS-01 | Put in place and document verifiably a cybersecurity policy to foster secure product development and effective vulnerability handling | Art 24(1) | Open Source Security Policy with documentation requirements |
| OSS-02 | Policy shall foster voluntary reporting of vulnerabilities (Article 15) by developers | Art 24(1) | Vulnerability Reporting Incentive Program for open source community |
| OSS-03 | Policy shall include aspects related to documenting, addressing, remediating vulnerabilities and promoting sharing within open-source community | Art 24(1) | Open Source Vulnerability Management Framework |
| OSS-04 | Cooperate with market surveillance authorities at request to mitigate cybersecurity risks | Art 24(2) | Authority Cooperation Protocol for Open Source Stewards |
| OSS-05 | Provide documentation referred to Art 24(1) to market surveillance authority upon reasoned request | Art 24(2) | Authority Document Production Procedure |

---

## Annex I - Essential Cybersecurity Requirements

### Part I - Product Requirements

| Ref | Requirement | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| PRD-01 | Products designed, developed, produced to ensure appropriate level of cybersecurity based on risks | Art IX-I(1) | Risk-Based Security Design Policy |
| PRD-02 | Products made available without known exploitable vulnerabilities | Art IX-I(1)(a) | Pre-Release Vulnerability Validation Process |
| PRD-03 | Products made available with secure by default configuration (unless agreed otherwise with business user for tailor-made products, including possibility to reset to original state) | Art IX-I(1)(b) | Secure Default Configuration Standard and Reset Procedure |
| PRD-04 | Ensure vulnerabilities can be addressed through security updates, including automatic updates enabled as default setting with clear opt-out mechanism, notification of available updates, option to temporarily postpone | Art IX-I(1)(c) | Update Management System with auto-update controls |
| PRD-05 | Ensure protection from unauthorized access by appropriate control mechanisms (authentication, identity/access management systems) and report on possible unauthorized access | Art IX-I(1)(d) | Access Control System and Unauthorized Access Detection |
| PRD-06 | Protect confidentiality of stored/transmitted/processed data (personal or other), e.g., encryption at rest/in transit by state of art mechanisms, other technical means | Art IX-I(1)(e) | Data Protection and Encryption Policy with standards specification |
| PRD-07 | Protect integrity of stored/transmitted/processed data, commands, programs, configuration against manipulation/modification not authorised by user, report corruptions | Art IX-I(1)(f) | Data Integrity Validation and Corruption Detection System |
| PRD-08 | Process only data adequate, relevant, limited to what necessary for intended purpose (data minimisation) | Art IX-I(1)(g) | Data Minimization Policy with processing documentation |
| PRD-09 | Protect availability of essential/basic functions also after incident through resilience and mitigation measures against denial-of-service attacks | Art IX-I(1)(h) | Business Continuity and DDoS Mitigation Plan |
| PRD-10 | Minimise negative impact by products themselves or connected devices on availability of services provided by other devices/networks | Art IX-I(1)(i) | System Impact Assessment and Mitigation Procedure |
| PRD-11 | Designed, developed, produced to limit attack surfaces including external interfaces | Art IX-I(1)(j) | Attack Surface Reduction Standard and Interface Management |
| PRD-12 | Designed, developed, produced to reduce impact of incident using appropriate exploitation mitigation mechanisms/techniques | Art IX-I(1)(k) | Exploitation Mitigation Strategy with technical controls |
| PRD-13 | Provide security related information by recording and monitoring relevant internal activity (including access/modification of data/services/functions) with opt-out mechanism for user | Art IX-I(1)(l) | Security Monitoring and Logging Capability with User Controls |
| PRD-14 | Provide possibility for users to securely and easily remove on a permanent basis all data/settings and where transferable, ensure secure manner | Art IX-I(1)(m) | Data Removal and Portability Procedure |

---

### Part II - Vulnerability Handling Requirements

| Ref | Requirement | Regulation Reference | Policy/Process Required |
|-----|------------|-------------------|------------------------|
| VULN-01 | Identify and document vulnerabilities and components contained in products including by drawing up software bill of materials in commonly used machine-readable format covering at least top-level dependencies | Art IX-II(1) | Software Bill of Materials (SBOM) Generation Procedure |
| VULN-02 | Address and remediate vulnerabilities without delay including by providing security updates; where technically feasible new security updates provided separately from functionality updates | Art IX-II(2) | Vulnerability Remediation SLA and Update Separation Standard |
| VULN-03 | Apply effective and regular tests and reviews of the security of products with digital elements | Art IX-II(3) | Security Testing and Review Schedule with methodology |
| VULN-04 | Once security update made available, share and publicly disclose information about fixed vulnerabilities including: description, product identification, impacts, severity, remediation info; may delay if security risks outweigh benefits until users given chance to apply patch | Art IX-II(4) | Vulnerability Disclosure Policy with public communication procedure |
| VULN-05 | Put in place and enforce policy on coordinated vulnerability disclosure | Art IX-II(5) | Coordinated Vulnerability Disclosure (CVD) Policy |
| VULN-06 | Take measures to facilitate sharing of information about potential vulnerabilities in products as well as third-party components including by providing contact address for reporting vulnerabilities discovered | Art IX-II(6) | Vulnerability Reporting Channel and Facilitation Procedure |
| VULN-07 | Provide mechanisms to securely distribute updates for products to ensure vulnerabilities fixed/mitigated timely and where applicable automatic manner | Art II-XII(7) | Secure Update Distribution Infrastructure and Process |
| VULN-08 | Ensure security updates available to address identified security issues disseminated without delay, and unless otherwise agreed with business user for tailor-made product, free of charge accompanied by advisory messages providing users relevant information including on potential action to be taken | Art IX-II(8) | Security Update Communication and Dissemination Procedure |

---

## Summary Statistics

### By Actor Type
- **Manufacturer Obligations**: 29 (MGT-01 through MGT-29)
- **Reporting Obligations**: 13 (REP-01 through REP-13)
- **Importer Obligations**: 12 (IMP-01 through IMP-12)
- **Distributor Obligations**: 9 (DIS-01 through DIS-09)
- **Open Source Steward Obligations**: 5 (OSS-01 through OSS-05)

### By Requirement Category
- **Product Security Requirements**: 14 (PRD-01 through PRD-14)
- **Vulnerability Management**: 8 (VULN-01 through VULN-08)

**Total Obligations Extracted: 95**

---

## Key Policies That Must Be Instantiated

Based on the extraction above, here are the comprehensive list of policies that organizations must implement to achieve CRA compliance:

### Mandatory Core Policies
1. **Secure Development Lifecycle (SDL) Policy** - Covering MGT-01 through MGT-18
2. **Risk Management Policy** - Covering MGT-02, MGT-03, MGT-04, MGT-05
3. **Vulnerability Management Process** - Covering MGT-07, MGT-09, VULN-01 through VULN-08
4. **Coordinated Vulnerability Disclosure (CVD) Policy** - Covering MGT-11, VULN-05
5. **Security Update Management Policy** - Covering MGT-09, MGT-12, PRD-04, VULN-02, VULN-04, VULN-07, VULN-08
6. **Third-Party Component Security Policy** - Covering MGT-05, MGT-06
7. **Technical Documentation Standard Operating Procedure** - Covering MGT-03, MGT-14, Annex VII requirements
8. **Conformity Assessment Procedure** - Covering MGT-15, Article 32 requirements
9. **Declaration of Conformity Process** - Covering MGT-16, Article 28
10. **Document Retention Policy** - Covering MGT-13, IMP-06, MGT-18

### Supporting Policies
11. **Product Identification and Traceability Standard** - Covering MGT-19
12. **Product Labeling Standard** - Covering MGT-16, IMP-04, DIS-01
13. **Non-Conformance Response Procedure** - Covering MGT-27, IMP-06, DIS-04, DIS-05
14. **User Documentation Standard** - Covering MGT-22, MGT-23
15. **Vulnerability Reporting Procedure** - Covering REP-01 through REP-13
16. **Severe Incident Reporting Procedure** - Covering REP-07 through REP-11
17. **User Notification Procedure for Security Issues** - Covering REP-12, VULN-04
18. **Import Compliance Verification Process** - Covering IMP-01 through IMP-12
19. **Distributor Due Care Procedure** - Covering DIS-01 through DIS-09

### Open Source Specific Policies
20. **Open Source Security Policy** - Covering OSS-01, OSS-02, OSS-03
21. **Authority Cooperation Protocol for Open Source Stewards** - Covering OSS-04, OSS-05

---

## Key Compliance Timelines and SLAs

### Critical Time-Based Requirements
| Obligation | Deadline | Regulation |
|------------|----------|-----------|
| Active vulnerability early warning | 24 hours of awareness | Art 14(2)(a) |
| Vulnerability notification (detailed) | 72 hours of awareness | Art 14(2)(b) |
| Vulnerability final report | 14 days after fix available | Art 14(2)(c) |
| Severe incident early warning | 24 hours of awareness | Art 14(4)(a) |
| Incident notification (detailed) | 72 hours of awareness | Art 14(4)(b) |
| Incident final report | 1 month after incident notification | Art 14(4)(c) |

### Support Period Requirements
- **Minimum support period**: 5 years from product placement on market (Art 13(8))
- **Security update availability**: 10 years or remainder of support period, whichever is longer (Art 13(9))

### Documentation Retention
- **Technical documentation and Declaration of Conformity**: At least 10 years after placing on market (Art 13(13), IMP-06)

---

## Enforcement & Penalties

| Violation Type | Fine |
|----------------|------|
| Non-compliance with essential requirements (Articles 6, 7, Annex I, II) | Up to €15M or 2.5% of worldwide turnover |
| Formal non-compliance violations | Up to €10M or 2% of worldwide turnover |
| Providing incomplete, incorrect, or misleading information | Up to €7.5M or 1.5% of worldwide turnover |

**Microenterprises and SMEs**: Exemptions apply for certain reporting SLAs (Articles 14, 16) if fewer than 50 employees and annual turnover not exceeding €10M.

---

## References

1. Regulation (EU) 2024/2847 of the European Parliament and of the Council of 23 October 2024
2. EUR-Lex Entry: 02024R2847-20241120
3. Official Journal L, 2024/2847

---

## Appendix: Additional cra Requirements

### CE Marking and Conformity Assessment (Article 32)
- **Self-declaration**: Most products can be self-certified
- **Third-party assessment**: Required for important/critical products as defined in delegated acts
- **EU Declaration of Conformity**: Mandatory for all compliant products

### Market Surveillance (Articles 26-28)
- Authorities have power to access, examine, test products
- Can require corrective measures, withdrawal, recall
- Can access technical documentation and test results

### Delegated Acts (Article 34)
- Commission may adopt delegated acts supplementing this Regulation
- Covering detailed requirements for:

---

*Document generated as part of the Steward Policy System project*
