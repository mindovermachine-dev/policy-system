#!/usr/bin/env python3
"""Ingest CRA regulation into FalkorDB via GraphRAG-SDK (spike pipeline-rag5).

CRA-only, native-graph output (no JSON export), graph name: policy_system_graphrag_native_full.
Backend: Azure gpt-5.4-mini + text-embedding-3-large via litellm.
Audit patch: monkey-patch _prune/_filter_quality to capture dropped nodes/rels.
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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from falkordb import FalkorDB
from graphrag_sdk import (
    ConnectionConfig,
    GraphExtraction,
    GraphRAG,
    IngestionPipeline,
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
DEFAULT_GRAPH_NAME = "policy_system_graphrag_native_full"


# === PRUNE/AUDIT DATA STRUCTURES ===

@dataclass
class PruneAudit:
    """Schema per README 'Sidecar schema'.

    Fields match the JSON schema in README:
    - ts, run_id, document_id, stage, kind
    - node: id, label, reason
    - rel: start, start_label, end, end_label, rel_type, source_ref, keywords, weight, chunk_ids, declared
    """
    ts: str
    run_id: str
    document_id: str
    stage: str  # "filter_quality" | "prune"
    kind: str   # "node" | "relationship"
    # node
    id: str | None = None
    label: str | None = None
    reason: str | None = None
    # relationship
    start: str | None = None
    start_label: str | None = None
    end: str | None = None
    end_label: str | None = None
    rel_type: str | None = None
    source_ref: str | None = None
    keywords: str | None = None
    weight: float | None = None
    chunk_ids: List[str] = field(default_factory=list)
    declared: Optional[List[List[str]]] = None

def _get_rel_type(r) -> str:
    """Safely get rel_type from a GraphRelationship (lives in .properties, not as attr)."""
    return r.properties.get("rel_type", r.type) if r.properties else r.type


def _build_prune_audit_patch(
) -> Tuple[Any, Any, Dict[str, Any], Any, Any]:
    """
    Monkey-patch factory for _prune/_filter_quality audit.

    Returns (audit_filter_quality_fn, audit_prune_fn, audit_ctx, orig_filter, orig_prune)
    where audit_ctx is a dict with:
      - 'file_handle': open file handle for JSONL output
    and orig_ methods are the un-patched originals to restore later.
    """
    import inspect

    # Guard 1: SDK version check (verified 1.4.0 works)
    try:
        import graphrag_sdk
    except ImportError:
        raise RuntimeError("graphrag_sdk must be importable for version guard")
    version = None
    for attr in ("__version__", "version", "VERSION"):
        if hasattr(graphrag_sdk, attr):
            version = getattr(graphrag_sdk, attr)
            break
    if version is None:
        raise RuntimeError("graphrag_sdk does not expose a version attribute")
    if version != "1.4.0":
        raise RuntimeError(
            f"graphrag_sdk version mismatch: expected 1.4.0, got {version}. "
            "Refusing to patch with unknown signature."
        )

    # Guard 2: _prune signature check
    prune_sig = inspect.signature(IngestionPipeline._prune)
    expected_prune_params = ['self', 'graph_data', 'ontology']
    actual_prune_params = list(prune_sig.parameters.keys())
    if actual_prune_params != expected_prune_params:
        raise RuntimeError(
            f"_prune signature changed: expected {expected_prune_params}, "
            f"got {actual_prune_params}. Patch will fail; aborting."
        )

    # Guard 3: _filter_quality signature check
    try:
        filter_sig = inspect.signature(IngestionPipeline._filter_quality)
        expected_filter_params = ['self', 'graph_data']
        actual_filter_params = list(filter_sig.parameters.keys())
        if actual_filter_params != expected_filter_params:
            raise RuntimeError(
                f"_filter_quality signature changed: expected {expected_filter_params}, "
                f"got {actual_filter_params}. Patch will fail; aborting."
            )
    except AttributeError:
        raise RuntimeError(
            "_filter_quality method not found. SDK may have changed the audit stages."
        )

    # Store originals
    _orig_filter_quality = IngestionPipeline._filter_quality
    _orig_prune = IngestionPipeline._prune

    # Audit context (written by run())
    audit_ctx: Dict[str, Any] = {'file_handle': None, 'current_document_id': None}

    def audit_filter_quality(self, graph_data: Any) -> Any:
        """Patch Step 4b: capture nodes/rels dropped by quality filter."""
        before_nodes = set(n.id for n in graph_data.nodes)
        before_rels = set((r.start_node_id, r.end_node_id, _get_rel_type(r))
                         for r in graph_data.relationships)

        out = _orig_filter_quality(self, graph_data)

        after_nodes = set(n.id for n in out.nodes)
        after_rels = set((r.start_node_id, r.end_node_id, _get_rel_type(r))
                        for r in out.relationships)

        dropped_nodes = before_nodes - after_nodes
        dropped_rels = before_rels - after_rels

        for nid in dropped_nodes:
            label = "Unknown"
            for n in graph_data.nodes:
                if n.id == nid:
                    label = n.label
                    break
            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=audit_ctx.get('current_document_id') or "unknown",
                document_id=audit_ctx.get('current_document_id') or "unknown",
                stage="filter_quality",
                kind="node",
                id=nid,
                label=label,
                reason="dangling"
            ))

        for start, end, rel_type in dropped_rels:
            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=audit_ctx.get('current_document_id') or "unknown",
                document_id=audit_ctx.get('current_document_id') or "unknown",
                stage="filter_quality",
                kind="relationship",
                start=start,
                end=end,
                rel_type=rel_type,
                reason="dangling",
                chunk_ids=[]
            ))

        return out

    def audit_prune(self, graph_data: Any, ontology: Any) -> Any:
        """Patch Step 5: capture nodes/rels pruned by ontology mismatch."""
        before_nodes = set(n.id for n in graph_data.nodes)
        before_rels = set((r.start_node_id, r.end_node_id, _get_rel_type(r))
                         for r in graph_data.relationships)

        out = _orig_prune(self, graph_data, ontology)

        after_nodes = set(n.id for n in out.nodes)
        after_rels = set((r.start_node_id, r.end_node_id, _get_rel_type(r))
                        for r in out.relationships)

        dropped_nodes = before_nodes - after_nodes
        dropped_rels = before_rels - after_rels

        # Build mapping from node id to label
        node_labels = {n.id: n.label for n in graph_data.nodes}
        # Build mapping from relationship to (src_label, tgt_label)
        rel_labels = {}
        for r in graph_data.relationships:
            rel_labels[(r.start_node_id, r.end_node_id, _get_rel_type(r))] = (
                node_labels.get(r.start_node_id, "Unknown"),
                node_labels.get(r.end_node_id, "Unknown")
            )

        # Determine reason for each dropped node
        declared_labels = {e.label for e in ontology.entities}
        for nid in dropped_nodes:
            label = node_labels.get(nid, "Unknown")
            if label not in declared_labels:
                reason = "label_undeclared"
            else:
                reason = "dangling"

            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=audit_ctx.get('current_document_id') or "unknown",
                document_id=audit_ctx.get('current_document_id') or "unknown",
                stage="prune",
                kind="node",
                id=nid,
                label=label,
                reason=reason
            ))

        # Determine reason for each dropped relationship
        declared_rel_types = {r.label for r in ontology.relations}
        # Per-relation pattern sets (mirrors _prune internal logic)
        declared_patterns_per_rel: dict[str, set[tuple]] = {}
        for rt in ontology.relations:
            if rt.patterns:
                declared_patterns_per_rel[rt.label] = {tuple(p) for p in rt.patterns}
            else:
                declared_patterns_per_rel[rt.label] = None  # open (accepts any pair)
        for start, end, rel_type in dropped_rels:
            src_label, tgt_label = rel_labels.get((start, end, rel_type), ("Unknown", "Unknown"))

            if rel_type not in declared_rel_types:
                reason = "rel_type_undeclared"
            else:
                valid_pairs = declared_patterns_per_rel.get(rel_type)
                if valid_pairs is not None and (src_label, tgt_label) not in valid_pairs:
                    reason = "pattern_mismatch"
                else:
                    reason = "dangling"

            # Extract source_ref, keywords, weight, chunk_ids from relationship properties
            source_ref = None
            keywords = None
            weight = None
            chunk_ids: List[str] = []
            declared: Optional[List[List[str]]] = None

            for r in graph_data.relationships:
                if (r.start_node_id == start and r.end_node_id == end
                        and (_get_rel_type(r) == rel_type)):
                    # Use r.properties.get as per FLAW-005
                    source_ref = r.properties.get('source_ref') if hasattr(r, 'properties') and isinstance(r.properties, dict) else None
                    keywords = r.properties.get('keywords') if hasattr(r, 'properties') and isinstance(r.properties, dict) else None
                    weight = r.properties.get('weight') if hasattr(r, 'properties') and isinstance(r.properties, dict) else None
                    chunk_ids = r.properties.get('chunk_ids') if hasattr(r, 'properties') and isinstance(r.properties, dict) and isinstance(r.properties.get('chunk_ids'), list) else []
                    # declared is only in output from _prune for pattern_mismatch; not available in input
                    break

            # For pattern_mismatch, reconstruct which patterns WERE declared for THIS rel_type
            if reason == "pattern_mismatch":
                pairs = declared_patterns_per_rel.get(rel_type)
                declared = [list(p) for p in pairs] if pairs else None

            _write_audit_entry(PruneAudit(
                ts=datetime.now(timezone.utc).isoformat(),
                run_id=audit_ctx.get('current_document_id') or "unknown",
                document_id=audit_ctx.get('current_document_id') or "unknown",
                stage="prune",
                kind="relationship",
                start=start,
                end=end,
                rel_type=rel_type,
                start_label=src_label,
                end_label=tgt_label,
                reason=reason,
                source_ref=source_ref,
                keywords=keywords,
                weight=weight,
                chunk_ids=chunk_ids,
                declared=declared,
            ))

        return out

    def _write_audit_entry(entry: PruneAudit):
        """Write to sidecar file if audit context has an open handle."""
        fh = audit_ctx.get('file_handle')
        if fh:
            try:
                fh.write(json.dumps(asdict(entry)) + "\n")
                fh.flush()
            except Exception as e:
                logging.warning(f"Failed to write audit entry: {e}")

    return audit_filter_quality, audit_prune, audit_ctx, _orig_filter_quality, _orig_prune


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


def _normalise_regex(s: str) -> Tuple[str, int]:
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
    p.add_argument("--source", choices=["cra"], required=False, default="cra",
                   help="Source to ingest (default: cra)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=6379)
    p.add_argument("--graph-name", default=DEFAULT_GRAPH_NAME)
    p.add_argument("--max-concurrency", type=int, default=2,
                   help="Concurrency for the shared LLM+extraction gate (default 2)")
    p.add_argument("--reset", action="store_true",
                   help="Delete the TARGET graph before ingest (scoped to --graph-name)")
    # Full-corpus defaults: no substantive filter, no chunk cap
    p.add_argument("--max-chunks", type=int, default=None,
                   help="[DEPRECATED] Full-corpus mode: max chunks now unrestricted. "
                        "This flag has no effect.")
    p.add_argument("--substantive", type=int, default=None,
                   help="[DEPRECATED] Full-corpus mode: no content filtering by default.")
    p.add_argument("--filter-regex", default="shall|should",
                   help="Deprecated: no filtering applied in full-corpus mode.")
    p.add_argument("--spread", action="store_true",
                   help="Deprecated: no stratification in full-corpus mode.")
    p.add_argument("--prune-log", type=str, default=None,
                   help="Path to JSONL sidecar for prune/quality audit (default: logs/pruned-<ts>.jsonl)")
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

    # === TASK 1: SDK version guard + monkey-patch registration ===
    audit_filter_quality, audit_prune, audit_ctx, _orig_filter_quality, _orig_prune = (
        _build_prune_audit_patch()
    )

    # Determine prune log path
    if args.prune_log:
        prune_log_path = Path(args.prune_log)
    else:
        prune_log_path = LOG_DIR / f"pruned-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.jsonl"

    # Initialize audit context with file path and opened handle
    audit_ctx['audit_file_path'] = str(prune_log_path)
    audit_ctx['file_handle'] = open(prune_log_path, 'a', encoding='utf-8')

    # Monkey-patch IngestionPipeline
    IngestionPipeline._filter_quality = audit_filter_quality
    IngestionPipeline._prune = audit_prune

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
    chunk_select_note = "full_document (no substantive filter)"
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

        # Prepare audit_ctx with document_id before ingest
        audit_ctx['current_document_id'] = doc_id

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

        # Restore original methods after ingest completes (even on error)
        IngestionPipeline._filter_quality = _orig_filter_quality
        IngestionPipeline._prune = _orig_prune

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

    # Close audit file handle
    if audit_ctx['file_handle']:
        audit_ctx['file_handle'].close()
        audit_ctx['file_handle'] = None

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
