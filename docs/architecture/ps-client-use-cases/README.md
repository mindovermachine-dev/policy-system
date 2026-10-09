<!-- © 2026 Cartman ApS. All rights reserved. -->
# ps-client Use Cases

Client-level use cases for ps-client, one document per use case, written from
`cuc-template.md`. They sit one level below the system-level URS use cases
(`../ps-primary-use-cases.md`, UC-1..UC-4): each CUC's **Realizes** field names the system
UC it serves, where one exists. The design doc (`../ps-client-ux-design.md`) derives its
contracts from these; its §4 treats this index as the scope ledger.

CUC docs stay at walkthrough altitude: observable two-pane beats and contracts touched —
no component design.

## Index

| CUC | Title | Class | Realizes | v1 | Status |
|-----|-------|-------|----------|----|--------|
| [CUC-01](cuc-01-first-login-and-session.md) | First login & session | — | — | ✓ | Specified |
| [CUC-02](cuc-02-invite-a-user.md) | Invite a user | Admin | — | ✓ | Specified |
| [CUC-03](cuc-03-grant-revoke-an-access-role.md) | Grant/revoke an access role | Admin | — | ✓ | Specified |
| [CUC-04](cuc-04-ingest-a-regulation.md) | Ingest a regulation | Long-running run | UC-1 | ✓ | Specified |
| [CUC-05](cuc-05-restore-a-curated-instrument.md) | Restore a curated instrument | Long-running run | UC-1 | ✓ | Specified |
| [CUC-06](cuc-06-resolve-a-near-miss.md) | Resolve a near-miss | Review/resolve | UC-1 | ✓ | Specified |
| [CUC-07](cuc-07-merge-duplicate-capabilities-obligations.md) | Merge duplicate Capabilities/Obligations | Review/resolve | — | ✓ | Specified |
| [CUC-08](cuc-08-ask-a-gap-question.md) | Ask a gap question | Q&A → report | UC-3 | ✓ | Specified |
| [CUC-09](cuc-09-author-policy-tree.md) | Author a policy tree (gap-to-closed) | Authoring | UC-2 | ✓ | Specified |
| [CUC-10](cuc-10-propose-a-policy.md) | Propose a policy | Lifecycle | UC-2 | ✓ | Specified |
| [CUC-11](cuc-11-return-and-resume.md) | Return & resume | — | — | ✓ | Specified |
| [CUC-12](cuc-12-act-on-an-inbox-assignment.md) | Act on an Inbox assignment | — | — | ✓ (minimal) | Specified |

Out-of-v1 flows (amendment-sweep UX, applicability assessment, audit browsing, local-file
intake) receive CUC ids when they are designed; this index is where they appear.
