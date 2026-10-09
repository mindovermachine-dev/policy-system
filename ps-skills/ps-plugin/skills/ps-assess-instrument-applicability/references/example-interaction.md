# Example interaction (ps-assess-instrument-applicability)

Read on demand from `SKILL.md`'s Process when unsure of the expected shape of an assessment. Every Guardrail in `SKILL.md` still applies.

> **Compliance Officer:** I need to run an applicability assessment for our
> company.
>
> **Skill:** To get started I need two things: the markets/geographies your
> company serves, and the products/services it offers. What
> markets/geographies does your company serve?
>
> **Compliance Officer:** We operate across the EU — customers in most
> member states.
>
> **Skill:** Got it. And what products/services does your company offer?
>
> **Compliance Officer:** Not sure that's relevant, can we skip it for now?
>
> **Skill:** I can't generate an assessment without it — products/services
> offered is required along with markets/geographies. What products/services
> does your company offer?
>
> **Compliance Officer:** Fair enough. We run a SaaS platform that processes
> online card payments for merchants based in the EU.
>
> **Skill:** Here's what I've captured — please confirm before I generate
> the assessment:
>
> ```text
> Markets/geographies: EU (customers across most member states)
> Products/services: SaaS platform processing online card payments for
> EU-based merchants
> ```
>
> Is this correct?
>
> **Compliance Officer:** Yes, that's correct.
>
> **Skill:** _(tiers the four candidates from step 4 first, then, per step 5,
> collects every candidate's determinable CELEX — PSD2, GDPR, and MiCA;
> DORA's is not determinable so it is excluded from the batch — and makes
> one batched call before presenting anything)_
>
> ```text
> check_instrument_ingestion_status(celex_ids=["32015L2366", "32016R0679", "32023R1114"])
> → {"statuses": {"32015L2366": "ingested", "32016R0679": "not_yet_ingested", "32023R1114": "unknown"}}
> ```
>
> ```text
> Captured input (confirmed):
>   Markets/geographies: EU (customers across most member states)
>   Products/services: SaaS platform processing online card payments for
>   EU-based merchants
>
> Disclaimer: This assessment is non-authoritative, may be incomplete, and
> is not a substitute for legal or compliance review.
>
> Candidate instruments:
>   [likely] Revised Payment Services Directive (PSD2) (CELEX 32015L2366)
>     Signal: processes online card payments for EU-based merchants —
>     squarely a "payment service provider" activity within PSD2's scope.
>     Status: ingested
>   [likely] General Data Protection Regulation (GDPR) (CELEX 32016R0679)
>     Signal: processing cardholder and merchant personal data as part of
>     the SaaS platform triggers general EU data-protection obligations.
>     Status: not_yet_ingested
>   [possible] Digital Operational Resilience Act (DORA) (CELEX not
>   determinable)
>     Signal: if the platform's merchant customers include EU-regulated
>     financial entities, DORA's ICT third-party risk-management provisions
>     could reach it as a critical ICT service provider; this needs
>     confirmation of whether the merchant base includes regulated financial
>     entities. The CELEX for the currently-applicable version could not be
>     confidently determined, so it is stated as not determinable rather
>     than guessed.
>     Status: not checked — no CELEX
>   [excluded] Markets in Crypto-Assets Regulation (MiCA) (CELEX 32023R1114)
>     Reason: the company processes card payments for merchants, not
>     crypto-asset issuance, custody, or exchange services — MiCA's scope
>     does not reach it.
>     Status: unknown
>
> Disclaimer (restated): non-authoritative, may be incomplete, not a
> substitute for legal or compliance review.
> ```
>
> This one worked example demonstrates all four ingestion-status flags:
> `ingested` (PSD2), `not_yet_ingested` (GDPR), `unknown` (MiCA — the tool
> call succeeded but could not resolve this CELEX), and
> "not checked — no CELEX" (DORA — no CELEX was ever submitted).
