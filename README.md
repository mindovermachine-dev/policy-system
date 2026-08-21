# Policy Sub System

Quick start: Read the [CONTRIBUTING.md](./CONTRIBUTING.md)

## Primary use case

Your system need functionality such that it can provide answers to questions about EU regulation and/or custom business regulations like Software, DevOps and MLOps Engineering Practices. For specific target audience and use cases please reference [Clients and Target Audiences](#clients-and-target-audiences) below.

## Grand idea

What if we could ingest EU regulations into a unified data model along with business policies and expose an API that can answer questions like:

- "What approved policies do we have in place that convert the Cyber Resiliency Act Obligations for Manufactures?"
- "What do I need to consider if I use this technology / library XYZ in my the code base I'm working on?"

## What is the Policy Sub System?

A single container that wrap the following functionality:

- EU Regulations Meta-model
- FalkorDB with graphRAG SDK
- Ingestion pipeline
- Q&A pipeline with falsification verification
- LiteLLM interface to 100+ LLMs via Ollama, Azure Foundry, AWS Bedrock, Anthropic, OpenAI using the OpenAI format.

### DevContainer

The dev container with all required dependencies installed and pinned that you will be using to develop or maintain PSS

### PSSContainer

The deployable container that implement PSS functionality. This is what you will be using as a dependency in you project.

## Clients and Target Audiences

The target audience outlined below will NOT be consuming PSS directly, they will be using one of multiple clients that consume PSS. The repo contains a Claude Desktop skill that can be uploaded into Claude Desktop and will function as a read-only client for asking questions to PSS.

| Role                     | Primary Use Case                                                                                                                                     |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Compliance Officers**  | Define governance processes; review regulations; query obligations and see mapped policies/controls; identify gaps                                   |
| **Policy Managers**      | Create, edit, and approve business policies and standards; manage content lifecycle                                                                  |
| **Legal Counsel**        | Review regulatory requirements and organizational responses; evaluate coverage gaps                                                                  |
| **Security Architects**  | See technical controls mapped to obligations they fulfill; design compliant solutions                                                                |
| **Risk Managers**        | Get compliance scores with drill-down by obligation, policy, standard, and control                                                                   |
| **DevOps/Engineering**   | Query compliance status of solutions; integrate automated checks in CI/CD pipelines                                                                  |
| **Auditors**             | Review governance decisions and approval logs; trace obligations to controls with full provenance                                                    |
| **Software Engineers**   | Check what a specific Standard/Control requires before shipping; ideally check "is my service compliant?"                                            |
| **Security Engineers**   | Find coverage gaps below the Policy level (governed capabilities with no working Control yet); reason about blast radius if a specific control fails |
| **Engineering Managers** | Get whole-team/whole-org posture summaries and prioritized punch lists — open-ended synthesis questions, not single-entity lookups                   |
| **System Admin**         | Start, stop, and configure the PSS container via PS-Cli; run the PDF ingestion pipeline for business regulations and policies                        |
