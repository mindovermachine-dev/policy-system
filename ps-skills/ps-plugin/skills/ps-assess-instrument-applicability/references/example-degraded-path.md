# Example interaction -- degraded path, PS Service connector unreachable (ps-assess-instrument-applicability)

Read on demand from `SKILL.md`'s Process only when the PS Service connector is unreachable at step 5. Every Guardrail in `SKILL.md` still applies.

A shorter continuation from the same confirmed input, showing what happens
when the PS Service connector itself cannot be reached at step 5, rather
than a reachable connector reporting a graph-side problem.

> **Compliance Officer:** Yes, that's correct.
>
> **Skill:** _(tiers the same four candidates, then attempts the batched
> `check_instrument_ingestion_status` call for PSD2, GDPR, and MiCA's
> determinable CELEX ids)_
>
> ```text
> check_instrument_ingestion_status(celex_ids=["32015L2366", "32016R0679", "32023R1114"])
> → connector "ps-mcp" unreachable: connection refused
> ```
>
> **Skill:** I couldn't reach PS Service to check ingestion status for this
> assessment's candidates, so every candidate's status below is reported as
> `unknown` rather than skipping the check silently. Here's the full
> assessment:
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
>     Status: unknown
>   [likely] General Data Protection Regulation (GDPR) (CELEX 32016R0679)
>     Signal: processing cardholder and merchant personal data as part of
>     the SaaS platform triggers general EU data-protection obligations.
>     Status: unknown
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
> Every candidate that had a determinable CELEX (PSD2, GDPR, MiCA) is shown
> as `unknown`; DORA's "not checked — no CELEX" is unchanged from the
> happy-path example, since it was never going to be sent to the tool
> regardless of connector state. The assessment is still produced in full —
> the connector failure never aborts it.
