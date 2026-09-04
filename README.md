# Policy System

Ingest EU regulations and internal business policies into a unified compliance
knowledge graph, and answer questions against it in natural language.

- _"What approved policies do we have in place that cover the Cyber Resilience Act's obligations for manufacturers?"_
- _"What do I need to consider if I use library XYZ in the code base I'm working on?"_
- _"Which governed capabilities have no working control yet?"_

Answers are grounded in the graph — every claim traces to regulation text and
policy content that was actually retrieved, not to model recall.

## Architecture

Two deployable containers:

| Container      | Responsibility                                                                                                                                                                                                    |
| -------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **PS Service** | EU regulation meta-model, ingestion pipeline, guarded read-only Cypher query engine, MCP interface, REST API, and a LiteLLM interface to 100+ models via Ollama, Azure Foundry, AWS Bedrock, Anthropic, or OpenAI |
| **FalkorDB**   | The graph database holding the compliance knowledge graph — a separate container so it can be patched independently without rebuilding or redeploying PS Service                                                  |

PS Service's image is `ghcr.io/mindovermachine-dev/ps-service`. Each release publishes
it under the release's semver tag and `latest`, as a multi-arch manifest list covering
`linux/amd64` and `linux/arm64`.

Users never talk to PS Service directly. They use a client:

- **Policy System plugin** — a Claude plugin bundling the `ps-qna` skill and its MCP
  connector. Read-only: ask compliance questions, get graph-grounded answers.
- **ps-cli** — a command-line client driving PS Service's REST API: select and ingest
  EU regulations from Cellar/ELI, ingest internal policies, check service health and
  readiness. See the [user guide](./docs/artifacts/user-guide.md#ps-cli).
- **Policy Editor** — authoring Policies, Standards, and Controls and linking them to
  Capabilities. Under exploration; not yet designed.

---

## Getting started for local testing

Deploying Policy System to try it out on your own laptop — prerequisites, the
step-by-step walkthrough, current implementation status of each step, and
troubleshooting — is documented in the user guide's
[Local Test](./docs/artifacts/user-guide.md#local-test) section. Steps 1–5 (cluster,
chart install, and seeding the graph from the curated catalog) work today; steps 6–7
(the Policy System plugin) do not yet — see the section's own
[Status of this path](./docs/artifacts/user-guide.md#status-of-this-path) table, tracked
as [#53](https://github.com/mindovermachine-dev/policy-system/issues/53).

**To actually run the system today**, follow [CONTRIBUTING.md](./CONTRIBUTING.md)
Option A or B — a devcontainer or local venv, with FalkorDB in a Podman container and
Claude Desktop wired to a locally-spawned MCP server. That path works now.

---

## Target audiences

These roles consume the Policy System through a client, never directly:

| Role                     | Primary use case                                                                                                                                           |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Compliance Officers**  | Define governance processes; review regulations; query obligations and see mapped policies/controls; identify gaps; select and ingest external regulations |
| **Policy Managers**      | Create, edit, and approve business policies and standards; manage content lifecycle                                                                        |
| **Legal Counsel**        | Review regulatory requirements and organizational responses; evaluate coverage gaps                                                                        |
| **Security Architects**  | See technical controls mapped to the obligations they fulfil; design compliant solutions                                                                   |
| **Risk Managers**        | Compliance scores with drill-down by obligation, policy, standard, and control                                                                             |
| **DevOps/Engineering**   | Query compliance status of solutions; integrate automated checks into CI/CD                                                                                |
| **Auditors**             | Review governance decisions and approval logs; trace obligations to controls with full provenance                                                          |
| **Software Engineers**   | Check what a Standard or Control requires before shipping; "is my service compliant?"                                                                      |
| **Security Engineers**   | Find coverage gaps below the Policy level; reason about blast radius if a control fails                                                                    |
| **Engineering Managers** | Whole-team posture summaries and prioritised punch lists — open-ended synthesis, not single-entity lookups                                                 |
| **System Admins**        | Check service health and readiness; configure which instance ps-cli targets; trigger ingestion                                                             |

## Development

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the working development setup, coding
standards, testing, and the pull request process.

Architecture and domain documentation lives in [docs/](./docs):

- [`docs/architecture/ps-solution-architecture.md`](./docs/architecture/ps-solution-architecture.md) — system context and containers
- [`docs/architecture/ps-service-container-architecture.md`](./docs/architecture/ps-service-container-architecture.md) — PS Service component design
- [`docs/artifacts/ps-domain-concepts.md`](./docs/artifacts/ps-domain-concepts.md) — the graph's entities, relationships, and vocabulary
