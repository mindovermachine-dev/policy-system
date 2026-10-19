# Policy System User Guide

## Table of Contents

- [Using ps-cli](#using-ps-cli)
  - [Install ps-cli](#install-ps-cli)
  - [Point ps-cli at your instance](#point-ps-cli-at-your-instance)
  - [Load curated content](#load-curated-content)
  - [Find and ingest a regulation from EUR-Lex](#find-and-ingest-a-regulation-from-eur-lex)
- [Using Claude Desktop - Code](#using-claude-desktop---code)
  - [Ask a question](#ask-a-question)
  - [Manage access roles](#manage-access-roles)
  - [Invite a new user](#invite-a-new-user)
  - [Review the audit trail](#review-the-audit-trail)
  - [Clean up duplicate graph nodes](#clean-up-duplicate-graph-nodes)
  - [List what is already ingested](#list-what-is-already-ingested)
  - [Review near-miss merge candidates](#review-near-miss-merge-candidates)
  - [Author a policy](#author-a-policy)
  - [Assess EU-instrument applicability](#assess-eu-instrument-applicability)
- [Role System](#role-system)
- [Glossary](#glossary)
- [Appendix: ps-cli reference](#appendix-ps-cli-reference)
  - [Command reference](#command-reference)
  - [Configuring which PS Service instance ps-cli targets](#configuring-which-ps-service-instance-ps-cli-targets)
    - [Single target (default)](#single-target-default)
    - [Multiple named targets (contexts)](#multiple-named-targets-contexts)
    - [Credential storage](#credential-storage)
  - [Running commands](#running-commands)
  - [Troubleshooting](#troubleshooting)
  - [Uninstalling](#uninstalling)
  - [Configuration reference](#configuration-reference)

This guide is for people who **use** an instance of Policy System that is already
deployed. With Claude Desktop, you ask compliance questions and ingest regulations.
With `ps-cli`, you ingest internal policies and administer an instance.

- For an overview of the project, see [README.md](../../README.md).
- To build, test, or release the project, see [CONTRIBUTING.md](../../CONTRIBUTING.md).
- To deploy a new instance, see the [Installation Guide](./installation-guide.md).
- To upgrade, rotate credentials, back up, or tear down an instance, see the
  [Operations Guide](./operations-guide.md).

| I want to... | Use |
| --- | --- |
| Ingest an internal policy, check service health, administer an instance | [Using ps-cli](#using-ps-cli) |
| Ask a compliance question in natural language, or ingest a regulation by CELEX | [Using Claude Desktop - Code](#using-claude-desktop---code) |
| Grant, revoke, or list access roles | [Role System](#role-system) |
| Invite a new user | [Invite a new user](#invite-a-new-user) |
| Review who did what, to what, and when | [Review the audit trail](#review-the-audit-trail) |
| Merge duplicate capabilities or obligations, release a capability from a draft policy, or reverse a merge | [Clean up duplicate graph nodes](#clean-up-duplicate-graph-nodes) |
| See which instruments are already in the graph | [List what is already ingested](#list-what-is-already-ingested) |
| Decide whether two near-identical entities are the same | [Review near-miss merge candidates](#review-near-miss-merge-candidates) |
| Draft a Policy, Standards and Controls | [Author a policy](#author-a-policy) |
| Find out which EU regulations might apply to a company | [Assess EU-instrument applicability](#assess-eu-instrument-applicability) |

---

## Using ps-cli

`ps-cli` is a command-line client for Policy System. This section shows the steps
that a new user does: install it, point it at an instance, load some content, and
ingest a regulation. For all the commands and the advanced configuration, see the
[Appendix](#appendix-ps-cli-reference).

### Install ps-cli

```bash
curl -fsSL https://raw.githubusercontent.com/mindovermachine-dev/policy-system/main/ps-cli/install.sh | bash
```

```bash
ps-cli --version
```

For the prerequisites and for what this command does, see
[Installation Guide: Install ps-cli](./installation-guide.md#6-install-ps-cli). It
installs a release that is verified with a checksum. It uses `uv`, and you do not
need a clone.

### Point ps-cli at your instance

**Evaluator instance**: PS Service listens on `http://127.0.0.1:8000`. This is a local
`kind` deployment (see [Installation Guide: Evaluator
installation](./installation-guide.md#evaluator-installation)). It **requires login**
through the bundled Authentik. Authentik uses HTTPS with a certificate that was issued
locally. `ps-cli` verifies the certificate against the operating system trust store.
On the machine that ran `scripts/deploy-ps-eval.sh --apply-host-setup`, that script
has already added the local CA to the trust store. Therefore, you do not need an
environment variable. On another machine, first trust the CA certificate (see
[Installation Guide, step
8](./installation-guide.md#8-register-your-passkey-and-log-in-with-ps-cli)).

```bash
ps-cli config set-context eval --url http://127.0.0.1:8000
```

```bash
ps-cli config use-context eval
```

```bash
ps-cli auth login
```

The account that you sign in with must first have a passkey. The owner registers a
passkey from the link that the deploy script printed. Everyone else registers a passkey
from an invitation (see [Invite a new user](#invite-a-new-user)). Neither person
sets a password.

**Production instance**: first find the URL. The person who ran
`scripts/deploy-ps-prod.sh` has it. The script prints it at the end of that run
(`PS Service: https://<label>.<region>.cloudapp.azure.com`).

If you do not have the URL, run `scripts/deploy-ps-prod.sh` again. The script is
idempotent. It makes no changes if nothing is different. It always prints the URL
again, also on that run with no changes. Then:

```bash
ps-cli config set-context prod --url https://<label>.<region>.cloudapp.azure.com
```

```bash
ps-cli config use-context prod
```

```bash
ps-cli auth login
```

`auth login` runs an OIDC device-authorization flow. `ps-cli` prints a verification
URL and a code. You complete the sign-in in a browser. `ps-cli` then stores the token
under the current context. Both the evaluator instance and the production instance
are deployed with the bundled Authentik identity provider. Therefore, this step is
necessary on both.

Each later `ps-cli` command uses the context that is current. For how to switch
between multiple environments, how to override a context for one command, and how
credentials are stored, see the
[Appendix](#configuring-which-ps-service-instance-ps-cli-targets).
You configure the `ps-mcp` connector of the Claude Desktop plugin separately, with
`PS_MCP_URL` (see [Using Claude Desktop - Code](#using-claude-desktop---code)).

### Load curated content

The `ps-get-catalog-listing` skill (see [Using Claude Desktop](#using-claude-desktop---code))
lists every curated instrument that the configured curated-content source of PS Service
currently serves. The list includes external and internal instruments. You do not need
a local checkout. Ask Claude to use the skill, for example
_"Use the ps-get-catalog-listing skill to list the curated catalog."_

Restore an instrument from its pre-ingested artifact. This is faster than a full run of
the ingestion pipeline. Use the id that the listing printed. The `ps-restore-instrument`
skill fetches the artifact and restores it. It needs only the `instrument_id`:
_"Use the ps-restore-instrument skill to restore CRA-1.0."_ This loads the curated
instrument that is registered under that id. The instrument can be external or
internal, for example an Engineering Practices standard.

Sometimes a restored Capability nearly matches a Capability that is already in the
graph. The restore then records a near-miss review and does not merge them. It reports
how many reviews it opened as `near_miss_reviews_created`. Resolve them with the
`ps-near-miss-review` skill (see
[Review near-miss merge candidates](#review-near-miss-merge-candidates)).

A freshly deployed instance has an empty graph. It answers nothing until you restore
or ingest something. This action requires `ComplianceOfficer`. See
[Role System](#role-system).

### Find and ingest a regulation from EUR-Lex

Not every regulation is curated. You ingest a regulation by its CELEX identifier with
the `ps-ingest-regulation` skill (see [Using Claude
Desktop](#using-claude-desktop---code)). This applies to curated and not curated
regulations. You do not use `ps-cli` for this. Ask Claude to ingest the regulation, for
example _"Use the ps-ingest-regulation skill to ingest 32016R0679 as gdpr."_

The skill always asks for both the CELEX identifier and a `short_name`. It asks for
both also for a curated regulation. Do not guess the `short_name` for the user. The
skill changes the `short_name` to upper case (`cra` is stored as `CRA`). The skill
rejects the ingestion in these cases:

- That CELEX is already ingested in the graph, under any `short_name`.
- A different CELEX already uses the `short_name`.

The skill submits the run. Then it tells you each time the pipeline moves to its next
stage (ingestion, extraction, derivation, merge). It does not wait in silence. A full
run typically takes several minutes. Sometimes a run is longer than the poll budget of
the skill (20 status checks). The skill then gives you the run id. Later, ask it to
"check run <run_id>". This picks the run up again, and you do not submit it again.

To find the CELEX identifier of a regulation:

1. Go to [eur-lex.europa.eu](https://eur-lex.europa.eu).
2. Search by title or keyword (for example "general data protection regulation").
3. Open the page of the regulation.
4. Switch to its **Document information** tab.
5. Read the **CELEX number**. It is a 10-character code such as `32016R0679`.

If the CELEX is not in the curated set, the skill resolves it directly against
Cellar/ELI. Cellar/ELI is the public document repository of the EU.

The skill runs the full pipeline. This takes minutes, not seconds (a full CRA ingestion
took about 10 minutes from start to end when measured). The FalkorDB and the LLM
interface of the target PS Service instance must be configured. The skill reports the
resolved `regulatory_instrument_id` and the outcome of each pipeline stage. If this
exact identifier is already fully ingested, the skill reports that immediately and runs
no pipeline stages. If a step fails, the skill reports a specific named error. This
action requires `ComplianceOfficer`. See [Role System](#role-system).
`ps-cli ingest document` (ingestion of internal documents, below) also requires
`ComplianceOfficer`.

---

## Using Claude Desktop - Code

Install the Policy System Plugin (see [Installation Guide: Install the
Policy System Plugin](./installation-guide.md#9-install-the-policy-system-plugin))
with:

```text
/plugin install ps-plugin@ps-marketplace
```

The Policy System MCP connector (`ps-mcp`) of the plugin reaches the PS Service in `PS_MCP_URL`
(see [Install the Policy System Plugin](./installation-guide.md#9-install-the-policy-system-plugin)).
The connector can need a moment to connect. If a query fails, try again.

To see which Claude surfaces can reach your instance, and which are verified, see [Which Claude surfaces reach your deployment](./installation-guide.md#which-claude-surfaces-reach-your-deployment).

### Ask a question

```text
What obligations does the Cyber Resilience Act place on manufacturers,
and which of our policies cover them?
```

The `ps-qna` skill first states, in plain language, what it understands that you ask
and the scope of the question. It asks you to confirm or correct this. The skill
queries nothing until you confirm. After you confirm, it queries the live knowledge
graph, verifies the answer, and prints short status lines while it works. The answer
cites a source reference for each claim. It ends with one line that confirms the graph
was queried live and says if the answer held up under verification. If the graph cannot
answer, the skill says so. It does not fill the gap from model recall. To see the work
behind the answer, ask for the underlying query, the retrieved data, or the
verification detail.

If the skill does not engage on its own, ask for it by name: _"Use the ps-qna skill."_

### Manage access roles

```text
List everyone's access roles.
```

The `ps-manage-access-roles` skill lists, grants, and revokes `SystemOwner`,
`SystemAdmin`, and `PolicyManager` assignments through the same MCP
connector. Before it does a grant or a revoke, it confirms the exact principal and
role with you. A grant or a revoke writes a permanent, audited change. It is never a
plain read. For what each role controls at this time, see [Role System](#role-system).

If the skill does not engage on its own, ask for it by name: _"Use the
ps-manage-access-roles skill."_

### Invite a new user

```text
Invite jane.doe@example.com to Policy System.
```

The `ps-invite-user` skill creates a single-use Authentik enrollment invite
for a confirmed target email through the same MCP connector. Before it calls the tool,
it confirms the exact address with you. This creates a live, audited invite. It is
never a plain read. The skill reports the `itoken` and the `invite_url`. You give
these to the invitee yourself. The system does not send an email. This action
requires `SystemAdmin` or above (`SystemOwner` counts). See [Role
System](#role-system).

The invitee opens the link, enters a username, a name, and an email, and registers a
passkey. There is no password field, and the account has no password. On the evaluator
instance, the machine of the invitee needs the same CA trust and hostname mapping.
These are the same as on the machine of the owner (see [Installation Guide, step
8](./installation-guide.md#8-register-your-passkey-and-log-in-with-ps-cli)).
On production, `invite_url` has the public address only when the deployed PS Service
release supports `PS_AUTHENTIK_PUBLIC_URL`. See [Installation Guide: Verification
status](./installation-guide.md#verification-status).

If the skill does not engage on its own, ask for it by name: _"Use the
ps-invite-user skill."_

### Review the audit trail

```text
Show me the most recent access-role changes.
```

The `ps-list-audit-events` skill reads the shared audit
trail of PS Service (`audit_log.audit_events`) through the same MCP connector. You can
filter by actor, resource, action, or time range. The skill shows the newest rows
first and gives them in pages. It is a plain read and needs no
confirmation. It requires `SystemAdmin` or above. See
[Role System](#role-system).

What is recorded:

| Operation | Rows | Notes |
|---|---|---|
| Access-role changes, policy lifecycle, graph cleanup | `access_role.*`, `policy.*`, `capability.*`, `obligation.*` | Applied and denied attempts |
| Regulation ingestion (`ingest_regulation`, `start_ingestion`, and each re-ingest of `check_regulations`) | `ingestion_run.submit` before the run, `ingestion_run.complete` after | The CELEX id, the instrument id, `trigger` (`sync_ingest`, `async_ingest`, `amendment_check`), the counts of new Obligations, new Capabilities and matched Capabilities, and a `reason_code` on failure. Polling a run writes nothing. A `check_regulations` sweep runs the full pipeline (Ingestion, Domain Mapper, Company Merge) for each amendment. Its completion row therefore has the real counts of the amendment |
| Instrument restore (`restore_instrument`, `POST /restorations`) | `instrument.restore` opening and terminal rows | The instrument id, the source (`catalog` or `upload`) and the outcome: `succeeded`, `failed` (with a `reason_code`), `rejected` (`group_rejected`: the graph write was refused and nothing was written) or `committed_apply_pending` (the restore is recorded in the graph log and is applied on recovery). The `succeeded` row also has `near_miss_reviews_created`. This is the number of near-miss reviews that the restore opened. The `committed_apply_pending` row does not have it |
| Near-miss resolution | `near_miss.resolve` (keep-separate), `capability.merge` (merge) | A keep-separate row has both entity ids and the decision. A near-miss merge writes the same `capability.merge` row as a graph cleanup merge, when the approver signs with their passkey. The approver is the actor. The row has the survivor, the absorbed Capability, the review id and before/after graph snapshots |
| `invite-user` | `user.invite` | The invitee email. The invite token and URL are never recorded |
| A role-gated tool or route refusing a caller | `access.gate` | The denied caller, the operation that the gate refused (`resource_id`, for example `set-catalog-source` or `POST /change-checks`) and the role that the operation requires. The system writes this row only for a caller whose identity is verified. `grant-access-role` and `revoke-access-role` keep their own `access_role.*` denied row |
| Passkey enrolment | `passkey.enroll` | The person the approval is bound to and the approval id. The row records that the passkey was added. It does not hold the passkey itself. Signing a change writes no row of its own. The row of the change (for example `capability.merge`) is the record |
| `cypher` | `query.cypher` | One row for each call. The row has the caller, the outcome (succeeded, was rejected for a write clause, or failed), the row count, and if the result was truncated. The system never records the query text |

The system does not record these items:

- Sign-in and failed sign-in. The event log of your identity provider is the record.
- Denials of a request that has no verified identity.
- Rejected passkey steps. Passkey rejections stay in the operator log of the service.
- The other read-only tools.

If the audit trail cannot record the opening row, the operation does not run.
You get `error: The audit trail is temporarily unavailable; the operation
was not performed.` (HTTP 503 over REST). For a merge that a passkey approved, the
system has already used the single-use approval at that point. Request a new
approval. If the system cannot write the closing row, it only logs this. The result
does not change. Rows never contain tokens, URLs, error text, stack traces
or file paths. Failures have an enumerated `reason_code`.

To answer "who ingested X?", filter with `details`, for example
`{"celex": "32024R2847"}`, `{"regulatory_instrument_id": "CRA-1.0"}` or, for
restores, `{"instrument_id": "CRA-1.0"}`. The system rejects any other `details` key.
Sometimes the identity check rejects a synchronous ingest (unknown CELEX,
short-name collision, already ingested). The audit trail then still has an opening row
and a terminal row. Sometimes the identity check rejects an asynchronous
`start_ingestion` at that step. The audit trail then has no row.

If the skill does not engage on its own, ask for it by name: _"Use the
ps-list-audit-events skill."_

#### Verify the audit log

```text
Check that the audit log has not been tampered with.
```

The `ps-verify-audit-log` skill calls the `verify-audit-log` tool, which takes
no arguments. The tool recomputes the hash chain that links each audit row to the
row before it. It compares the last row with the recorded head. It is a plain
read that writes nothing. It requires `SystemOwner` or `SystemAdmin` and a
real authenticated session. An intact result reports `rows_checked` and
`head_chain_seq`. A break is reported with its kind (`row_hash_mismatch`,
`prev_hash_mismatch`, `sequence_gap` or `head_mismatch`). Except for
`head_mismatch`, the report also gives the `chain_seq`, the id, and the time of the
row. Treat a break as a security finding and tell your operator.

The check is tamper-evident, not tamper-proof. It does not detect a database
superuser who rewrites the whole chain and its head consistently. For the side of the
operator, see the
[Audit log integrity](./operations-guide.md#audit-log-integrity) section of the
Operations Guide. If the skill does not engage on its own, ask for it by name:
_"Use the ps-verify-audit-log skill."_

### Clean up duplicate graph nodes

```text
Find duplicate capabilities in the graph.
```

Ingestion can leave the same duty in the graph twice. There are two cases:
near-identical Capabilities (for example several "Vulnerability Remediation" variants)
or two Obligations with the same text under one Role. Duplicates increase the
obligation counts and split the policy coverage across the variants. With the
`ps-graph-cleanup` skill, a Compliance Officer can find duplicates, merge them, and
reverse a merge. The skill uses the same MCP connector.

Whether two nodes are the same duty is a judgement about regulatory meaning. Therefore,
a human decides every change. You confirm the change in your own words and sign it with
a passkey. The skill requires an explicit `ComplianceOfficer` grant on a real signed-in
session. A `SystemOwner` or `SystemAdmin` without that grant is denied. The
local-test bypass never satisfies the requirement. See [Role System](#role-system).

**Discovery (read-only).** `find-capability-merge-candidates` returns groups of
similar active Capabilities. Each member shows its id, name, obligation count and
governing policy (or none). Each group shows how the tool matched it and its merge
case. The match is `name` or `embedding` similarity. Embedding similarity is a looser
signal, so read the names before you treat a group as a duplicate. A Capability with
no cached embedding can only match by name.

`find-duplicate-obligations` returns groups of Obligations under the **same** Role
with identical or near-identical text. Each Obligation has the `source_ref` of its
Requirements. Use it to check if the regulation really states the duty twice.
Identical wording under two different Roles is two duties. The tool never offers
such a pair. Neither tool changes anything.

**Merging two Capabilities.** Pick which Capability survives and which one the merge
absorbs. The tool shows a preview of the edges that move to the survivor, the
obligations that the merge affects, and the policy case. It creates a passkey approval
that is bound to that exact pair and state. Confirm, open the approval link, and sign
with your passkey. The approval is valid for 15 minutes. Then the skill checks the
outcome.

The merge deletes the absorbed Capability. A `MergedCapability` marker that names the
survivor stays in the graph. A later ingest or restore can regenerate its name. That name then attaches to the survivor, and the system does not
recreate the duplicate. The case that applies depends on the governing policies:

| Case | Situation | What you do |
| --- | --- | --- |
| 1 | Neither Capability has a governing policy | Confirm, then sign. |
| 2 | Exactly one has a governing policy | The first call returns the preview only. Acknowledge the governance change. The policy then governs the survivor and not the absorbed Capability. For an approved policy, its governed set changes, but its content and version do not change. Then the call returns the approval to sign. The acknowledgment is part of what you sign and of the audit record. |
| 3, same policy | Both are governed by the same policy | Confirm, then sign. The absorbed Capability leaves the governed set of that policy. |
| 3, different policies | Both are governed, by different policies | Blocked before any approval exists. Release the absorbed Capability from its policy first (below). |

**Merging two Obligations.** You can merge only two Obligations under the same Role.
The tool rejects a pair across two Roles. The flow is the same (preview, confirm,
passkey). The survivor keeps its single link to the Role. It gets the requirement links
and capability links of the absorbed Obligation. The merge **deletes** the absorbed
Obligation. The audit record keeps its full node and edge snapshot. A small
`MergedObligation` marker stays. A later ingest or restore then attaches to the
survivor, and the system does not recreate the absorbed Obligation. The marker is not
an Obligation. The system never counts it as one.

**Releasing governance.** `release-capability-governance` removes the link between one
Capability and its governing policy. After this, a case 3 merge can continue. It works
only while that policy is a `draft`. You can first return a `proposed` policy to draft
with the revert step of the `ps-policy-lifecycle` skill. The tool rejects an `approved`
or `deprecated` policy and points to the policy lifecycle. It changes no Policy,
Standard or Control.

**Reversing a merge.** `unmerge` takes the id that the merge absorbed. It restores that
id from the snapshot in the audit record of the merge. It first does the same preview,
confirmation and passkey approval. The tool recreates a deleted Capability or
Obligation under its original id and removes its marker. The tool restores exactly the edges that the merge
moved. Edges that you added to the survivor after the merge stay where they are. The
preview lists them.

Sometimes the survivor has changed since the merge in a way that conflicts. For
example, it was merged away, its governance changed, or a linked node is gone. The tool
then rejects the unmerge with an explanation. It forces nothing.

**Passkey approval and interrupted approvals.** Nothing changes in the graph until
you sign. Each change writes one audit record **before** the system changes the graph.
The record has the actor, the action, both ids, the policy case, the acknowledgment,
the approval id and a before/after snapshot. If the system cannot write the audit
record, it does not change the graph.

The graph and the audit trail are separate stores. A graph failure after the audit
record leaves an `applied` row and then a `failed` row for the same approval id. That
pair means that no edit occurred.

Sometimes the run is interrupted after you sign, before the system records the outcome.
Ask the skill to check the approval again. When it is more than five minutes past
expiry, the check looks for the change in the graph. If the change is there, the check
records it as applied. If the change is not there, the check records a `failed` row
(`interrupted_no_effect`). Read the records with [Review the audit
trail](#review-the-audit-trail). Filter by the actions `capability.merge`,
`obligation.merge`, `capability.release_governance`, `capability.unmerge` and
`obligation.unmerge`.

**Known limitation.** You cannot yet merge two Capabilities that two **approved**
policies govern. The policies are different. Releasing works only on a draft. You amend
an approved policy by forking it, but a fork carries the whole governed set, so it
cannot drop one Capability. The tool says this plainly and does not offer a workaround.
A separate item tracks a follow-on change that lets a fork draft drop a single
Capability.

**Merged duplicates and raw Cypher.** An absorbed Capability is deleted, so a query for
Capabilities never returns it and needs no status filter for merges. If you query the
graph directly with the `cypher` tool, the `MergedCapability`, `MergedObligation` and
`MergedRole` nodes (each with `id` and `merged_into`) record which node replaced an
absorbed id. The packaged domain concepts describe the markers.

If the skill does not engage on its own, ask for it by name: _"Use the
ps-graph-cleanup skill."_

### List what is already ingested

```text
Use the ps-list-ingested skill to show what is in the graph.
```

The `ps-list-ingested` skill lists each regulatory instrument that is in the compliance graph at this time. It reads from the live graph and not from the curated catalog. Use it before you ingest, restore or author policy. It shows what is already there.

### Review near-miss merge candidates

```text
Use the ps-near-miss-review skill to show the pending near-miss reviews.
```

Sometimes ingestion or a restore finds two entities that are close but not identical. It then records a near-miss pending review and does not merge them. The `ps-near-miss-review` skill lists the pending reviews. It resolves a review when you keep the two entities separate or merge them.

A merge needs the `ComplianceOfficer` role. It is the same Capability merge as in graph cleanup, so it does not run at the call. The skill returns the merge preview and an approval link. The merge runs when you complete a passkey signing in the browser. The skill can then report if the approval was signed. The absorbed Capability is deleted behind a `MergedCapability` marker that points at the survivor, and its edges move to the survivor. The system therefore does not raise the pair again on a later ingest or restore.

If a Policy governs exactly one of the two, you must acknowledge the governance change. If they have different governing Policies, the system refuses the merge until you release one with `release-capability-governance`. You can reverse a merge with `unmerge`. This does not bring back the deleted review.

Resolve the near misses of an instrument before you export it. The system refuses an export while any unresolved review touches the Capabilities of the instrument.

### Author a policy

```text
Use the ps-author-policy skill to draft a policy for the capability "Vulnerability Remediation".
```

The `ps-author-policy` skill checks if a governing Policy already exists for the Capabilities that you name. It then guides you through the Policy, its Standards and their Controls, one rubric criterion at a time. This continues until the rubric-weighted score of each document is above its pass threshold. The `ps-policy-lifecycle` skill moves a finished draft through propose, approve, reject and revert to draft.

### Assess EU-instrument applicability

```text
Assess which EU regulations might apply to [company].
```

The `ps-assess-instrument-applicability` skill asks you about the markets and
geographies, and the products and services, of a company. It repeats them to you for
confirmation. Then it produces a tiered candidate list of EU regulatory instruments
(likely, possible, excluded). The list comes from general regulatory knowledge. It
never comes from the graph or a curated catalog. The skill checks the CELEX of each
candidate against the same MCP connector, to see if it is already ingested. The result
always has a prominent disclaimer. The result is non-authoritative and can be
incomplete. It is not a substitute for legal or compliance review.

If the skill does not engage on its own, ask for it by name: _"Use the
ps-assess-instrument-applicability skill."_

---

## Role System

Each authenticated caller is automatically a member of `AuthenticatedUser`.
There is no explicit grant for it, and you cannot revoke it for one caller.
In addition to that baseline, PS Service enforces four more roles:

| Role | How it's granted | What it currently gates |
| --- | --- | --- |
| `SystemOwner` | Automatically, one time. The first authenticated caller whose identity (`sub` + `iss`) matches the identity that the operator configured at deploy time gets it. The setting is `psService.authzBootstrapOwner`. The deploy scripts set it to the email of the owner. The first caller is not the owner just because that caller calls first. Any other caller who reaches an empty instance first gets nothing. You cannot grant it afterward. Exactly one `SystemOwner` exists for the life of a real deployment. To see how an operator claims it, see [SystemOwner bootstrap](./installation-guide.md#systemowner-bootstrap). | Everything that `SystemAdmin` gates, and the grant and revoke of `SystemAdmin`. |
| `SystemAdmin` | A `SystemOwner` grants or revokes it. | The catalog-source tools (`set-catalog-source`, `reset-catalog-source`, `get-catalog-source`), `list-access-roles`, `invite-user`, `list-audit-events`, and `verify-audit-log`. |
| `PolicyManager` | A `SystemOwner` or `SystemAdmin` grants or revokes it. | Nothing yet. It is provisioned ahead of future policy-authoring features. No tool checks for it at this time. |
| `ComplianceOfficer` | A `SystemOwner` or `SystemAdmin` grants or revokes it. | `POST /restorations`, `POST /restorations/from-catalog`, `POST /exports`, `POST /change-checks`, `POST /ingestions` (either `source`), and the MCP tools `ingest_regulation`, `start_ingestion`, `get_ingestion_status`, `restore_instrument`, `check_regulations`, and the graph cleanup tools (`find-capability-merge-candidates`, `find-duplicate-obligations`, `merge-capabilities`, `merge-obligations`, `release-capability-governance`, `unmerge`, `check-cleanup-approval`; see [Clean up duplicate graph nodes](#clean-up-duplicate-graph-nodes)). There is no hierarchy override. The gate also denies a `SystemAdmin` or `SystemOwner` without an explicit grant. |
| `AuthenticatedUser` | Automatic for every authenticated caller. | Asking questions through Claude Desktop ([Ask a question](#ask-a-question)). No elevated role is necessary. |

Two safety rules apply to each grant or revoke, for all roles:

- **No self-service.** A caller can never grant or revoke their own access
  roles.
- **The SystemOwner floor.** The system blocks the revoke of `SystemOwner` if
  it would leave zero active `SystemOwner`s. In practice, the
  sole `SystemOwner` can never revoke themselves. The no-self-service rule
  blocks this first, before the system gets to the floor check.

The system permanently records each grant, each revoke, and the initial bootstrap in an
audit trail (`audit_events`). The record shows who did what, to whom, and when. It
includes denied attempts. The audit trail is separate from the roster of the current
state. Manage roles with the [Manage access roles](#manage-access-roles) skill of Claude
Desktop. Read the audit trail with [Review the audit trail](#review-the-audit-trail).

---

## Glossary

[`docs/artifacts/ps-domain-concepts.md`](./ps-domain-concepts.md) defines the entities,
the relationships, and the vocabulary that this guide uses (Regulatory
Instrument, Obligation, Capability, Control, and others).

---

## Appendix: ps-cli reference

This appendix gives all the commands and the configuration options for `ps-cli`. For
most daily use, you only need the steps in [Using ps-cli](#using-ps-cli) above.

### Command reference

Global flags. You can use them before or after any subcommand:

| Flag | Description |
| --- | --- |
| `-v`, `--verbose` | Print the failure site (file:line) when an error occurs. |
| `--context <name>` | Use the PS Service URL of this named context for this one command only. The system never saves it. |
| `--version` | Print the versions of the PS-CLI client and of PS Service, and exit. |

| Command | Arguments | Description |
| --- | --- | --- |
| `ps-cli get health` | — | Report the reachability, the health (`/health`), and the readiness (`/ready`) of the configured target. When you are logged in, the report names any unhealthy dependency; without a login it shows the readiness status only. Readiness depends on FalkorDB, PS state Postgres and the startup graph replay. The report also names an unhealthy LLM Interface or Cellar/ELI when there is one. Such a dependency does not by itself make the target not ready. |
| `ps-cli ingest document <document_path>` | `document_path` — the path of a local `.json` file. `ps-cli` reads it from your own machine and sends its content | Ingest an internal policy document. Requires `ComplianceOfficer`. |
| `ps-cli export instrument <instrument_id> [destination]` | `instrument_id` — the already-ingested instrument's id (for example `CRA-1.0`). `destination` — an optional local directory. The default is the current directory | Export the baseline, native, and manifest files of an already-ingested instrument to a local destination. |
| `ps-cli auth login` | — | Log in to the current context with the OIDC device flow (see [Credential storage](#credential-storage)). |
| `ps-cli auth status` | — | Verify the stored credential of the current context when it refreshes the credential. Show its login status (context, issuer). The command exits non-zero unless the credential works. It reports each of these cases separately: not logged in, credential not usable, cannot verify (IdP or PS Service unreachable), and credential store not accessible. |
| `ps-cli auth logout` | — | Remove the stored credential of the current context. |
| `ps-cli config set-context <name> --url <url>` | `name`, `--url` (required) | Create or update the PS Service URL of a named context. It clears any credential that was stored before for that name. |
| `ps-cli config use-context <name>` | `name` | Select the named context that each later command uses. |
| `ps-cli config get-contexts` | — | List each named context and mark the current one. |

To get the same reference from the CLI itself, run `ps-cli --help` or
`ps-cli <command> --help`.

### Configuring which PS Service instance ps-cli targets

#### Single target (default)

By default, `ps-cli` targets `http://127.0.0.1:8000`. This is the same as the default
of PS Service. To point it at a different target, use the `PS_CLI_SERVICE_URL` env var.
You can also put a `ps-cli.toml` (`service_url = "..."`) in your current directory:

#### Multiple named targets (contexts)

If you often switch between environments (dev, test, prod), `ps-cli` supports
named contexts. This is like the contexts of `kubectl` or the subscriptions of `az`.
The whole CLI, not only the contexts, follows the `<verb> <resource>` pattern of
kubectl. See [Command reference](#command-reference) above.

```bash
ps-cli config set-context dev --url https://dev.example.com
```

```bash
ps-cli config set-context prod --url https://prod.example.com
```

```bash
ps-cli config use-context prod
```

```bash
ps-cli config get-contexts
```

When a context is current, each command uses it:

```bash
ps-cli get health
```

Targets prod.

**Override for a single command.** Use `--context`. The current context does not change:

```bash
ps-cli --context dev get health
```

Targets dev, for this one command only.

```bash
ps-cli config get-contexts
```

Still shows prod as the current context.

**Resolution order** (the highest wins): `PS_CLI_SERVICE_URL` env var > `--context` flag >
the current context in your config > the single-target fallback above.

By default, `ps-cli` stores contexts in `targets.toml` under `~/.config/ps-cli/`. To
change the location, use `PS_CLI_CONFIG_DIR`. This is like `GH_CONFIG_DIR` of `gh`. Use
it if you want an isolated config, for example for testing:

```bash
PS_CLI_CONFIG_DIR=/tmp/my-ps-cli-config ps-cli config get-contexts
```

`targets.toml` holds only context names and URLs. It never holds a credential.

#### Credential storage

`ps-cli` stores credentials only in your OS keyring, with one key for each context
name. It saves only a `refresh_token` and its `issuer`. It never writes the access
token that it gets from the refresh token to the keyring or to disk. The access token
stays in memory for the current `ps-cli` invocation only. `ps-cli` refreshes it one
time for each invocation and discards it when the process exits. Sometimes the OS
keyring is not available, or a keyring operation fails. `ps-cli` then raises an error
that you can act on. The error names the context and the failure type. It never shows
the credential value. `ps-cli` does not write a file as a fallback.

Log in with `ps-cli auth login`. It needs a named context, because the system keys a
stored credential to the context name. First run `config set-context` and
`config use-context`. The command runs an OIDC device-authorization flow. `ps-cli`
prints a verification URL and a code. You complete the sign-in in a browser. `ps-cli`
then stores the token under the current context.

`ps-cli auth status` verifies the stored credential when it refreshes the credential
with the identity provider. "Logged in" therefore means that the credential works. The
command exits non-zero when it does not work. `ps-cli auth logout` removes the stored
credential.

The target PS Service instance decides if login is required. The server implements
generic OIDC bearer-token validation against any OIDC-compliant provider. It does not
assume the IdP of one vendor. A deployment enforces this validation when you configure
it with an issuer and an audience. The evaluator deployment (see [Installation Guide:
Evaluator installation](./installation-guide.md#evaluator-installation)) and the
production deployment both enforce it. Therefore, each `ps-cli` command needs
`ps-cli auth login` first.

The `ps-mcp` connector of the plugin authenticates through `/mcp` Authenticate instead
(see [Using Claude Desktop](#using-claude-desktop---code)). Only a deployment that you
start with the local-test bypass (`PS_SERVICE_LOCAL_TEST_BYPASS`, which cannot run in a
container) needs no login. Run `config set-context` again for an existing context name
with a new `--url`. This always clears any credential that was stored before for that
name. The system never carries a credential over to a new URL without a notice.

### Running commands

The curated catalog listing and the restore-from-catalog are both outside `ps-cli`
now. Use the `ps-get-catalog-listing` and `ps-restore-instrument` skills (see
[Load curated content](#load-curated-content)). They are not `ps-cli` subcommands.

```bash
ps-cli ingest document <document_path>.json
```

The command reads the file locally and sends its content. (To ingest a regulation by
CELEX, use the `ps-ingest-regulation` skill. Do not use `ps-cli`. See [Find and
ingest a regulation from EUR-Lex](#find-and-ingest-a-regulation-from-eur-lex).)

The command imports the Policy, Standard and Control nodes in the document as `draft`.
It does this for any `status` that they have. You (the signed-in user) own each Policy.
This is the same as when you restore a curated internal instrument. The command needs a
signed-in user who holds `ComplianceOfficer`. Without a signed-in user, or without the
role, it fails with HTTP 403 and writes nothing. A re-ingest does not change nodes that
already exist (for example a Policy that is approved since then). The merge stage
summary reports how many Policies, Standards and Controls were imported as drafts. It
also reports how many authored statuses were overridden. Take the drafts through the
Policy Lifecycle to approve them.

`ingest document` uses the full pipeline. The PS Service instance that you target
therefore needs FalkorDB and its LLM interface configured. If a command fails and you do
not know why, first check the `/ready` endpoint of the instance, for example with `ps-cli get health` while logged in (see
[Troubleshooting](#troubleshooting) below).

Know these behaviors of `export instrument`:

- If you omit `destination`, it is your current working directory.
- The destination must already exist and be writable. `ps-cli` checks this before it
  makes a call to PS Service. A bad path therefore fails fast with an error that you can
  act on. It does not fail after a wasted round trip.
- If you run the command again against the same destination, it overwrites
  `baseline.json`, `native.json`, and `manifest.json` in a deterministic way. There is
  no merge or append behavior.
- You must resolve near misses before an export. Sometimes an unresolved near-miss
  review touches one of the Capabilities of the instrument. PS Service then refuses
  with HTTP 409. `ps-cli` prints the message (the count and the review ids), exits
  non-zero, and writes no files. Resolve the reviews with the `ps-near-miss-review` skill
  (see [Review near-miss merge candidates](#review-near-miss-merge-candidates)). Then
  export again.
- If the exported instrument has an internal source, `ps-cli` prints an explicit
  notice. The notice says that the exported files can contain the confidential policy
  content of your organization.

### Troubleshooting

If you reference a context that does not exist, the command exits non-zero and lists the valid names:

```bash
$ ps-cli config use-context staging
❌ context 'staging' is not defined in targets.toml
💡 valid contexts: dev, prod
```

A malformed `targets.toml` exits non-zero and names the file:

```
❌ /home/you/.config/ps-cli/targets.toml contains invalid TOML: Invalid value (at line 1, column 7)
```

If a command cannot reach PS Service at all, `ps-cli` reports this in a different way
from an unhealthy server:

```
❌ Could not reach PS Service at http://127.0.0.1:8000.
💡 check PS_CLI_SERVICE_URL / ps-cli.toml, and that ps-service is running
```

Sometimes `ps-cli auth login` against an evaluator instance fails with `Could not reach
<issuer>.` while Authentik is running. The issuer uses HTTPS with a certificate that
was issued locally, and `ps-cli` verifies it against the operating system trust store.
If the message says that it could not verify the TLS certificate, the machine does not
trust the local CA. Trust it as described in [Installation Guide, step
8](./installation-guide.md#8-register-your-passkey-and-log-in-with-ps-cli). Then try
again. A plain `Could not reach` message means that the host is not reachable. It is not
a certificate problem. On Linux, `SSL_CERT_FILE` or `SSL_CERT_DIR` also work. On macOS
and Windows, `ps-cli` ignores them.

If this does not help, check the health of PS Service. See the [Operations
Guide: Troubleshooting / FAQ](./operations-guide.md#troubleshooting--faq) for
`/health` and `/ready`.

Note that `ready: ready` does not mean that the LLM Interface or Cellar/ELI are
healthy. Readiness depends only on FalkorDB, PS state Postgres and the startup graph
replay. An LLM Interface outage shows in a different way. It shows to
`ps-cli ingest document` (and to the `ps-ingest-regulation` and
`ps-check-regulations` skills) in the own pre-flight failure message of that command.
This is `❌ LLM Interface is unavailable.` for `ps-cli` and
`error: LLM Interface is unavailable.` for the skills. The message comes before any
pipeline call.

`ps-cli get health` reports all three (reachability, health, and readiness) in one call:

```
$ ps-cli get health
reachable: yes
health: alive
ready: ready
```

```
$ ps-cli get health
❌ PS Service is reachable but not ready (health='alive', ready='not_ready').
💡 unhealthy dependencies: falkordb
```

Without a login PS Service answers the readiness status only, so the report carries no
dependency names and adds the hint to log in to see them:

```
$ ps-cli get health
reachable: yes
health: alive
ready: ready
💡 log in to see dependency detail
```

### Uninstalling

You install `ps-cli` as a `uv` tool. You remove it in the same way:

```bash
uv tool uninstall ps-cli
```

This removes the `ps-cli` executable and its isolated environment. It keeps your
config and any stored credentials, so a reinstall does not lose them. To remove
those also:

```bash
rm -rf ~/.config/ps-cli
```

If you set `$PS_CLI_CONFIG_DIR`, remove that directory instead.

This deletes `targets.toml` (your contexts), if it exists. The system stores credentials
in your OS keyring (see [Credential storage](#credential-storage) above). They are not
under this directory. `uv tool uninstall` cannot see the keyring. Remove the credentials
separately for each context. For example, run `keyring del ps-cli <context-name>`, or
use the keychain or Credential Manager UI of your OS.

You can create a project-local `ps-cli.toml` (see
[Single target (default)](#single-target-default) above). None of the steps above
changes it. Delete it directly where you created it.

### Configuration reference

| Setting | Applies to | Default | Purpose |
| --- | --- | --- | --- |
| `PS_CLI_SERVICE_URL` (env var) | ps-cli | unset | The override with the highest precedence for the PS Service instance that ps-cli targets. |
| `ps-cli.toml` (`service_url`, in current directory) | ps-cli | none shipped | Project-local single-target override. It has the lowest precedence. |
| `PS_CLI_CONFIG_DIR` (env var) | ps-cli | `~/.config/ps-cli/` | The location from which `targets.toml` is read and to which it is written. |
| `targets.toml` (`[contexts]`, `current_context`) | ps-cli | none until `config set-context` is run | Named PS Service targets and the one that is current. It never contains a credential. |

This table covers `ps-cli` only. The plugin has its own settings, below.

### Plugin (`ps-mcp`) settings

| Setting | Applies to | Default | Purpose |
| --- | --- | --- | --- |
| `PS_MCP_URL` (`env` in `~/.claude/settings.json`) | plugin `ps-mcp` | unset | PS Service URL, for example `https://<host>/mcp/`. The trailing slash is part of the URL. |

- **Not set:** `/mcp` shows `ps-mcp` as failed with `Missing environment variables: PS_MCP_URL`.
  Set it in the `env` block of `~/.claude/settings.json`. Then start a new session.
- **`userConfig` is not used.** A `${user_config.*}` value never reached Claude Desktop - Code
  sessions. An unset value drops the server without a notice.
- **Sign-in:** Use `/mcp`, then Authenticate. Authentik shows its consent screen, because Claude Code
  sends `prompt=consent`. Approve it.
- **Same URL:** if a server that you added by hand has the same URL as the plugin server, Claude Code
  does not load the plugin server, and `/mcp` shows no error. Remove one of the two.
- **Versions:** the lowest verified versions are Claude Code CLI 2.1.236 and the Claude Desktop app 2.31226.1
  (bundled Claude Code 2.1.295). Lower versions are untested.

The `ps-cli` rows above do not affect the plugin.

For the configuration of PS Service and of the chart, see the
[Helm Chart Values Reference](./helm-chart-values-reference.md).
