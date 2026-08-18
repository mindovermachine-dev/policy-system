#!/usr/bin/env python3
"""
Map policy_system_graphrag_native_full -> policy_system_graphrag_final_full
via an LLM-curated, Role-first cascade.

Applies the tests from test-data/eu-regulations/cra-extraction-methodology.md
to the already-extracted native entities (no re-ingest): a Role is kept only
if it is duty-bearing (Art. 3-style or clearly duty-creating operative text,
not any noun that appears in the text); Requirements are then searched for
scoped to each kept Role's mention-chunks (retrieval scoping, not a hard
graph gate) and kept only if they state an operative "shall" duty; kept
Requirements get a canonical Obligation assigned (1:1 with Role, text-collision
disambiguated); kept Obligations get a Capability assigned by convergence
against an already-minted registry before any new Capability is proposed.

The one CRA Regulation node the SDK never extracted (schema.py told it to,
but it didn't) is synthesized deterministically from regulation_map.json --
it's the document being ingested, not a judgment call.

Backend: Azure gpt-5.4-mini via litellm, through the same RateLimitedLLM
wrapper ingest.py uses (ratelimit.py) -- global concurrency=2 / 10-per-60s.

Idempotent: deletes + recreates the final graph each run. JSONL decision log
per run under logs/curate-<ts>.jsonl records every kept/dropped/merged call
with its stated reason, mirroring the existing pruned-*.jsonl audit shape.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from falkordb import FalkorDB
from graphrag_sdk import LiteLLM
from graphrag_sdk.core.models import ChatMessage

from ratelimit import RateLimitedLLM

SPIKE_DIR = Path(__file__).resolve().parent
LOG_DIR = SPIKE_DIR / "logs"

DEFAULT_ROLE_BATCH_SIZE = 12
DEFAULT_REQ_BATCH_SIZE = 15
MAX_EVIDENCE_CHUNKS = 3
MAX_EVIDENCE_CHARS = 1500


# === Cypher helpers ===

def esc(s: Any) -> str:
    """Escape single quotes in a Cypher string literal (FalkorDB rejects '')."""
    if s is None:
        return ""
    return str(s).replace("'", "\\'")


def _chunked(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# === LLM plumbing ===

def _require_azure_env() -> None:
    missing = [v for v in ("AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION")
               if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {missing}. Export them before running."
        )


def build_llm(max_concurrency: int = 2) -> RateLimitedLLM:
    _require_azure_env()
    llm_raw = LiteLLM(
        model="azure/gpt-5.4-mini",
        api_key=os.environ["AZURE_API_KEY"],
        api_base=os.environ["AZURE_API_BASE"],
        api_version=os.environ["AZURE_API_VERSION"],
        timeout=300.0,
    )
    return RateLimitedLLM(llm_raw, concurrency=max_concurrency, req_per_window=10, window_s=60.0)


async def call_json(llm: RateLimitedLLM, system: str, user: str, *, max_retries: int = 3) -> Any:
    """Call the LLM with system+user messages and parse a JSON value from the reply."""
    messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user),
    ]
    resp = await llm.ainvoke_messages(messages, max_retries=max_retries, timeout=120.0)
    text = resp.content.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m2 = re.search(r"(\[.*\]|\{.*\})", text, re.S)
        if not m2:
            raise
        return json.loads(m2.group(1))


# === Decision logging ===

class DecisionLog:
    """JSONL sidecar of every kept/dropped/merged LLM decision, for audit/spot-check."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def write(self, **kwargs: Any) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(), **kwargs}
        self._fh.write(json.dumps(entry) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# === Native graph reads ===

def load_regulation(path: str) -> dict:
    with open(path) as f:
        mapping = json.load(f)
    (reg_id, reg) = next(iter(mapping.items()))
    return {"id": reg.get("id", reg_id), "name": reg.get("name", reg_id)}


def fetch_roles(native) -> list[dict]:
    r = native.query("MATCH (n:__Entity__ {type: 'Role'}) RETURN n.id, n.name")
    return [{"id": row[0], "name": row[1]} for row in r.result_set]


def fetch_mention_chunks(native, entity_id: str) -> list[dict]:
    q = (
        "MATCH (:__Entity__ {id: '" + esc(entity_id) + "'})-[:MENTIONED_IN]->(c:Chunk) "
        "RETURN c.id AS id, c.text AS text, c.index AS idx ORDER BY c.index"
    )
    r = native.query(q)
    return [{"id": row[0], "text": row[1], "idx": row[2]} for row in r.result_set]


def fetch_co_mentioned(native, target_type: str, chunk_ids: list[str]) -> list[dict]:
    """Distinct native entities of target_type sharing any chunk in chunk_ids."""
    if not chunk_ids:
        return []
    ids_list = ",".join("'" + esc(c) + "'" for c in chunk_ids)
    q = (
        "MATCH (c:Chunk)<-[:MENTIONED_IN]-(t:__Entity__ {type: '" + esc(target_type) + "'}) "
        "WHERE c.id IN [" + ids_list + "] "
        "RETURN DISTINCT t.id AS id, t.name AS name, "
        "t.obligation_type AS obligation_type, t.confidence AS confidence, "
        "t.capability_type AS capability_type, t.source_ref AS source_ref"
    )
    r = native.query(q)
    return [
        {"id": row[0], "name": row[1], "obligation_type": row[2],
         "confidence": row[3], "capability_type": row[4], "source_ref": row[5]}
        for row in r.result_set
    ]


def expand_with_adjacent_chunks(native, role: dict) -> None:
    """Widen a role's chunk-scope with its immediate NEXT_CHUNK neighbors.

    A role name and the sentence stating its operative duty don't always
    land in the same 512-token chunk (paragraph-spanning duties, or a duty
    stated just after the defining sentence). Retrieval-scoping off only the
    exact mention-chunks then finds zero Requirement candidates even for a
    genuinely duty-bearing role. +/-1 chunk is a cheap, low-risk recall fix
    -- it reuses the ingestion's own chunk-order chain, no new inference.
    """
    existing_ids = {c["id"] for c in role["chunks"]}
    if not existing_ids:
        return
    ids_list = ",".join("'" + esc(c) + "'" for c in existing_ids)
    q = (
        "MATCH (c:Chunk) WHERE c.id IN [" + ids_list + "] "
        "OPTIONAL MATCH (c)-[:NEXT_CHUNK]->(nxt:Chunk) "
        "OPTIONAL MATCH (prv:Chunk)-[:NEXT_CHUNK]->(c) "
        "RETURN DISTINCT nxt.id AS nid, nxt.text AS ntext, nxt.index AS nidx, "
        "prv.id AS pid, prv.text AS ptext, prv.index AS pidx"
    )
    r = native.query(q)
    for row in r.result_set:
        nid, ntext, nidx, pid, ptext, pidx = row
        if nid and nid not in existing_ids:
            role["chunks"].append({"id": nid, "text": ntext, "idx": nidx})
            existing_ids.add(nid)
        if pid and pid not in existing_ids:
            role["chunks"].append({"id": pid, "text": ptext, "idx": pidx})
            existing_ids.add(pid)


def evidence_text(chunks: list[dict]) -> str:
    text = "\n---\n".join(c["text"] for c in chunks[:MAX_EVIDENCE_CHUNKS] if c.get("text"))
    if len(text) > MAX_EVIDENCE_CHARS:
        text = text[:MAX_EVIDENCE_CHARS] + "…"
    return text or "(no source text found)"


# === Final graph writes (MERGE-by-id: entities can legitimately be revisited
# across roles via chunk co-occurrence, so writes must be idempotent) ===

_NULL_LIKE = {"null", "none", "n/a", "na"}


def _is_missing(v: Any) -> bool:
    """True for real None and for the LLM's occasional stringified 'null'/
    'none'/'' -- verified a Requirement written with source_ref the literal
    string "null" because it's non-None and so slipped past a bare `is
    None` check."""
    if v is None:
        return True
    if isinstance(v, str) and v.strip().lower() in _NULL_LIKE | {""}:
        return True
    return False


def write_node(final, label: str, node_id: str, name: str, status: str = "active", **extra: Any) -> None:
    cypher = (
        "MERGE (n:" + label + " {id: '" + esc(node_id) + "'}) "
        "SET n.name = '" + esc(name) + "', n.status = '" + esc(status) + "'"
    )
    for k, v in extra.items():
        if _is_missing(v):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            cypher += f", n.{k} = {v}"
        else:
            cypher += ", n." + k + " = '" + esc(v) + "'"
    final.query(cypher)


def write_edge(final, src_label: str, src_id: str, rel: str, tgt_label: str, tgt_id: str, **props: Any) -> None:
    cypher = (
        "MATCH (s:" + src_label + " {id: '" + esc(src_id) + "'}), "
        "(t:" + tgt_label + " {id: '" + esc(tgt_id) + "'}) "
        "MERGE (s)-[e:" + rel + "]->(t)"
    )
    sets = [f"e.{k} = '{esc(v)}'" for k, v in props.items() if not _is_missing(v)]
    if sets:
        cypher += " SET " + ", ".join(sets)
    final.query(cypher)


# === Stage 1: Role test (batched, parallelizable) + dedup ===

ROLE_TEST_SYSTEM = """You are curating entities extracted from the EU Cyber \
Resilience Act (CRA) into a compliance graph. Apply this test for the Role \
entity type, exactly as defined by the project's extraction methodology:

TEST: A Role is an actor type the Regulation itself names and assigns duties \
to -- not any noun that appears in the text. Typically sourced from Article 3 \
(Definitions), but a role may qualify without an explicit Art. 3 entry if the \
operative text clearly creates a duty-bearing actor category (e.g. \
"Substantial modifier" in Art. 22(1) is not in Art. 3 but is unambiguously a \
distinct actor subject to obligations).

Do NOT keep institutional/procedural bodies the regulation merely talks \
ABOUT without imposing a "shall" duty ON them (e.g. committees, coordination \
bodies, consumer groups, classes of persons mentioned for context) unless the \
text does impose a duty on them specifically.

For each candidate, given its name and the chunk text where it is mentioned, \
decide whether it is duty-bearing per this test.

Respond with ONLY a JSON array, one object per candidate id, no prose:
[{"id": "...", "duty_bearing": true|false, "defines_ref": "Art. X" or null, \
"confidence": 0.0-1.0, "reason": "short phrase"}]"""

DEFINES_REF_SYSTEM = """You are locating an EU regulation citation. Given \
the full Article 3 (Definitions) text and a Role name, find the numbered \
point that defines that Role and return its citation, e.g. "Art. 3(14)". \
The source text has broken mid-word spacing from PDF extraction (e.g. \
"st ewar d"); match by meaning, not exact spelling. If no point defines \
this exact Role, return null -- do not guess.

Respond with ONLY one JSON object: {"defines_ref": "Art. 3(N)" or null}"""


def _whitespace_collapse(s: str) -> str:
    return re.sub(r"\s+", "", s.strip().lower())


_article3_text_cache: list[str | None] = []


def fetch_article3_text(native) -> str | None:
    """Article 3 ('Definitions') is chunked as a single oversized chunk in
    this corpus (~10k chars covering all ~51 points), so it's cheap to
    fetch once and reuse rather than re-querying per role."""
    if not _article3_text_cache:
        r = native.query(
            "MATCH (c:Chunk) WHERE c.text CONTAINS 'Definitions' AND c.text CONTAINS 'Article' "
            "RETURN c.text ORDER BY c.index LIMIT 1"
        )
        _article3_text_cache.append(r.result_set[0][0] if r.result_set else None)
    return _article3_text_cache[0]


async def _resolve_missing_defines_ref(
    llm: RateLimitedLLM, native, log: DecisionLog, role: dict,
) -> str | None:
    """defines_ref sometimes comes back null from stage1_role_test not
    because no Art. 3 entry exists for the role, but because the role's own
    evidence (capped at MAX_EVIDENCE_CHARS) got truncated before reaching
    it -- verified for open-source software steward, whose Art. 3(14) entry
    sits well past 1500 chars into the single oversized Art. 3 chunk. This
    re-checks against the full Article 3 text directly, not the role's
    truncated evidence window."""
    art3 = fetch_article3_text(native)
    if art3 is None or _whitespace_collapse(role["name"]) not in _whitespace_collapse(art3):
        return None
    try:
        result = await call_json(
            llm, DEFINES_REF_SYSTEM, f"Role: {role['name']!r}\n\nArticle 3 text:\n{art3}",
        )
    except Exception as exc:
        log.write(stage="role_test", id=role["id"], event="defines_ref_lookup_error", error=str(exc))
        return None
    ref = result.get("defines_ref") if isinstance(result, dict) else None
    return ref or None


ROLE_DEDUP_SYSTEM = """You are deduplicating a list of Roles kept from the \
CRA extraction pipeline. Apply this rule from the project's extraction \
methodology:

RULE: a role is NOT minted separately if it is merely an umbrella term for \
other roles already in the list (e.g. "economic operator" covering \
Manufacturer/Importer/Distributor/Authorised representative), or a \
near-duplicate naming variant of another role naming the exact same \
duty-bearing actor category. Keep the most specific, canonical name for each \
genuinely distinct duty-bearing actor category; merge variants into it.

Given the list of Role id/name pairs, decide for each: "keep" (a genuinely \
distinct actor category), "merge" (fold it into another id in the list -- \
name the target id), or "drop" (a pure umbrella term with no distinct duties \
of its own).

Respond with ONLY a JSON array:
[{"id": "...", "action": "keep"|"merge"|"drop", "merge_into": "<id>" or null, \
"reason": "short phrase"}]"""


def _build_role_test_prompt(batch: list[dict]) -> str:
    parts = []
    for role in batch:
        parts.append(
            f"id: {role['id']}\nname: {role['name']}\n"
            f"evidence:\n{evidence_text(role['chunks'])}\n"
        )
    return "\n===\n".join(parts)


async def stage1_role_test(llm: RateLimitedLLM, native, log: DecisionLog,
                            batch_size: int) -> list[dict]:
    roles = fetch_roles(native)
    for role in roles:
        role["chunks"] = fetch_mention_chunks(native, role["id"])

    kept: list[dict] = []
    for batch in _chunked(roles, batch_size):
        try:
            result = await call_json(llm, ROLE_TEST_SYSTEM, _build_role_test_prompt(batch))
        except Exception as exc:
            log.write(stage="role_test", event="batch_error", ids=[r["id"] for r in batch],
                       error=str(exc))
            continue
        by_id = {r["id"]: r for r in batch}
        for entry in result:
            role = by_id.get(entry.get("id"))
            if role is None:
                continue
            log.write(stage="role_test", id=role["id"], name=role["name"],
                       duty_bearing=entry.get("duty_bearing"),
                       defines_ref=entry.get("defines_ref"),
                       confidence=entry.get("confidence"), reason=entry.get("reason"))
            if entry.get("duty_bearing"):
                defines_ref = entry.get("defines_ref")
                if _is_missing(defines_ref):
                    defines_ref = await _resolve_missing_defines_ref(llm, native, log, role)
                    if defines_ref:
                        log.write(stage="role_test", id=role["id"],
                                   event="defines_ref_recovered", defines_ref=defines_ref)
                role["defines_ref"] = defines_ref
                kept.append(role)
    return kept


_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.I)


def _normalize_role_name(name: str) -> str:
    """Collapse leading articles/plurals so 'The notified body' and
    'Notified Body' land on the same key -- verified the dedup LLM missed
    exactly this pair, leaving a duty-bearing role's duplicate with zero
    obligations of its own."""
    name = _LEADING_ARTICLE_RE.sub("", name.strip())
    name = re.sub(r"\s+", " ", name).strip().lower()
    if name.endswith("s") and not name.endswith("ss"):
        name = name[:-1]
    return name


def _merge_article_variants(kept_roles: list[dict], log: DecisionLog) -> list[dict]:
    """Deterministic pre-pass: fold definite/indefinite-article duplicates
    into whichever variant's id has no leading article, before the LLM
    dedup pass handles remaining semantic-only duplicates."""
    groups: dict[str, list[dict]] = {}
    for role in kept_roles:
        groups.setdefault(_normalize_role_name(role["name"]), []).append(role)

    canonical: list[dict] = []
    for key, group in groups.items():
        if len(group) == 1:
            canonical.append(group[0])
            continue
        group.sort(key=lambda r: (bool(_LEADING_ARTICLE_RE.match(r["name"].strip())), r["id"]))
        target = group[0]
        seen_ids = {c["id"] for c in target["chunks"]}
        for dup in group[1:]:
            for c in dup["chunks"]:
                if c["id"] not in seen_ids:
                    target["chunks"].append(c)
                    seen_ids.add(c["id"])
            log.write(stage="role_dedup", id=dup["id"], action="merge",
                       merge_into=target["id"],
                       reason="article/plural variant (normalized name match)")
        canonical.append(target)
    return canonical


def _build_dedup_prompt(roles: list[dict]) -> str:
    return "\n".join(f"{r['id']}: {r['name']}" for r in roles)


async def stage1_dedup(llm: RateLimitedLLM, kept_roles: list[dict],
                        log: DecisionLog) -> list[dict]:
    kept_roles = _merge_article_variants(kept_roles, log)
    if not kept_roles:
        return []
    try:
        result = await call_json(llm, ROLE_DEDUP_SYSTEM, _build_dedup_prompt(kept_roles))
    except Exception as exc:
        log.write(stage="role_dedup", event="batch_error", error=str(exc))
        return kept_roles  # fail open: keep everything rather than lose data silently

    by_id = {r["id"]: r for r in kept_roles}
    kept_ids = set(by_id)
    actions: dict[str, tuple[str, str | None, str | None]] = {}
    for entry in result:
        rid = entry.get("id")
        if rid not in by_id:
            continue
        action = entry.get("action", "keep")
        merge_into = entry.get("merge_into")
        if action == "merge" and merge_into not in kept_ids:
            action = "drop"  # target doesn't exist among kept roles -- don't fabricate a link
        actions[rid] = (action, merge_into, entry.get("reason"))

    # A role merged away can carry evidence (mention-chunks) that its
    # canonical target doesn't have on its own -- e.g. "CSIRTs designated as
    # coordinators" gets folded into "CSIRTs", but the operative duty text
    # lived under the variant's own chunk mentions. Union it into the
    # target *before* Stage 2+3 scopes retrieval off the target's chunks,
    # or that evidence is silently lost.
    for rid, (action, merge_into, _reason) in actions.items():
        if action != "merge":
            continue
        target = by_id.get(merge_into)
        if target is None or by_id.get(merge_into) is None:
            continue
        target_action = actions.get(merge_into, ("keep", None, None))[0]
        if target_action != "keep":
            continue  # target isn't itself a keeper -- don't chain, just drop below
        seen_ids = {c["id"] for c in target["chunks"]}
        for c in by_id[rid]["chunks"]:
            if c["id"] not in seen_ids:
                target["chunks"].append(c)
                seen_ids.add(c["id"])

    canonical: list[dict] = []
    for rid, (action, merge_into, reason) in actions.items():
        log.write(stage="role_dedup", id=rid, action=action, merge_into=merge_into, reason=reason)
        if action == "keep":
            canonical.append(by_id[rid])
    return canonical


# === Stage 2+3: Requirement test + Obligation derivation (sequential per Role) ===

REQ_OBL_SYSTEM = """You are curating Requirement and Obligation entities \
extracted from the EU Cyber Resilience Act (CRA), scoped to a single Role.

REQUIREMENT TEST: keep a Requirement only if it states an operative "shall" \
duty. Exclude: permissive text ("may") unless it embeds a real conditional \
shall; institutional/procedural text that doesn't impose a duty on an \
economic operator (Commission delegated-act powers, committee procedure, \
CSIRT/ENISA/ADCO internal mechanics, submission-routing mechanics, pure \
definitions); duplicate restatements of a duty already captured by another \
kept Requirement in this batch.

OBLIGATION TEST: for each kept Requirement, choose the canonical Obligation \
among its candidates that best names the generic duty it establishes (a \
short imperative phrase, e.g. "Conduct Cybersecurity Risk Assessment"), or \
write one if no candidate fits well. obligation_type is "technical" (a \
property/mechanism the product must have) or "organizational" (a process the \
responsible party must run). confidence reflects how directly the \
Requirement text maps to the Obligation's phrasing (near-verbatim: \
0.9-0.95; more paraphrase/judgment: 0.75-0.85).

REQUIREMENT TEXT: for each kept Requirement, also write requirement_text -- \
a concise phrase describing what the requirement actually establishes, from \
the evidence. Never just its citation (e.g. not "Article 15" -- say what \
Article 15 requires); the input's own requirement_name is sometimes a bare \
citation the extractor fell back to, so this field must independently \
describe content, not repeat it.

Respond with ONLY a JSON array, one object per requirement_id in the input:
[{"requirement_id": "...", "keep": true|false, "reason": "short phrase", \
"requirement_text": "..." (required if keep), \
"obligation_text": "..." (required if keep), \
"obligation_type": "technical"|"organizational" (required if keep), \
"confidence": 0.0-1.0 (required if keep), "source_ref": "Art. X.Y" or null}]"""


def _build_req_obl_prompt(role: dict, requirements: list[dict]) -> str:
    parts = [f'Role: "{role["name"]}"\n']
    for req in requirements:
        cand_lines = "\n".join(
            f"  - {c['id']}: {c['name']!r} (native type={c.get('obligation_type')}, "
            f"native confidence={c.get('confidence')})"
            for c in req["obligation_candidates"]
        ) or "  (none found)"
        parts.append(
            f"requirement_id: {req['id']}\n"
            f"requirement_name: {req['name']!r}\n"
            f"evidence:\n{req['evidence']}\n"
            f"obligation_candidates:\n{cand_lines}\n"
        )
    return "\n===\n".join(parts)


def _canonical_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


async def stage2_3_requirements_obligations(
    llm: RateLimitedLLM, native, final, log: DecisionLog,
    role: dict, regulation_id: str, obligation_registry: dict[str, str],
    batch_size: int,
) -> list[dict]:
    """Returns the list of kept Obligation dicts (id, name, text) written for this Role."""
    role_chunk_ids = [c["id"] for c in role["chunks"]]
    req_candidates = fetch_co_mentioned(native, "Requirement", role_chunk_ids)
    kept_obligations: list[dict] = []

    # Built eagerly, not per-LLM-batch, so we know which native Obligation
    # entities were already reachable through some Requirement's own
    # chunk-scope before adding the rest below. Obligations directly
    # co-mentioned with the Role's own chunks but never reachable that way
    # -- e.g. importer/distributor's duties, which the SDK linked straight
    # from extraction with no Requirement chunk-overlap at all -- would
    # otherwise never become a candidate for this Role.
    enriched: list[dict] = []
    seen_obl_ids: set[str] = set()
    for req in req_candidates:
        req_chunks = fetch_mention_chunks(native, req["id"])
        obl_candidates = fetch_co_mentioned(native, "Obligation", [c["id"] for c in req_chunks])
        seen_obl_ids.update(c["id"] for c in obl_candidates)
        enriched.append({
            "id": req["id"], "name": req["name"],
            "evidence": evidence_text(req_chunks),
            "obligation_candidates": obl_candidates,
        })

    direct_obls = [
        o for o in fetch_co_mentioned(native, "Obligation", role_chunk_ids)
        if o["id"] not in seen_obl_ids
    ]
    for obl in direct_obls:
        obl_chunks = fetch_mention_chunks(native, obl["id"])
        enriched.append({
            "id": f"{obl['id']}__direct", "name": obl["name"],
            "evidence": evidence_text(obl_chunks),
            "obligation_candidates": [obl],
        })

    for batch in _chunked(enriched, batch_size):
        try:
            result = await call_json(llm, REQ_OBL_SYSTEM, _build_req_obl_prompt(role, batch))
        except Exception as exc:
            log.write(stage="req_obl", role_id=role["id"], event="batch_error", error=str(exc))
            continue

        by_id = {r["id"]: r for r in batch}
        for entry in result:
            rid = entry.get("requirement_id")
            req = by_id.get(rid)
            if req is None:
                continue
            keep = entry.get("keep")
            log.write(stage="req_obl", role_id=role["id"], requirement_id=rid,
                       keep=keep, reason=entry.get("reason"),
                       requirement_text=entry.get("requirement_text"),
                       obligation_text=entry.get("obligation_text"))
            if not keep:
                continue

            obl_text = (entry.get("obligation_text") or "").strip()
            if not obl_text:
                continue
            key = _canonical_key(obl_text)
            owner = obligation_registry.get(key)
            if owner is not None and owner != role["id"]:
                obl_text = f"{obl_text} as {role['name']}"
            obligation_registry[_canonical_key(obl_text)] = role["id"]

            # requirement_text is the LLM's own description of the evidence
            # it just read; req["name"] is only a fallback for when the LLM
            # omits it, since that's sometimes the bare citation ("Article
            # 15") the native extractor fell back to -- not descriptive.
            req_text = (entry.get("requirement_text") or "").strip() or req["name"]

            obl_id = f"{req['id']}__obl"
            write_node(final, "Requirement", req["id"], req_text,
                       source_ref=entry.get("source_ref"))
            write_edge(final, "Regulation", regulation_id, "EXPRESSES", "Requirement", req["id"],
                       source_ref=entry.get("source_ref"))
            write_node(final, "Obligation", obl_id, obl_text,
                       obligation_type=entry.get("obligation_type"),
                       confidence=entry.get("confidence"))
            write_edge(final, "Role", role["id"], "HAS", "Obligation", obl_id)
            write_edge(final, "Requirement", req["id"], "SATISFIED_BY", "Obligation", obl_id)

            kept_obligations.append({
                "id": obl_id, "name": obl_text, "requirement_id": req["id"],
            })

    return kept_obligations


# === Stage 4: Capability convergence (sequential per Obligation) ===

CAPABILITY_SYSTEM = """You are mapping an Obligation to the underlying \
Capability it requires, for a CRA compliance graph.

TEST: identify the capacity, independent of who holds the duty, that would \
satisfy this Obligation. Before minting a new Capability, check whether it \
is already named by one of the EXISTING capabilities listed (same \
underlying capacity even if phrased differently) -- if so, reuse it. Only \
propose a new Capability if none of the existing ones fit; keep new \
Capability names specific enough to name a distinct technical or \
organizational capacity, not a vague generic bucket.

Respond with ONLY one JSON object:
{"action": "reuse"|"new", "capability_id": "<id of existing capability>" or \
null, "name": "<name, required if action=new>", \
"capability_type": "technical"|"organizational", "reason": "short phrase"}"""


def _build_capability_prompt(obligation_name: str, native_candidates: list[dict],
                              registry: list[dict]) -> str:
    existing = "\n".join(f"  - {c['id']}: {c['name']!r}" for c in registry) or "  (none yet)"
    native_c = "\n".join(f"  - {c['name']!r}" for c in native_candidates) or "  (none found)"
    return (
        f"Obligation: {obligation_name!r}\n\n"
        f"EXISTING capabilities already in the graph:\n{existing}\n\n"
        f"Native extraction candidates near this Obligation (for inspiration, "
        f"not authoritative):\n{native_c}"
    )


async def stage4_capability(
    llm: RateLimitedLLM, native, final, log: DecisionLog,
    obligation: dict, capability_registry: list[dict],
) -> None:
    req_chunks = fetch_mention_chunks(native, obligation["requirement_id"])
    native_candidates = fetch_co_mentioned(native, "Capability", [c["id"] for c in req_chunks])

    try:
        entry = await call_json(
            llm, CAPABILITY_SYSTEM,
            _build_capability_prompt(obligation["name"], native_candidates, capability_registry),
        )
    except Exception as exc:
        log.write(stage="capability", obligation_id=obligation["id"], event="error", error=str(exc))
        return

    action = entry.get("action")
    if action == "reuse":
        cap_id = entry.get("capability_id")
        cap = next((c for c in capability_registry if c["id"] == cap_id), None)
        if cap is None:
            action = "new"  # LLM named an id that isn't in the registry -- fall back to minting
        else:
            log.write(stage="capability", obligation_id=obligation["id"], action="reuse",
                       capability_id=cap["id"], reason=entry.get("reason"))
            write_edge(final, "Obligation", obligation["id"], "REQUIRES", "Capability", cap["id"])
            return

    name = (entry.get("name") or "").strip()
    if not name:
        log.write(stage="capability", obligation_id=obligation["id"], event="no_name_skipped")
        return
    cap_id = f"cap_{re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')}"
    log.write(stage="capability", obligation_id=obligation["id"], action="new",
               capability_id=cap_id, name=name, reason=entry.get("reason"))
    # final-graph shape uses `type`, not the native `capability_type` --
    # schema.py's DEFECT-1 fix renamed the *native* property specifically to
    # avoid colliding with the SDK's `n.type` label discriminator; that
    # constraint doesn't apply here since final_full nodes carry real
    # FalkorDB labels, not a generic __Entity__ shape. compare.py's own
    # DEFECT-1 check expects `type` on the final Capability node.
    write_node(final, "Capability", cap_id, name,
               type=entry.get("capability_type"))
    write_edge(final, "Obligation", obligation["id"], "REQUIRES", "Capability", cap_id)
    capability_registry.append({"id": cap_id, "name": name})


# === Orchestration ===

async def run(args: argparse.Namespace) -> None:
    db = FalkorDB(host=args.host, port=args.port)
    native = db.select_graph(args.native_graph)

    if args.final_graph in db.list_graphs():
        db.select_graph(args.final_graph).delete()
    final = db.select_graph(args.final_graph)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log = DecisionLog(LOG_DIR / f"curate-{ts}.jsonl")
    t0 = time.time()

    llm = build_llm(max_concurrency=args.max_concurrency)
    regulation = load_regulation(args.regulation_map_path)

    print("Stage 1: Role test...")
    all_roles = fetch_roles(native)
    kept_roles = await stage1_role_test(llm, native, log, args.role_batch_size)
    print(f"  {len(kept_roles)} / {len(all_roles)} roles duty-bearing")

    if args.dry_run:
        for r in kept_roles:
            print(f"    KEEP {r['id']}: {r['name']} ({r.get('defines_ref')})")
        log.write(stage="summary", event="dry_run_complete",
                   total_roles=len(all_roles), kept_roles=len(kept_roles))
        log.close()
        return

    print("Stage 1: Role dedup...")
    canonical_roles = await stage1_dedup(llm, kept_roles, log)
    print(f"  {len(canonical_roles)} canonical roles after dedup")

    if args.limit:
        canonical_roles = canonical_roles[:args.limit]
        print(f"  --limit applied: processing {len(canonical_roles)} roles")

    for role in canonical_roles:
        expand_with_adjacent_chunks(native, role)

    write_node(final, "Regulation", regulation["id"], regulation["name"])
    for role in canonical_roles:
        write_node(final, "Role", role["id"], role["name"])
        write_edge(final, "Regulation", regulation["id"], "DEFINES", "Role", role["id"],
                   source_ref=role.get("defines_ref"))

    obligation_registry: dict[str, str] = {}
    capability_registry: list[dict] = []
    total_obligations = 0

    for i, role in enumerate(canonical_roles, 1):
        print(f"Stage 2+3 [{i}/{len(canonical_roles)}]: {role['name']}...")
        kept_obls = await stage2_3_requirements_obligations(
            llm, native, final, log, role, regulation["id"], obligation_registry,
            args.req_batch_size,
        )
        print(f"  {len(kept_obls)} obligations kept")
        total_obligations += len(kept_obls)

        for obl in kept_obls:
            await stage4_capability(llm, native, final, log, obl, capability_registry)

    duration_ms = int((time.time() - t0) * 1000)
    log.write(stage="summary", event="run_complete",
               total_roles=len(all_roles), kept_roles=len(kept_roles),
               canonical_roles=len(canonical_roles), total_obligations=total_obligations,
               total_capabilities=len(capability_registry), duration_ms=duration_ms)
    log.close()
    print(f"Done in {duration_ms/1000:.1f}s: {len(canonical_roles)} roles, "
          f"{total_obligations} obligations, {len(capability_registry)} capabilities.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--native-graph", default="policy_system_graphrag_native_full")
    parser.add_argument("--final-graph", default="policy_system_graphrag_final_full")
    parser.add_argument("--regulation-map-path", default="regulation_map.json")
    parser.add_argument("--role-batch-size", type=int, default=DEFAULT_ROLE_BATCH_SIZE)
    parser.add_argument("--req-batch-size", type=int, default=DEFAULT_REQ_BATCH_SIZE)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true",
                        help="Run Stage 1 only (Role test), print kept/dropped, write nothing.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N canonical roles end-to-end.")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
