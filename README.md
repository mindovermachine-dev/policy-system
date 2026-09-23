# Policy System

Policy System is a Open Source Software that can ingest EU regulations and internal business policies into a unified regulatory and compliance knowledge graph, and answer questions against it in natural language.

- _"What approved policies do we have in place that cover the Cyber Resilience Act's obligations for manufacturers?"_
- _"What do I need to consider if I use library XYZ in the code base I'm working on?"_
- _"Which governed capabilities have no working control yet?"_

Answers are grounded in the graph — every claim traces to regulation text and
policy content that was retrieved. AI is used to analyze questions, derive intent, create queries and converting the returned data into an answer. The actual queries are fully deterministic. Where AI is involved, measures have been implemented to ensure that data is accurate and reliable.

## Getting started

### Evaluating Policy System

If you want to try Policy System and evaluate it on your own computer this is documented in the user guide's
[Local Test](./docs/artifacts/user-guide.md#local-test) section.

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
