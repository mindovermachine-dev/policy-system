# Policy System

Quick start: Read the [CONTRIBUTING.md](./CONTRIBUTING.md)

## Primary use case

Your system need functionality such that it can provide answers to questions about EU regulation and/or custom business regulations like Software, DevOps and MLOps Engineering Practices. For specific target audience and use cases please reference [Clients and Target Audiences](#clients-and-target-audiences) below.

## Grand idea

What if we could ingest EU regulations into a unified data model along with business policies and expose an API that can answer questions like:

- "What approved policies do we have in place that convert the Cyber Resiliency Act Obligations for Manufactures?"
- "What do I need to consider if I use this technology / library XYZ in my the code base I'm working on?"

## What is the Policy System?

The Policy System is deployed as two containers:

**PS Service** — implements:
- EU Regulations Meta-model
- Ingestion pipeline
- LiteLLM interface to 100+ LLMs via Ollama, Azure Foundry, AWS Bedrock, Anthropic, OpenAI using the OpenAI format.

**FalkorDB** — the graph database storing the compliance knowledge graph, deployed as a separate container so it can be patched independently without rebuilding or redeploying PS Service.

### DevContainer

The dev container with all required dependencies installed and pinned that you will be using to develop or maintain the Policy System

### PS Service Container

The deployable container that implements PS Service functionality. This is what you will be using as a dependency in your project.

## Clients and Target Audiences

The target audience outlined below will NOT be consuming the Policy System directly, they will be using one of several clients that consume it:

- **PS Question Skill** — a Claude Desktop / VS Code skill included in this repo, functioning as a read-only client for asking questions, including falsification verification of answers
- **PS-Cli** — a command-line interface for starting, stopping, and configuring PS Service, and for driving a PDF ingestion pipeline for business regulations/policies (under exploration)
- **Policy Editor** — a client for authoring a Policy/Standard/Control from scratch and linking it to an existing Capability (under exploration, client not yet designed)

| Role                     | Primary Use Case                                                                                                                                     |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Compliance Officers**  | Define governance processes; review regulations; query obligations and see mapped policies/controls; identify gaps. Select and ingest external regulations |
| **Policy Managers**      | Create, edit, and approve business policies and standards; manage content lifecycle                                                                  |
| **Legal Counsel**        | Review regulatory requirements and organizational responses; evaluate coverage gaps                                                                  |
| **Security Architects**  | See technical controls mapped to obligations they fulfill; design compliant solutions                                                                |
| **Risk Managers**        | Get compliance scores with drill-down by obligation, policy, standard, and control                                                                   |
| **DevOps/Engineering**   | Query compliance status of solutions; integrate automated checks in CI/CD pipelines                                                                  |
| **Auditors**             | Review governance decisions and approval logs; trace obligations to controls with full provenance                                                    |
| **Software Engineers**   | Check what a specific Standard/Control requires before shipping; ideally check "is my service compliant?"                                            |
| **Security Engineers**   | Find coverage gaps below the Policy level (governed capabilities with no working Control yet); reason about blast radius if a specific control fails |
| **Engineering Managers** | Get whole-team/whole-org posture summaries and prioritized punch lists — open-ended synthesis questions, not single-entity lookups                   |
| **System Admin**         | Start, stop, and configure PS Service via PS-Cli; run the PDF ingestion pipeline for business regulations and policies                        |
