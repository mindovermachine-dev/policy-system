# © 2026 Cartman ApS. All rights reserved.
"""Ingest CRA/NIS2/GDPR (PDF) and Engineering Practices (narrative rewrite)
into FalkorDB via GraphRAG-SDK, constrained to schema.SCHEMA.

Writes to a graph namespace separate from the production `policy_system`
graph (default: `policy_system_graphrag_spike`) so this spike never touches
the graph the current pipeline (tools/graph-ingestion) produces.

Run from the repo root:

    pip install -r spikes/pipeline-rag/requirements.txt
    python spikes/pipeline-rag/ingest.py --source all --backend ollama

Ollama backend prerequisites (see README.md):
    ollama pull gemma4:12b        # default chat model -- already pulled
    ollama pull nomic-embed-text  # embedding model -- already pulled

Azure Foundry backend prerequisites: see docs/azure-foundry-setup.md. Not
run without an explicit go-ahead per this spike's setup decisions — this
script will refuse to start against --backend azure if the required env
vars are unset (see _require_azure_env below).
"""

import argparse
import asyncio
import logging
import os
from pathlib import Path

from graphrag_sdk import (
    ConnectionConfig,
    GraphExtraction,
    GraphRAG,
    LiteLLM,
    LiteLLMEmbedder,
    LLMExtractor,
)

from schema import SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_DIR = Path(__file__).resolve().parent

SOURCES = {
    "cra": (REPO_ROOT / "docs/regulations/CRA.pdf", "CRA-1.0"),
    "nis2": (REPO_ROOT / "docs/regulations/NIS2.pdf", "NIS2-1.0"),
    "gdpr": (REPO_ROOT / "docs/regulations/gdpr.pdf", "GDPR-1.0"),
    "engprac": (SPIKE_DIR / "engineering-practices-narrative.md", "ENGPRAC-3.0"),
}

DEFAULT_GRAPH_NAME = "policy_system_graphrag_spike"


def _require_azure_env() -> None:
    missing = [
        var
        for var in ("AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION")
        if not os.environ.get(var)
    ]
    if missing:
        raise SystemExit(
            "Azure Foundry backend requires "
            + ", ".join(missing)
            + " to be set. See docs/azure-foundry-setup.md — this script will not "
            "silently fall back to another backend."
        )


def build_llm_and_embedder(
    backend: str, model: str | None, embed_model: str | None, timeout: float
):
    """Returns (llm, embedder, embedding_dimension). embedding_dimension must be
    passed to GraphRAG(embedding_dimension=...) verbatim -- it sizes the vector
    index and has to match what the embedder actually returns, not the SDK's
    own default (256). LiteLLMEmbedder has no `dimensions` field of its own in
    graphrag-sdk 1.4.0 (confirmed against the installed package); extra kwargs
    pass through to the underlying litellm.embedding() call, which only some
    providers (e.g. OpenAI text-embedding-3-*) honor as a truncation request.

    `timeout` is passed to LiteLLM(...) explicitly because the SDK's own
    per-call timeout (graphrag_sdk.core.context.Context.provider_timeout_seconds)
    only applies when a latency_budget_ms is set on the ingest ctx -- this
    script never sets one, so without an explicit timeout here litellm falls
    back to an internal default (observed as 600s against Ollama, confirmed
    via a real run against qwen3-coder-next:q8_0 -- every extraction call
    timed out and several chunks failed outright after 3 retries).
    """
    if backend == "ollama":
        chat_model = model or "ollama_chat/gemma4:12b"
        embed = embed_model or "ollama/nomic-embed-text"
        # num_ctx: without it, Ollama loads gemma4:12b at its full
        # architecture-max context (observed: -c 262144 on the actual
        # llama-server process, confirmed via `ps -www -p <pid>`), which on
        # this hardware stalled every extraction call past a 1800s timeout.
        # None of this spike's chunks need anywhere close to that; 8192 is
        # generous headroom over a single regulation chunk + schema prompt.
        llm = LiteLLM(model=chat_model, timeout=timeout, num_ctx=8192)
        embedder = LiteLLMEmbedder(model=embed, timeout=timeout)
        return llm, embedder, 768  # nomic-embed-text's native, non-configurable output size
    if backend == "azure":
        _require_azure_env()
        if not model or not embed_model:
            raise SystemExit(
                "--backend azure requires --model <chat-deployment-name> and "
                "--embed-model <embedding-deployment-name> (the Azure Foundry "
                "deployment names, not the base model names)."
            )
        llm = LiteLLM(model=f"azure/{model}", timeout=timeout)
        embedder = LiteLLMEmbedder(model=f"azure/{embed_model}", dimensions=1536, timeout=timeout)
        return llm, embedder, 1536  # matches the truncation requested above
    raise SystemExit(f"Unknown backend: {backend}")


async def run(args: argparse.Namespace) -> None:
    llm, embedder, embedding_dimension = build_llm_and_embedder(
        args.backend, args.model, args.embed_model, args.timeout
    )

    sources = SOURCES.keys() if args.source == "all" else [args.source]
    for key in sources:
        path, _ = SOURCES[key]
        if not path.exists():
            raise SystemExit(f"Missing source file for '{key}': {path}")

    connection = ConnectionConfig(host=args.host, port=args.port, graph_name=args.graph_name)

    # GraphRAG.ingest(source=<single path>, max_concurrency=...) silently ignores
    # max_concurrency -- per its own docstring that kwarg only applies when `source`
    # is a list (parallel *documents*, dispatched through _ingest_batch). This
    # script always calls ingest() with one path at a time, so the per-chunk LLM
    # call concurrency actually used is whatever GraphExtraction's own default is
    # (12, matching LLMInterface's hardcoded default -- LiteLLM(...) has no
    # constructor param to override it). Building the extractor explicitly here is
    # the only way to make --max-concurrency do anything: it threads into
    # GraphExtraction._max_concurrency, which both the Step 1 (NER) and Step 2
    # (relation extraction) semaphores actually read. Confirmed via a real
    # engprac run that fired all 9 chunks' Step 2 calls concurrently against
    # Ollama despite --max-concurrency 1, because this wasn't wired up.
    # entity_extractor=LLMExtractor(llm): every prior run (Ollama and Azure
    # alike) left this unset, which defaults GraphExtraction's Step 1 (entity
    # NER) to GLiNERExtractor -- a local, non-LLM, zero-shot span-tagger
    # built for concrete named entities (Person/Organization/Location-shaped).
    # This ontology's abstract classification types (PracticeArea, RiskPath,
    # Capability, Obligation) are categories to infer, not proper-noun spans
    # in the text, which is closer to what Step 2's LLM already does well
    # (per SCHEMA's per-type descriptions) than what GLiNER is built for.
    # Routing Step 1 through the same chat LLM as Step 2 -- via LLMExtractor,
    # which (only when driven through GraphExtraction.extract(), not called
    # standalone) receives those same per-type descriptions -- is the
    # highest-signal untested variable identified in
    # docs/graphrag-sdk-configuration.md's config audit. Cost: roughly
    # doubles per-chunk LLM calls (Step 1 + Step 2 both hit the LLM now).
    extractor = GraphExtraction(
        llm=llm,
        entity_extractor=LLMExtractor(llm),
        entity_types=[e.label for e in SCHEMA.entities],
        max_concurrency=args.max_concurrency,
    )

    async with GraphRAG(
        connection=connection,
        llm=llm,
        embedder=embedder,
        ontology=SCHEMA,
        embedding_dimension=embedding_dimension,
    ) as rag:
        for key in sources:
            path, document_id = SOURCES[key]
            print(f"--- ingesting {key} ({path.relative_to(REPO_ROOT)}) as {document_id} ---")
            result = await rag.ingest(source=str(path), document_id=document_id, extractor=extractor)
            print(f"    nodes_created={result.nodes_created} relationships_created={result.relationships_created}")

        print("--- finalize (cross-document dedup, embedding backfill, index build) ---")
        await rag.finalize()

    print(f"Done. Graph: {args.graph_name} @ {args.host}:{args.port}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=[*SOURCES.keys(), "all"], default="all")
    parser.add_argument("--backend", choices=["ollama", "azure"], default="ollama")
    parser.add_argument("--model", default=None, help="Chat model/deployment override")
    parser.add_argument("--embed-model", default=None, help="Embedding model/deployment override")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="Per-call LLM/embedder timeout in seconds (default: 1800). See build_llm_and_embedder "
        "docstring for why this needs to be explicit against Ollama.",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=None,
        help="Concurrent extraction calls. Defaults by backend when unset: 1 for ollama (a single "
        "local instance on one GPU serializes concurrent requests anyway, so concurrency there "
        "mostly adds queuing/timeout risk instead of real parallelism), 10 for azure (matches this "
        "spike's deployment rate limit -- see docs/azure-foundry-setup.md).",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--graph-name", default=DEFAULT_GRAPH_NAME)
    args = parser.parse_args()

    if args.max_concurrency is None:
        args.max_concurrency = 10 if args.backend == "azure" else 1

    # graphrag_sdk logs pipeline step progress (Step 1/9..9/9, chunk counts) via
    # the stdlib `logging` module but never configures a handler itself -- without
    # this, those markers are silently dropped and the only visible output is the
    # per-source print() before/after each `rag.ingest()` call.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
