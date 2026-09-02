# Policy System

Ingest EU regulations and internal business policies into a unified compliance
knowledge graph, and answer questions against it in natural language.

- *"What approved policies do we have in place that cover the Cyber Resilience Act's obligations for manufacturers?"*
- *"What do I need to consider if I use library XYZ in the code base I'm working on?"*
- *"Which governed capabilities have no working control yet?"*

Answers are grounded in the graph — every claim traces to regulation text and
policy content that was actually retrieved, not to model recall.

## Architecture

Two deployable containers:

| Container | Responsibility |
| --- | --- |
| **PS Service** | EU regulation meta-model, ingestion pipeline, guarded read-only Cypher query engine, MCP interface, REST API, and a LiteLLM interface to 100+ models via Ollama, Azure Foundry, AWS Bedrock, Anthropic, or OpenAI |
| **FalkorDB** | The graph database holding the compliance knowledge graph — a separate container so it can be patched independently without rebuilding or redeploying PS Service |

PS Service's image is `ghcr.io/mindovermachine-dev/ps-service`. Each release publishes
it under the release's semver tag and `latest`, as a multi-arch manifest list covering
`linux/amd64` and `linux/arm64`.

Users never talk to PS Service directly. They use a client:

- **Policy System plugin** — a Claude plugin bundling the `ps-qna` skill and its MCP
  connector. Read-only: ask compliance questions, get graph-grounded answers.
- **ps-cli** — a command-line client driving PS Service's REST API: select and ingest
  EU regulations from Cellar/ELI, ingest internal policies, check service health and readiness.
- **Policy Editor** — authoring Policies, Standards, and Controls and linking them to
  Capabilities. Under exploration; not yet designed.

---

## Getting started for local testing

> [!IMPORTANT]
> **This section describes the target experience, and most of it does not work yet.**
>
> It is written ahead of the implementation deliberately, so that the gaps between
> "what we intend" and "what exists" are visible and trackable rather than discovered
> by the first person who tries it. Each step below carries its real status.
>
> **To actually run the system today**, follow [CONTRIBUTING.md](./CONTRIBUTING.md)
> Option A or B — a devcontainer or local venv, with FalkorDB in a Podman container and
> Claude Desktop wired to a locally-spawned MCP server. That path works now.

### Status of this path

| Step | Status | Tracking |
| --- | --- | --- |
| 1. Install Claude Desktop | ✅ Works | — |
| 2. Install Podman | ✅ Works | — |
| 3. Create the local cluster | ❌ No `kind` config exists | [#59](https://github.com/mindovermachine-dev/policy-system/issues/59) |
| 4. Deploy Policy System | ❌ No Helm chart; no release tag pushed, so the registry is empty | [#59](https://github.com/mindovermachine-dev/policy-system/issues/59), [#60](https://github.com/mindovermachine-dev/policy-system/issues/60) |
| 5. Load regulations | ❌ No curated-restore path into a deployed cluster | [#66](https://github.com/mindovermachine-dev/policy-system/issues/66) |
| 6. Install the plugin | ❌ Plugin does not exist; needs a remote MCP endpoint | [#53](https://github.com/mindovermachine-dev/policy-system/issues/53) ← [#39](https://github.com/mindovermachine-dev/policy-system/issues/39) ← [#67](https://github.com/mindovermachine-dev/policy-system/issues/67) |
| 7. Ask a question | ❌ Depends on 3–6 | — |

### Prerequisites

| Tool | Why | Install |
| --- | --- | --- |
| [Claude Desktop](https://claude.com/download) | Hosts the Policy System plugin | Download for macOS or Windows |
| [Podman](https://podman.io/docs/installation) | Container runtime backing the local cluster | `brew install podman` (macOS) |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Runs a Kubernetes cluster on Podman | `brew install kind` |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Talks to the cluster | `brew install kubectl` |
| [Helm](https://helm.sh/docs/intro/install/) | Installs the Policy System chart | `brew install helm` |

No LLM provider is required to evaluate the system. Restoring curated content and asking
questions both work without one. You only need provider credentials to ingest a regulation
outside the curated catalog — see
[CONTRIBUTING.md](./CONTRIBUTING.md#configure-the-llm-interface) for options, including
Ollama for a fully local, no-cost setup.

### 1. Install Claude Desktop

Download and sign in. No Policy System configuration is needed yet.

### 2. Install Podman and start its machine

```bash
podman machine init --cpus 4 --memory 8192
podman machine start
podman info    # confirm the machine is running
```

kind under Podman needs a machine with enough headroom to run a control plane plus both
Policy System containers. 4 CPUs / 8 GB is the tested floor.

### 3. Create the local cluster

```bash
export KIND_EXPERIMENTAL_PROVIDER=podman
kind create cluster --config deploy/kind/cluster.yaml
kubectl cluster-info --context kind-policy-system
```

The cluster config binds PS Service's REST and MCP ports to fixed host ports via
`extraPortMappings`, so clients reach a stable URL. This must be set at cluster creation
— it cannot be added to a running cluster — and it is what keeps the system reachable
without a `kubectl port-forward` held open in a terminal.

> ❌ **Not yet implemented.** `deploy/kind/cluster.yaml` does not exist — [#59](https://github.com/mindovermachine-dev/policy-system/issues/59).

### 4. Deploy Policy System

```bash
helm install policy-system ./charts/policy-system \
  --set llm.provider=ollama \
  --wait

kubectl get pods    # ps-service and falkordb should reach Running
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/health` reports process liveness; `/ready` additionally reports that every dependency
— FalkorDB, the configured LLM provider — is currently reachable. Wait for `/ready`
before continuing.

The chart pulls PS Service from `ghcr.io/mindovermachine-dev/ps-service`; pin a release
tag rather than tracking `latest` when you want a local test you can reproduce later.

LLM provider credentials, when you need them, are supplied as chart values backed by a
Kubernetes secret, never baked into the image.

> ❌ **Not yet implemented.** No chart exists ([#59](https://github.com/mindovermachine-dev/policy-system/issues/59)), and nothing has reached the registry yet: the release pipeline builds and pushes the image, but no release tag has been pushed, and the GHCR package must be made public by hand once after the first push before an unauthenticated pull can succeed ([#60](https://github.com/mindovermachine-dev/policy-system/issues/60)). Today PS Service runs as a container built from the repo's `Dockerfile`, or as a process from a virtualenv — see [CONTRIBUTING.md](./CONTRIBUTING.md).

### 5. Load regulations into the graph

A freshly deployed system has an empty graph and can answer nothing. Seed it:

```bash
ps catalog list                              # curated instruments available to restore
ps catalog restore --instrument cra          # Cyber Resilience Act
ps catalog restore --instrument baseline     # baseline engineering practices
ps regulations list                          # confirm what landed
```

Curated instruments are pre-ingested graphs published in a git repository and restored as a
data load — no LLM provider needed, and seconds rather than minutes. Regulations outside the
curated set are still ingested live with `ps regulations ingest --celex ...`, which does
require a configured LLM provider.

Until something is seeded, the system answers questions with an explicit "graph is unseeded"
error rather than an empty result.

> ❌ **Not yet implemented.** No curated-restore path exists — today's only seeding route
> (`tools/graph-ingestion/load_all.sh`) is a repo-local script connecting straight to
> `localhost:6379`, requiring a checkout, a virtualenv, and test fixtures. The curated
> catalog, export format, restore commands, and unseeded-graph error are all [#66](https://github.com/mindovermachine-dev/policy-system/issues/66).

### 6. Install the Policy System plugin

In Claude Desktop: **+** next to the prompt box → **Plugins** → **Add plugin**, then add
this repo as a marketplace and install `policy-system`.

One install brings both the `ps-qna` skill and its MCP connector — the skill arrives
already wired to the transport it needs, with no separate connector setup and no
credential pasting.

Point the connector at your local deployment when prompted for the endpoint URL:

```
http://localhost:8000/mcp
```

A local-test deployment runs with authentication disabled, so there is nothing to log into.
That mode is opt-in, refuses to bind anything but loopback, and warns on every startup — it
is for evaluation only.

> ❌ **Not yet implemented.** The plugin does not exist ([#53](https://github.com/mindovermachine-dev/policy-system/issues/53)). It requires a remote MCP transport with per-user authentication ([#39](https://github.com/mindovermachine-dev/policy-system/issues/39)), and the local-test authentication bypass it runs under ([#67](https://github.com/mindovermachine-dev/policy-system/issues/67)). Full per-user authentication is deferred, not dropped — it remains required before any non-local deployment ([#65](https://github.com/mindovermachine-dev/policy-system/issues/65)).

### 7. Ask a question

```
What obligations does the Cyber Resilience Act place on manufacturers,
and which of our policies cover them?
```

The skill grounds itself against the domain model, writes read-only Cypher, retrieves
from the graph, and constructs an answer that cites what it retrieved. If the graph
cannot answer, it says so rather than filling the gap from model recall.

If the skill does not engage on its own, ask for it by name: *"Use the ps-qna skill."*

### Troubleshooting

| Symptom | Check |
| --- | --- |
| Service unreachable | `ps health` — reports health, readiness, and which dependency is failing ([#68](https://github.com/mindovermachine-dev/policy-system/issues/68)) |
| Pods stuck `Pending` | `podman machine` sizing — the control plane plus both containers need ~8 GB |
| `/ready` returns unhealthy | `kubectl logs deploy/ps-service` — usually FalkorDB or an LLM provider is unreachable |
| Answers say "not present in the graph" | Step 5 — the graph is probably empty; run `ps regulations list` |
| Plugin installed but no cypher tool | The MCP connector's endpoint URL, and whether `/ready` is green |

---

## Target audiences

These roles consume the Policy System through a client, never directly:

| Role | Primary use case |
| --- | --- |
| **Compliance Officers** | Define governance processes; review regulations; query obligations and see mapped policies/controls; identify gaps; select and ingest external regulations |
| **Policy Managers** | Create, edit, and approve business policies and standards; manage content lifecycle |
| **Legal Counsel** | Review regulatory requirements and organizational responses; evaluate coverage gaps |
| **Security Architects** | See technical controls mapped to the obligations they fulfil; design compliant solutions |
| **Risk Managers** | Compliance scores with drill-down by obligation, policy, standard, and control |
| **DevOps/Engineering** | Query compliance status of solutions; integrate automated checks into CI/CD |
| **Auditors** | Review governance decisions and approval logs; trace obligations to controls with full provenance |
| **Software Engineers** | Check what a Standard or Control requires before shipping; "is my service compliant?" |
| **Security Engineers** | Find coverage gaps below the Policy level; reason about blast radius if a control fails |
| **Engineering Managers** | Whole-team posture summaries and prioritised punch lists — open-ended synthesis, not single-entity lookups |
| **System Admins** | Check service health and readiness; configure which instance ps-cli targets; trigger ingestion |

## Development

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the working development setup, coding
standards, testing, and the pull request process.

Architecture and domain documentation lives in [docs/](./docs):

- [`docs/architecture/ps-solution-architecture.md`](./docs/architecture/ps-solution-architecture.md) — system context and containers
- [`docs/architecture/ps-service-container-architecture.md`](./docs/architecture/ps-service-container-architecture.md) — PS Service component design
- [`docs/artifacts/ps-domain-concepts.md`](./docs/artifacts/ps-domain-concepts.md) — the graph's entities, relationships, and vocabulary
