# Policy System Agent Skills

One of the client types users use to access Policy System is Agent Skills. The following Agent Skills are officially supported:

- ps-qna. This skill is used to ask policy related Questions and retrieve Answers (QnA) from the Company Policy Knowledge Graph.
- ps-ingest-regulation. This skill ingests an EU regulation into the Company Policy Knowledge Graph by CELEX identifier, whether or not that CELEX is in the curated catalog, reporting each pipeline stage as it runs.
- ps-check-regulations. This skill sweeps every regulation tracked in the Company Policy Knowledge Graph for detected amendments, automatically re-ingesting any that are found, and reports the per-instrument outcome.
- ps-near-miss-review. This skill lists near-miss pending reviews (Company Merge dedup candidates) awaiting a keep-separate/merge decision in the Company Policy Knowledge Graph, and resolves one by keeping its two entities separate or merging them.
- ps-graph-cleanup. This skill is the Compliance Officer's graph cleanup tool: it finds groups of near-duplicate Capabilities in the Company Policy Knowledge Graph so a human can decide which to merge, merges them (or duplicate Obligations) after a passkey approval, releases a Capability from a draft governing policy, and unmerges a Capability or Obligation merge from its audit snapshot. It requires an explicit ComplianceOfficer grant.
- ps-get-catalog-listing. This skill lists every curated instrument (external and internal) available from the Policy System's configured curated-content source.
- ps-restore-instrument. This skill fetches one curated instrument's artifact from the Policy System's configured curated-content source and restores it into the Company Policy Knowledge Graph, given its instrument_id.
- ps-list-ingested. This skill lists every RegulatoryInstrument already ingested into the Company Policy Knowledge Graph, read from the live graph (unlike ps-get-catalog-listing, which lists the curated source before ingestion), so you can check what is there before deciding to ingest, restore, or author policy.
- ps-assess-instrument-applicability. This skill interviews a Compliance Officer about the company's markets, geographies, products and services, confirms the captured input, and produces a tiered (likely, possible, excluded) candidate list of EU regulatory instruments from general regulatory knowledge, each with its CELEX id when known.
- ps-author-policy. This skill guides a user through authoring a new or amended Policy, Standard and Control tree, detecting whether a governing Policy already exists for the named Capabilities and then walking Policy, Standards and Controls one rubric criterion at a time until each document's own rubric-weighted score clears its pass threshold.
- ps-policy-lifecycle. This skill creates a draft Policy, reads a Policy's full Standard and Control tree, and moves a Policy through its governance lifecycle (propose, approve, reject, revert to draft).
- ps-manage-access-roles. This skill lists, grants and revokes access roles (SystemOwner, SystemAdmin, PolicyManager) for a principal, and surfaces the SystemOwner floor warning.
- ps-list-audit-events. This skill reads the shared audit trail (who did what, to what, and when), filtered by actor, resource, action or time range and paginated newest first. It is restricted to SystemOwner and SystemAdmin and is read-only.
- ps-invite-user. This skill invites a new user by creating a single-use Authentik enrollment invite. It is restricted to SystemAdmin and above.

## Development of Policy System Skills

Agent Skills are shipped as a single git-hosted Claude plugin, not a separate dev/dist split. `ps-skills/ps-plugin/` is the plugin directory itself — its `skills/` subfolder and `.mcp.json` connector are exactly what gets installed, so there is no packaging or build step between editing the skill and using it.

Policy System Agent Skills should not be part of the Policy System repo skill structure as they are not used across the development team to develop Policy System, rather they are part of the product so they should be installed into the local user Agent Skill structure if needed for testing the skills.

To install: add this repo as the Policy System Marketplace (`ps-marketplace`) and run `/plugin install ps-plugin@ps-marketplace` (see `docs/artifacts/installation-guide.md`).
