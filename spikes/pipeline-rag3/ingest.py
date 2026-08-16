#!/usr/bin/env python3
"""Ingest CRA regulation into FalkorDB via GraphRAG-SDK (spike pipeline-rag3).

CRA-only, native-graph output (no JSON export), graph name: policy_system_graphrag_native.
Backend: Azure gpt-5.4-mini + text-embedding-3-large via litellm.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from falkordb import FalkorDB
from graphrag_sdk import (
    ConnectionConfig,
    GraphExtraction,
    GraphRAG,
    LLMExtractor,
    LiteLLM,
    LiteLLMEmbedder,
)
from graphrag_sdk.core.models import TextChunks
from graphrag_sdk.ingestion.chunking_strategies.base import ChunkingStrategy
from graphrag_sdk.ingestion.chunking_strategies.sentence_token_cap import SentenceTokenCapChunking
from graphrag_sdk.ingestion.loaders.pdf_loader import PdfLoader

from ratelimit import RateLimitedLLM
from schema import SCHEMA


REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_DIR = Path(__file__).resolve().parent
LOG_DIR = SPIKE_DIR / "logs"

# CRA only
SOURCES = {
    "cra": {"path": REPO_ROOT / "docs/regulations/CRA.pdf",
            "document_id": "CRA-1.0", "kind": "pdf"},
}
DEFAULT_GRAPH_NAME = "policy_system_graphrag_native"


def _require_azure_env() -> None:
    missing = [
        v for v in ("AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION")
        if not os.environ.get(v)
    ]
    if missing:
        raise SystemExit(
            "Azure backend requires " + ", ".join(missing)
            + " to be set; this script will not silently fall back. Fetch via:\n"
            + "  az cognitiveservices account keys list --name "
            "policy-system-graphrag-spike --resource-group "
            "rg-policy-system-graphrag-spike --query key1 -o tsv"
        )


class CappedChunker:
    """Yield at most N chunks per document from the inner chunker."""

    def __init__(self, inner: ChunkingStrategy, cap: int) -> None:
        self.inner = inner
        self.cap = cap

    async def chunk_document(self, document, ctx):
        tc = await self.inner.chunk_document(document, ctx)
        return TextChunks(chunks=tc.chunks[: self.cap])

    def __getattr__(self, name):
        return getattr(self.inner, name)


class FilteringChunker:
    """Content-filtered, position-agnostic sample."""

    def __init__(self, inner, predicate, cap, spread: bool = False) -> None:
        self.inner = inner
        self.predicate = predicate
        self.cap = cap
        self.spread = spread
        self.total = 0
        self.matched = 0
        self.kept = 0
        self.last_kept_positions = []

    async def chunk_document(self, document, ctx):
        tc = await self.inner.chunk_document(document, ctx)
        chunks = tc.chunks
        self.total += len(chunks)
        positions = []
        for i, c in enumerate(chunks):
            if self.predicate.search(getattr(c, "text", None) or ""):
                positions.append(i)
        self.matched += len(positions)
        remaining = (self.cap - self.kept) if self.cap is not None else len(positions)
        if self.cap is None:
            chosen = positions
        elif remaining <= 0:
            chosen = []
        elif self.spread and len(positions) > remaining:
            step = max(1, len(positions) // remaining)
            chosen = positions[::step][:remaining]
        else:
            chosen = positions[:remaining]
        self.last_kept_positions = list(chosen)
        self.kept += len(chosen)
        return TextChunks(chunks=[chunks[i] for i in chosen])

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _normalise_regex(s: str) -> tuple[str, int]:
    """Handle optional regex shorthand /.../i|s|m form.

    Returns (pattern_str, additional_flags).
    re.I is always added by caller; this function only returns ADDITIONAL flags.
    """
    if s.startswith("/") and len(s) > 1 and s[-1] in "ims":
        # Look for flags at the end
        parts = s.rsplit("/", 1)
        if len(parts) == 2 and parts[0].startswith("/"):
            pattern_part = parts[0][1:]  # strip leading /
            flag_part = parts[1] if len(parts) > 1 else ""
            additional_flags = 0
            if "i" in flag_part:
                additional_flags |= re.I
            if "m" in flag_part:
                additional_flags |= re.M
            if "s" in flag_part:
                additional_flags |= re.S
            # Log warning if shorthand was detected
            logging.warning(
                "filter-regex used shorthand syntax %r; normalized to pattern=%r, flags=%s",
                s, pattern_part, flag_part
            )
            return pattern_part, additional_flags
    return s, 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest CRA into FalkorDB GraphRAG (spike)")
    p.add_argument("--source", choices=["cra"], required=True, default="cra",
                   help="Source to ingest (default: cra)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--graph-name", default=DEFAULT_GRAPH_NAME)
    p.add_argument("--max-concurrency", type=int, default=2,
                   help="Concurrency for the shared LLM+extraction gate (default 2)")
    p.add_argument("--reset", action="store_true",
                   help="Delete the TARGET graph before ingest (scoped to --graph-name)")
    p.add_argument("--max-chunks", type=int, default=None,
                   help="Dry-run cap: at most N chunks total per source (no cap by default)")
    p.add_argument("--substantive", type=int, default=None,
                   help="Content-filtered sample: keep only chunks matching "
                         "--filter-regex, cap at N.")
    p.add_argument("--filter-regex", default="shall|should",
                   help="Case-insensitive regex pattern (NOT shorthand — no delimiters/flags;\n"
                        "e.g. shall|should, not /shall/i; default: shall|should)")
    p.add_argument("--spread", action="store_true",
                   help="Stratify the sample: take an EVENLY-SPACED subset across all matches (front->back).")
    return p


def reset_graph(host: str, port: int, graph_name: str) -> None:
    """Per-graph reset scoped to graph_name."""
    db = FalkorDB(host=host, port=port)
    graph = db.select_graph(graph_name)
    try:
        graph.delete()
    except Exception:
        pass


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _require_azure_env()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"ingest-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"
    log_fh = log_file.open("a", encoding="utf-8")
    lock = asyncio.Lock()

    async def emit(entry: dict) -> None:
        async with lock:
            entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
            log_fh.write(json.dumps(entry) + "\n")
            log_fh.flush()

    llm_raw = LiteLLM(
        model="azure/gpt-5.4-mini",
        api_key=os.environ["AZURE_API_KEY"],
        api_base=os.environ["AZURE_API_BASE"],
        api_version=os.environ["AZURE_API_VERSION"],
        timeout=1800.0,
    )
    embedder = LiteLLMEmbedder(
        model="azure/text-embedding-3-large",
        api_key=os.environ["AZURE_API_KEY"],
        api_base=os.environ["AZURE_API_BASE"],
        api_version=os.environ["AZURE_API_VERSION"],
        dimensions=1536,
        timeout=1800.0,
    )
    llm = RateLimitedLLM(llm_raw, concurrency=args.max_concurrency,
                         req_per_window=10, window_s=60.0)

    chunker: ChunkingStrategy = SentenceTokenCapChunking(max_tokens=512, overlap_sentences=2)
    chunk_select_note = "no-filter (full document)"
    if args.substantive is not None:
        pattern_str, additional_flags = _normalise_regex(args.filter_regex)
        pred = re.compile(pattern_str, additional_flags | re.I)
        chunker = FilteringChunker(chunker, pred, cap=args.substantive, spread=args.spread)
        chunk_select_note = f"substantive=/{pattern_str}/i, cap {args.substantive}"
    elif args.max_chunks is not None:
        chunker = CappedChunker(chunker, args.max_chunks)
        chunk_select_note = f"prefix cap first {args.max_chunks}"

    extractor = GraphExtraction(
        llm=llm,
        entity_extractor=LLMExtractor(llm),
        entity_types=[e.label for e in SCHEMA.entities],
        max_concurrency=args.max_concurrency,
    )

    keys = ["cra"]
    if args.reset:
        reset_graph(args.host, args.port, args.graph_name)

    connection = ConnectionConfig(
        host=args.host, port=args.port, graph_name=args.graph_name)
    errors: list[dict] = []
    t0 = time.perf_counter()

    async with GraphRAG(
        connection=connection, llm=llm, embedder=embedder,
        ontology=SCHEMA, embedding_dimension=1536) as rag:
        await emit({"event": "start", "graph": args.graph_name, "sources": keys,
                    "substantive": args.substantive, "chunk_select": chunk_select_note,
                    "max_chunks": args.max_chunks, "max_concurrency": args.max_concurrency,
                    "chunker": "SentenceTokenCapChunking(512,2)",
                    "model": "azure/gpt-5.4-mini", "embed": "azure/text-embedding-3-large"})

        key = keys[0]
        src = dict(SOURCES[key])
        path = Path(src.get("source_file", str(src["path"])))
        doc_id = src.get("document_id", SOURCES[key]["document_id"])
        loader_cls = PdfLoader

        if not path.exists():
            await emit({"event": "error", "source": key,
                        "message": f"missing source file {path}"})
            errors.append({"source": key, "error": f"missing file {path}"})
            return 1

        t_src = time.perf_counter()
        pre = (getattr(chunker, "total", 0), getattr(chunker, "matched", 0),
               getattr(chunker, "kept", 0))
        try:
            result = await rag.ingest(
                source=str(path),
                document_id=doc_id,
                loader=loader_cls(),
                chunker=chunker,
                extractor=extractor,
            )
            await emit({
                "event": "source_done", "source": key, "document_id": doc_id,
                "wall_s": round(time.perf_counter() - t_src, 2),
                "nodes_created": result.nodes_created,
                "relationships_created": result.relationships_created,
                "chunks_indexed": result.chunks_indexed,
            })
        except Exception as exc:  # noqa: BLE001
            tb = "\n".join(traceback.format_exc().split("\n"))
            await emit({
                "event": "error", "source": key, "document_id": doc_id,
                "wall_s": round(time.perf_counter() - t_src, 2),
                "error": f"{type(exc).__name__}: {exc}", "traceback": tb})
            errors.append({"source": key, "error": str(exc)})
        post = (getattr(chunker, "total", 0), getattr(chunker, "matched", 0),
                getattr(chunker, "kept", 0))
        await emit({"event": "chunk_select", "source": key, "note": chunk_select_note,
                    "total_scanned": post[0] - pre[0],
                    "matched_filter": post[1] - pre[1],
                    "kept_for_ingest": post[2] - pre[2]})

        try:
            await rag.finalize()
            await emit({"event": "finalize_done"})
        except Exception as exc:  # noqa: BLE001
            tb = "\n".join(traceback.format_exc().split("\n"))
            await emit({
                "event": "finalize_error",
                "error": f"{type(exc).__name__}: {exc}", "traceback": tb})
            errors.append({"source": "finalize", "error": str(exc)})

    await emit({
        "event": "summary", "total_s": round(time.perf_counter() - t0, 2),
        "sources_processed": len(keys), "errors_count": len(errors), "errors": errors})
    log_fh.close()
    print(f"Done. graph={args.graph_name} sources={keys} errors={len(errors)} "
          f"total_s={round(time.perf_counter() - t0, 2)} -> {log_file}")
    return 1 if errors else 0


def main() -> None:
    args = build_parser().parse_args()
    rc = asyncio.run(run(args))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
