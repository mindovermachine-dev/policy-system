# Policy System

Policy System is a Open Source Software that can ingest EU regulations and internal business policies into a unified regulatory and compliance knowledge graph, and answer questions against it in natural language.

- _"What approved policies do we have in place that cover the Cyber Resilience Act's obligations for manufacturers?"_
- _"What do I need to consider if I use library XYZ in the code base I'm working on?"_
- _"Which governed capabilities have no working control yet?"_

Answers are grounded in the graph — every claim traces to regulation text and
policy content that was retrieved. AI is used to analyze questions, derive intent, create queries and converting the returned data into an answer. The actual queries are fully deterministic. Where AI is involved, measures have been implemented to ensure that data is accurate and reliable.

## Features

| Feature                                           | Description                                                                                                                                      | Business value                                                                                                                 | Status         |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | -------------- |
| **EU Regulation Ingestion**                       | Ingest an EU regulation by CELEX identifier (via Cellar/ELI), mapping its raw text into the compliance graph                                     | Removes manual regulation tracking and transcription; keeps coverage current with authoritative source text                    | ✅ Implemented |
| **Internal Policy & Regulation Ingestion**        | Author internal business regulations, policies, standards, and controls from a structured intake document                                        | Brings internal governance into the same graph as external regulation for unified traceability                                 | ✅ Implemented |
| **Natural Language Compliance Q&A**               | Ask compliance questions in plain language via Claude Desktop or VSCode; answers are grounded in and traceable to graph data                     | Makes compliance knowledge accessible without requiring Cypher or graph expertise                                              | ✅ Implemented |
| **Regulatory Knowledge Graph**                    | Unified compliance spine linking Regulatory Instrument → Role/Requirement → Obligation → Capability → Policy → Standard → Control                | Single source of truth connecting obligations to concrete controls with full provenance                                        | ✅ Implemented |
| **Regulatory Change Monitoring**                  | Polls EU sources for amendments to tracked regulations and triggers re-ingestion of affected content                                             | Keeps the graph current without manual regulation-watching effort                                                              | ✅ Implemented |
| **Curated Content Catalog**                       | Restore pre-ingested regulations and standards (e.g. CRA, GDPR) from vetted artifacts instead of running a full ingestion pipeline               | Faster onboarding and evaluation; avoids re-running expensive ingestion for common instruments                                 | ✅ Implemented |
| **Passkey-Signed Privileged Actions**             | Requires a signed passkey approval before a privileged action is carried out — today, merging semantically-similar but not identical graph nodes | Prevents high-impact actions (like conflating distinct obligations or capabilities) from happening without a human signing off | ✅ Implemented |
| **Policy/Standard/Control Proposal Lifecycle**    | Author and review a Policy, Standard, or Control as a proposal before it's committed to the graph                                                | Lets policy managers draft and review governance content with an approval step, instead of authoring directly into production  | 🗺️ Roadmap     |
| **RBAC + ABAC Authorization**                     | Role- and attribute-based access control for who can read, ingest, or approve content in an instance                                             | Lets organizations restrict sensitive compliance data and approval actions to the right people                                 | 🗺️ Roadmap     |
| **National Transposition & Directive Monitoring** | Track how an EU directive is transposed into national law per member state, and monitor each transposition for amendments                        | Answers jurisdiction-specific compliance questions instead of only the EU-directive level                                      | 🗺️ Roadmap     |
| **FalkorDB Backup & Restore**                     | Scheduled backups of the compliance graph to Azure Blob Storage, with restore from a snapshot                                                    | Protects the compliance graph against data loss and supports disaster recovery                                                 | 🗺️ Roadmap     |

## Getting started

### Evaluating Policy System

If you want to try Policy System and evaluate it on your own computer this is documented in the installation guide's
[Evaluator installation](./docs/artifacts/installation-guide.md#evaluator-installation) section.

### Contributing to Policy Systems codebase

If you want to help develop and evolve Policy System we welcome your contributions. See [CONTRIBUTING.md](./CONTRIBUTING.md) for
development setup, coding standards, testing, and the contribution workflow.

### Understanding the architecture

Architecture and domain documentation lives in [docs/](./docs):

- [`docs/architecture/ps-solution-architecture.md`](./docs/architecture/ps-solution-architecture.md) — system context and containers
- [`docs/architecture/ps-service-container-architecture.md`](./docs/architecture/ps-service-container-architecture.md) — PS Service component design
- [`docs/artifacts/ps-domain-concepts.md`](./docs/artifacts/ps-domain-concepts.md) — the graph's entities, relationships, and vocabulary

---

## Target audiences

Policy System has been designed with the following roles in mind:

| Role                     | Primary use case                                                                                                    |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| **Compliance Officers**  | Define governance processes; review regulations; select and ingest external regulations                             |
| **Policy Managers**      | Create, edit, and approve business policies and standards; manage content lifecycle; ingest internal regulations    |
| **Legal Counsel**        | Review regulatory requirements and organizational responses; evaluate coverage gaps                                 |
| **Security Architects**  | See technical controls mapped to the obligations they fulfil; design compliant solutions                            |
| **Risk Managers**        | Compliance scores with drill-down by obligation, policy, standard, and control                                      |
| **DevOps/Engineering**   | Query compliance status of solutions; integrate automated checks into CI/CD                                         |
| **Auditors**             | Review governance decisions and approval logs; trace obligations to controls with full provenance                   |
| **Software Engineers**   | Check what a Standard or Control requires before shipping; "is my service compliant?"                               |
| **Security Engineers**   | Find coverage gaps below the Policy level; reason about blast radius if a control fails                             |
| **Engineering Managers** | Whole-team posture summaries and prioritised punch lists — open-ended synthesis, not single-entity lookups          |
| **System Admins**        | Check service health and readiness; provision ps-cli's named targets/contexts; manage Policy-System global settings |
