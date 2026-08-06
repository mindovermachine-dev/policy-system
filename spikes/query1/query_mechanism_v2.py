#!/usr/bin/env python3
"""Approach 2: agentic tool-use over the live graph, for what approach 1 can't reach.

See q-approach2.md for the design rationale, including why this is *not*
built on FalkorDB's GraphRAG-SDK (its installed API turned out to be a
document-ingestion + hybrid-chunk-retrieval package, not a fit for a graph
that's already structured and already correctly extracted).

`QueryMechanismV2.ask()` always tries `query_mechanism_v1.QueryMechanismV1`
first -- unchanged, imported directly, zero LLM cost. Only on
`NoTemplateMatch` does this module hand the question to an LLM agent with
three tools (`list_entities`, `run_cypher`, `whole_graph_stats`). See
q-approach2.md for why there are exactly three, and why `whole_graph_stats`
exists as pre-computed Python rather than one more thing the model
freehands in Cypher (the FalkorDB 6-hop projection bug documented in
q-approach1.md is the reason: whole-graph aggregate queries are exactly the
shape that broke silently there).

No `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` is configured in this environment
(checked directly -- see q-approach2.md). `LLMClient` is a Protocol; the
default implementation (`NoLLMConfigured`) fails loudly and specifically
rather than silently returning nothing, so that gap is never hidden behind
a vague error.
"""

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from query_mechanism_v1 import FIXTURE_ANCHOR_DATE, NoTemplateMatch, QueryMechanismV1

MAX_AGENT_TURNS = 8

# Sample the agent this many times per question and mechanically combine, per
# q-approach2.md's "Further experiments" section: of four combination ideas
# tested (self-consistency/union, temperature, generator+validator loop, union
# + validator synthesis), plain union-of-N with regex-extracted cited ids was
# the only one with unambiguous positive evidence (7/7 on the test question in
# experiment_self_consistency.py, vs. 0/7 for strict consensus). The honest
# tradeoff, also noted there: this multiplies LLM calls per v2-agent-routed
# question by UNION_RUNS_DEFAULT -- v1's template path is unaffected, it never
# reaches this.
UNION_RUNS_DEFAULT = 3

# Anything other than a pure read is refused before it ever reaches FalkorDB.
# Deliberately blunt (keyword match, not a Cypher parser) -- same "fail
# loudly rather than get clever" instinct as v1's EntityResolver.
_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|SET|REMOVE|DROP|CALL\s+db\.|FOREACH)\b", re.I
)


class ReadOnlyViolation(Exception):
    def __init__(self, query: str):
        super().__init__(f"refusing to run a non-read-only query: {query!r}")


def _assert_read_only(query: str) -> None:
    if _WRITE_KEYWORDS.search(query):
        raise ReadOnlyViolation(query)


# --------------------------------------------------------------------------
# Deterministic relationship-direction correction
# --------------------------------------------------------------------------
#
# Per q-approach3.md: relationship-direction reversal (`(pol)<-[:SUPPORTED_BY]-
# (:Standard)` instead of the schema's `Policy-[:SUPPORTED_BY]->Standard`, seen
# for real on H1 and H11 -- see q-approach2.md's "Result" table) is a solved
# problem, and not by better prompting. There's a public competition for
# exactly this (github.com/tomasonjo/cypher-direction-competition) and its
# winning approach ships in LangChain as `CypherQueryCorrector`: parse the
# generated query, compare relationship directions against the schema,
# correct them where a query's own node labels make the correct direction
# unambiguous. No model judgment -- the same category of fix `_annotate_trust`
# above already represents elsewhere in this codebase (move a decision out of
# prose and into deterministic Python).
#
# Single source of truth for canonical (from_label, to_label) per relationship
# type -- every edge in this schema has exactly one valid direction regardless
# of which node is written first, so the type name alone determines it once
# both endpoint labels are known. Kept in sync with GRAPH_SCHEMA's prose below
# by test_query_mechanism_v2.py.
SCHEMA_RELATIONSHIP_DIRECTIONS: dict[str, tuple[str, str]] = {
    "DEFINES": ("Regulation", "Role"),
    "HAS": ("Role", "Obligation"),
    "REQUIRES": ("Obligation", "Capability"),
    "GOVERNED_BY": ("Capability", "Policy"),
    "SUPPORTED_BY": ("Policy", "Standard"),
    "IMPLEMENTED_BY": ("Standard", "Control"),
    "EXPRESSES": ("Regulation", "Requirement"),
    "SATISFIED_BY": ("Requirement", "Obligation"),
    "SUPERSEDED_BY": ("Regulation", "Regulation"),
}

# A node pattern: `(`, optional variable, optional `:Label`, anything else
# that isn't a paren (property map, more labels, whitespace), `)`. Deliberately
# permissive -- it also matches non-path parenthesized text (`count(ctrl)`,
# `IN ('a','b')`), but those false positives are harmless: correction below
# only fires when *both* sides of a hop resolve to a label that matches the
# schema's pair for that relationship type, which a stray function-call token
# essentially never does.
_NODE_PATTERN = re.compile(
    r"\(\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)?\s*(?::(?P<label>[A-Za-z_][A-Za-z0-9_]*))?[^()]*\)"
)
# What can sit between two node patterns for it to be a single-type,
# fixed-length relationship arrow. Multi-type (`[:A|B]`) and variable-length
# (`[:R*1..3]`) patterns intentionally fall outside this and are left alone --
# same "can't correct what it can't verify" instinct as the label check below.
_REL_ARROW = re.compile(r"(?P<left_arrow><)?-\[(?P<rel_body>[^\]]*)\]-(?P<right_arrow>>)?")
# Anchored at the start of rel_body: optional variable, then `:TYPE`. The
# trailing group catches a `|` (multi-type, `[:A|B]`) or `*` (variable-length,
# `[:R*1..3]`) immediately following the type name, so those can be rejected
# by the caller rather than mis-corrected.
_REL_TYPE = re.compile(r"^\s*(?:[A-Za-z_]\w*\s*)?:\s*([A-Za-z_]\w*)\s*([|*]?)")


def _single_rel_type(rel_body: str) -> Optional[str]:
    m = _REL_TYPE.match(rel_body)
    if not m or m.group(2):
        return None
    return m.group(1)


@dataclass
class DirectionCorrection:
    relationship: str
    original: str
    corrected: str


def correct_relationship_directions(query: str) -> tuple[str, list[DirectionCorrection]]:
    """Parse `query` for `(a)-[:REL]->(b)` / `(a)<-[:REL]-(b)` hops and flip
    any arrow that runs backwards relative to `SCHEMA_RELATIONSHIP_DIRECTIONS`,
    wherever both endpoints' labels are known (inline, or bound earlier in the
    same query text) and unambiguously identify which orientation is correct.
    Node write order and everything outside the arrow itself is left
    untouched -- a query legitimately traversing Control back to Standard
    with a correct backward arrow is not touched, only a reversed one is.

    Returns the (possibly rewritten) query and a list of corrections made, so
    callers can log/surface what changed rather than silently rewriting.
    """
    var_labels: dict[str, str] = {}
    for m in _NODE_PATTERN.finditer(query):
        if m.group("var") and m.group("label"):
            var_labels[m.group("var")] = m.group("label")

    node_matches = list(_NODE_PATTERN.finditer(query))
    corrections: list[DirectionCorrection] = []
    replacements: list[tuple[int, int, str]] = []

    for left, right in zip(node_matches, node_matches[1:]):
        between = query[left.end() : right.start()]
        rel_match = _REL_ARROW.fullmatch(between.strip())
        if not rel_match:
            continue

        rel_type = _single_rel_type(rel_match.group("rel_body"))
        if rel_type is None:
            continue
        canonical = SCHEMA_RELATIONSHIP_DIRECTIONS.get(rel_type)
        if canonical is None or canonical[0] == canonical[1]:
            # Same label both ends (SUPERSEDED_BY: Regulation->Regulation) --
            # labels alone can't tell "from" from "to", so this can't be
            # verified and is left untouched, same as an unresolvable label.
            continue

        left_label = left.group("label") or var_labels.get(left.group("var"))
        right_label = right.group("label") or var_labels.get(right.group("var"))
        if left_label is None or right_label is None:
            continue

        from_label, to_label = canonical
        if left_label == from_label and right_label == to_label:
            wanted_forward = True
        elif left_label == to_label and right_label == from_label:
            wanted_forward = False
        else:
            continue  # labels don't fit either orientation -- not this function's call

        is_forward = bool(rel_match.group("right_arrow")) and not rel_match.group("left_arrow")
        is_backward = bool(rel_match.group("left_arrow")) and not rel_match.group("right_arrow")
        if not (is_forward or is_backward) or is_forward == wanted_forward:
            continue  # undirected (matches both ways already) or already correct

        rel_body = rel_match.group("rel_body")
        new_arrow = f"-[{rel_body}]->" if wanted_forward else f"<-[{rel_body}]-"
        leading_ws = between[: len(between) - len(between.lstrip())]
        trailing_ws = between[len(between.rstrip()) :]
        new_between = f"{leading_ws}{new_arrow}{trailing_ws}"

        replacements.append((left.end(), right.start(), new_between))
        corrections.append(DirectionCorrection(relationship=rel_type, original=between, corrected=new_between))

    if not replacements:
        return query, []

    result, pos = [], 0
    for start, end, new_text in replacements:
        result.append(query[pos:start])
        result.append(new_text)
        pos = end
    result.append(query[pos:])
    return "".join(result), corrections


# --------------------------------------------------------------------------
# Entity-id extraction, used by union-of-N to score each sampled run's
# citation coverage. Originally built and evaluated in
# experiment_citation_completeness.py as a candidate *standalone* completeness
# gate (q-approach2.md's "Next" item 3) -- that role was rejected there (see
# q-approach2.md's "Citation completeness as a deterministic post-check"
# section: too imprecise against exact-id substring matching, blind to
# several real failure classes, one clean catch against union-of-N's
# already-unambiguous 7/7). Reused here for a narrower, better-evidenced job:
# not judging any single run pass/fail, just comparing sampled runs against
# each other to pick which one covers the union best. Every entity id in this
# graph is either one of these slugified-name-plus-hash forms
# (build_helvex_graph.py's `f"{prefix}_{slugify(name)}_{content_hash(...)}"`)
# or a Regulation/Requirement id (GRAPH_SCHEMA's documented shape).
# --------------------------------------------------------------------------

_PREFIXED_ID = re.compile(r"\b(?:role|cap|obl|pol|std|ctrl)_[a-z0-9]+(?:_[a-z0-9]+)*\b")
_REG_OR_REQ_ID = re.compile(r"\b[A-Z][A-Z-]*-\d+\.\d+(?:_req_art_[\d.]+[a-z]?)?\b")


def extract_entity_ids(value: Any) -> set[str]:
    """Pull every real-entity-id-shaped token out of `value` (an answer
    string, a tool result dict, or anything else JSON-serializable).
    Serializing to JSON first and running both patterns over the text is
    deliberately simpler than walking the structure by hand -- ids never
    legitimately span a JSON delimiter, so this loses nothing a manual
    recursive walk would catch, for a fraction of the code.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return set(_PREFIXED_ID.findall(text)) | set(_REG_OR_REQ_ID.findall(text))


# --------------------------------------------------------------------------
# LLM seam
# --------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMTurn:
    """One model turn: either a final answer, or a batch of tool calls."""

    text: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    """Deliberately small: one method, provider-agnostic message/tool shape
    close to Anthropic/OpenAI's tool-use conventions so a real provider is a
    thin adapter, not a rewrite. Modeled after graphrag_sdk.LLMInterface's
    invoke(prompt) -> LLMResponse shape (see q-approach2.md) but defined
    fresh here rather than imported, to avoid pulling graphrag-sdk's ~10
    heavy ML dependencies into a module that doesn't use its retrieval
    pipeline at all.
    """

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMTurn: ...


class NoLLMConfigured:
    """Default LLMClient. Fails loudly and specifically -- see module docstring."""

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMTurn:
        raise RuntimeError(
            "Approach 2 needs an LLM provider and none is configured in this "
            "environment (no ANTHROPIC_API_KEY / OPENAI_API_KEY / equivalent "
            "found -- see q-approach2.md's 'Environment constraint' section). "
            "Pass a real LLMClient implementation to QueryMechanismV2 to "
            "exercise this path; see test_query_mechanism_v2.py's "
            "FakeLLMClient for the plumbing-only alternative, or OllamaClient "
            "below for a real local model."
        )


class OllamaClient:
    """Real LLMClient against a local Ollama server, via its OpenAI-compatible
    endpoint (no cloud API key needed -- this is what closes the gap
    q-approach2.md's 'Environment constraint' section flagged, once Ollama
    is actually running locally with a downloaded model).

    Deliberately NOT routed through a general-purpose coding-agent harness
    (e.g. `pi`, also available in this environment): those harnesses give a
    fixed tool set (read/bash/edit/write) aimed at repo editing, not typed
    domain tools. Getting our three FalkorDB tools in front of one would
    mean a bash-CLI-wrapper indirection (the model shells out to a script,
    parses text back) instead of native structured tool-calling -- strictly
    worse for this problem, not a harness we'd be "fighting" by not using.
    The actual harness this problem needs is the agent loop already built
    and tested in QueryMechanismV2._ask_agent -- this class's only job is
    to make `LLMClient.complete()` real.

    Lazy-imports `openai` (already installed in this environment) inside
    __init__ rather than at module level, so the rest of this module has no
    hard dependency on it.
    """

    def __init__(self, model: str = "qwen3:14b", base_url: str = "http://127.0.0.1:11434/v1"):
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(base_url=base_url, api_key="ollama")

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> LLMTurn:
        import json

        oa_messages = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                oa_messages.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                oa_messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                            }
                            for tc in m["tool_calls"]
                        ],
                    }
                )
            elif m["role"] == "tool":
                oa_messages.append(
                    {"role": "tool", "tool_call_id": m["tool_call_id"], "content": json.dumps(m["content"], default=str)}
                )

        oa_tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
            for t in tools
        ]
        resp = self._client.chat.completions.create(model=self.model, messages=oa_messages, tools=oa_tools)
        msg = resp.choices[0].message

        if msg.tool_calls:
            calls = [
                ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments))
                for tc in msg.tool_calls
            ]
            return LLMTurn(tool_calls=calls)
        return LLMTurn(text=msg.content or "")


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

_LISTABLE_LABELS = {
    "Role": "name",
    "Capability": "name",
    "Policy": "title",
    "Regulation": "id",
}


class ToolBox:
    """The three tools handed to the agent. Wraps the same live graph
    connection QueryMechanismV1 already opened -- one connection, shared.
    """

    def __init__(self, graph):
        self.graph = graph

    def list_entities(self, label: str) -> list[str]:
        if label not in _LISTABLE_LABELS:
            raise ValueError(f"list_entities: unknown label {label!r}, expected one of {sorted(_LISTABLE_LABELS)}")
        prop = _LISTABLE_LABELS[label]
        rows = self.graph.query(f"MATCH (n:{label}) RETURN DISTINCT n.{prop} ORDER BY n.{prop}").result_set
        return [r[0] for r in rows if r[0] is not None]

    def run_cypher(self, query: str) -> dict:
        _assert_read_only(query)
        query, corrections = correct_relationship_directions(query)
        result = self.graph.query(query)
        columns = [c[1] for c in result.header] if result.header else []
        rows = [list(r) for r in result.result_set]
        response = {"columns": columns, "rows": rows}
        if corrections:
            # Surfaced to the model, not applied silently -- it should know its
            # own query had a reversed relationship, the same way it sees a
            # tool error, not have it invisibly fixed underneath it.
            response["direction_corrected"] = [
                f"{c.relationship}: {c.original.strip()!r} -> {c.corrected.strip()!r}" for c in corrections
            ]
        return response

    def whole_graph_stats(self) -> dict:
        """Pre-computed, deterministic aggregate facts for the genuinely
        'global' questions (H12/H13/H14) -- the model's job for these is
        narration, never arithmetic over rows it pulled itself. Every query
        here is the same one already verified against golden values in
        test_query_mechanism_v1.py (H2/M9/M10/M11/M12/M7), not a fresh
        untested aggregate.
        """
        g = self.graph

        total_capabilities = g.query("MATCH (c:Capability) RETURN count(c)").result_set[0][0]
        ungoverned = g.query(
            "MATCH (c:Capability) WHERE NOT (c)-[:GOVERNED_BY]->(:Policy) RETURN count(c)"
        ).result_set[0][0]

        policy_status = {
            status: n
            for status, n in g.query("MATCH (p:Policy) RETURN p.status, count(p)").result_set
        }

        overdue_controls = [
            {"id": cid, "title": title, "next_review_date": d}
            for cid, title, d in g.query(
                "MATCH (ctrl:Control) WHERE ctrl.next_review_date < $anchor "
                "AND ctrl.implementation_status <> 'deprecated' "
                "RETURN ctrl.id, ctrl.title, ctrl.next_review_date",
                params={"anchor": FIXTURE_ANCHOR_DATE},
            ).result_set
        ]

        planned_controls = [
            {"id": cid, "title": title}
            for cid, title in g.query(
                "MATCH (ctrl:Control {implementation_status:'planned'}) RETURN ctrl.id, ctrl.title"
            ).result_set
        ]

        governed_no_implemented_control = [
            {"capability_id": cid, "capability_name": name, "policy_id": pid, "policy_status": pstatus}
            for cid, name, pid, pstatus in g.query(
                "MATCH (c:Capability)-[:GOVERNED_BY]->(p:Policy) "
                "OPTIONAL MATCH (p)-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->(ctrl:Control) "
                "WITH c, p, collect(DISTINCT ctrl.implementation_status) AS statuses "
                "WHERE NOT 'implemented' IN statuses "
                "RETURN c.id, c.name, p.id, p.status"
            ).result_set
        ]

        gdpr_chains = g.query(
            "MATCH (req:Requirement)-[:SATISFIED_BY]->(o:Obligation)-[:REQUIRES]->(c:Capability)"
            "-[:GOVERNED_BY]->(p:Policy)-[:SUPPORTED_BY]->(s:Standard)-[:IMPLEMENTED_BY]->(ctrl:Control) "
            "WHERE req.id STARTS WITH 'GDPR' "
            "RETURN req.id, o.id, c.id, p.id, p.status, s.id, s.implementation_status, ctrl.id, ctrl.implementation_status"
        ).result_set
        current = sum(
            1
            for row in gdpr_chains
            if row[4] == "approved" and row[6] in ("implemented", "reviewed") and row[8] == "implemented"
        )

        return {
            "fixture_reference_date": FIXTURE_ANCHOR_DATE,
            "capabilities_total": total_capabilities,
            "capabilities_ungoverned": ungoverned,
            "capabilities_governed": total_capabilities - ungoverned,
            "policy_status_breakdown": policy_status,
            "controls_overdue_for_review": overdue_controls,
            "controls_planned_not_yet_implemented": planned_controls,
            "capabilities_governed_but_zero_implemented_controls": governed_no_implemented_control,
            "gdpr_requirement_to_control_chains": {
                "total": len(gdpr_chains),
                "current_evidence": current,
                "stale_or_not_yet_current": len(gdpr_chains) - current,
            },
        }


TOOL_SPECS = [
    ToolSpec(
        name="list_entities",
        description=(
            "List every real name/title/id for a node label, so free text ('MFA', 'SBOM', "
            "'PII') can be matched against actual graph vocabulary instead of guessed. "
            "One of: Role, Capability, Policy, Regulation."
        ),
        input_schema={
            "type": "object",
            "properties": {"label": {"type": "string", "enum": sorted(_LISTABLE_LABELS)}},
            "required": ["label"],
        },
    ),
    ToolSpec(
        name="run_cypher",
        description=(
            "Run a read-only Cypher query against the live policy_system graph and return "
            "columns + rows. Writes (CREATE/MERGE/DELETE/SET/...) are rejected. The full graph "
            "schema (node labels, relationship directions, exact property names) is given in "
            "the system prompt -- re-read it before writing a query, not just the first time."
        ),
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
    ToolSpec(
        name="whole_graph_stats",
        description=(
            "Pre-computed aggregate facts about the whole graph's governance/staleness state "
            "-- ungoverned-capability count, Policy status breakdown, overdue/planned Controls, "
            "capabilities with a Policy but no working Control. Use this instead of writing your "
            "own whole-graph aggregate Cypher; those numbers are independently verified, yours "
            "would not be."
        ),
        input_schema={"type": "object", "properties": {}},
    ),
]

# Factored out of SYSTEM_PROMPT so it can be reused verbatim wherever else grounding
# matters -- e.g. experiment_validator_loop.py's VALIDATOR_SYSTEM_GROUNDED variant,
# built to test whether the validator's own accuracy has the same root cause the
# generator's did (see q-approach2.md's "Was the validator cold-called?" section).
GRAPH_SCHEMA = """Graph schema -- re-read this before every run_cypher call, not just once:

Relationship chains (direction matters -- an edge only matches the direction shown,
querying it backwards silently returns zero rows, it does not error):
  (:Regulation)-[:DEFINES]->(:Role)-[:HAS]->(:Obligation)-[:REQUIRES]->(:Capability)
    -[:GOVERNED_BY]->(:Policy)-[:SUPPORTED_BY]->(:Standard)-[:IMPLEMENTED_BY]->(:Control)
  (:Regulation)-[:EXPRESSES]->(:Requirement)-[:SATISFIED_BY]->(:Obligation)
  (:Regulation)-[:SUPERSEDED_BY]->(:Regulation)

Exact property names per label -- a query using any other property name for a label
will silently match nothing, not error:
  Regulation: id (e.g. 'GDPR-1.0', 'CRA-1.0'), title, source_type, effective_date, status.
    No 'name' property.
  Role: name, description. No 'title'.
  Requirement: id (e.g. 'GDPR-1.0_req_art_32.1a'), text, type, status. No 'title'.
  Obligation: id, text, confidence, obligation_type. No 'title'.
  Capability: id, name, description, type, status.
  Policy: id, title, description, owner_id, status, version. No 'name'.
  Standard: id, title, description, implementation_status, version.
  Control: id, type, title, description, implementation_status, execution_frequency,
    last_test_date, next_review_date, evidence_ref.

Requirement ids encode article + sub-clause, e.g. 'GDPR-1.0_req_art_18.2b'. A single
article commonly has several Requirement ids (a base clause plus lettered
sub-clauses, e.g. '..._art_18.2', '..._art_18.2a', '..._art_18.2b'...) -- the
sub-clause suffix extends past the bare article number, it doesn't equal it. To find
every Requirement under one article, use `req.id CONTAINS "art_18"` or
`req.id STARTS WITH "GDPR-1.0_req_art_18"`. Do NOT use `req.id ENDS WITH "art_18"` to
mean "this article and everything under it" -- that only matches an id that ends
*exactly* there, which will silently miss every lettered sub-clause."""

SYSTEM_PROMPT = f"""You answer questions about the Policy System compliance graph \
(FalkorDB, graph name policy_system) using the tools provided. You have no other \
source of truth about this graph's contents -- do not answer from memory or \
plausible-sounding inference.

{GRAPH_SCHEMA}

Rules, non-negotiable:
1. Never state a fact about the graph's contents (an id, a status, a count, a chain) \
that you did not just retrieve via a tool call in this conversation.
2. If list_entities finds no plausible match for something the question describes \
(e.g. no capability resembles "rate limiting"), or run_cypher returns zero rows, say \
so explicitly as your answer. Do not fill the gap with a plausible guess. But a zero-row \
result from a query YOU wrote is not by itself proof the data doesn't exist -- it may mean \
you used the wrong property name. Before concluding something isn't in the graph, re-check \
the property names you used against the schema given for run_cypher, or use list_entities \
to confirm the real vocabulary, then retry. Concluding "not tracked" from an unverified \
query is treated as a wrong answer, same as a fabricated one.
6. When a query returns multiple rows, your answer must account for all of them, not a \
subset you find most relevant. Dropping rows you actually retrieved is treated the same as \
fabricating an answer that ignores them.
3. Whenever your answer relies on a chain that passes through Policy, Standard, or \
Control, state the status of each (approved/draft/deprecated, implemented/reviewed/ \
planned/deprecated) -- a chain through a deprecated Policy or a planned Control is not \
current evidence, and presenting it without that caveat is treated as a wrong answer \
even if the chain itself is real.
4. Cite the real ids/names you retrieved. "Several obligations require this" is not \
an acceptable citation; the specific obligation ids are.
5. For whole-graph/no-named-entity questions (e.g. "where are we most exposed," "give \
me an overview"), call whole_graph_stats rather than writing your own aggregate Cypher.
"""


# --------------------------------------------------------------------------
# Mechanism
# --------------------------------------------------------------------------


class AgentTurnLimitExceeded(Exception):
    pass


@dataclass
class MechanismResult:
    question: str
    mechanism: str  # "v1-template" or "v2-agent"
    answer: str
    template: Optional[str] = None
    tool_calls_made: list[ToolCall] = field(default_factory=list)
    runs_sampled: int = 1
    union_ids_added: list[str] = field(default_factory=list)


class QueryMechanismV2:
    def __init__(
        self,
        host="localhost",
        port=6379,
        graph_name="policy_system",
        llm: Optional[LLMClient] = None,
        union_runs: int = UNION_RUNS_DEFAULT,
    ):
        self.v1 = QueryMechanismV1(host=host, port=port, graph_name=graph_name)
        self.tools = ToolBox(self.v1.graph)
        self.llm = llm or NoLLMConfigured()
        self.union_runs = union_runs
        self._dispatch = {
            "list_entities": lambda args: self.tools.list_entities(args["label"]),
            "run_cypher": lambda args: self.tools.run_cypher(args["query"]),
            "whole_graph_stats": lambda args: self.tools.whole_graph_stats(),
        }

    def ask(self, question: str) -> MechanismResult:
        try:
            v1_result = self.v1.ask(question)
            summary = "; ".join(f"{v1_result.columns}: {row}" for row in v1_result.rows[:20])
            return MechanismResult(
                question=question,
                mechanism="v1-template",
                answer=summary or "(no rows)",
                template=v1_result.template,
            )
        except NoTemplateMatch:
            pass

        return self._ask_agent_union(question)

    def _ask_agent_union(self, question: str) -> MechanismResult:
        """Sample `self.union_runs` independent agent runs and mechanically
        combine them -- per q-approach2.md's "Further experiments" finding
        that regex-extracted-id union beats every tested form of LLM-driven
        combination (an extra synthesis call lost information a plain union
        kept; a validator pass didn't reliably catch what synthesis lost).
        No extra LLM call here either: the run whose own answer already
        covers the most of the pooled id set is kept verbatim as the answer,
        and only the ids other runs found that it didn't are appended as a
        flagged, unverified addendum -- structural combination, not prose
        re-synthesis.

        `AgentTurnLimitExceeded` on an individual run is treated as "this
        sample contributed nothing," not a hard failure of the whole
        question -- self-consistency's real run data (a 0/7, a 7/7, and a
        non-convergent third) showed one non-converging sample among several
        working ones is the normal case here, not a rare edge case. Only if
        every sampled run fails to converge does that propagate.
        """
        attempts: list[MechanismResult] = []
        for _ in range(self.union_runs):
            try:
                attempts.append(self._ask_agent(question))
            except AgentTurnLimitExceeded:
                continue

        if not attempts:
            raise AgentTurnLimitExceeded(
                f"none of {self.union_runs} sampled runs converged for: {question!r}"
            )

        cited_per_attempt = [extract_entity_ids(a.answer) for a in attempts]
        union_ids: set[str] = set().union(*cited_per_attempt)

        best_idx = max(range(len(attempts)), key=lambda i: len(cited_per_attempt[i]))
        best = attempts[best_idx]
        missing = sorted(union_ids - cited_per_attempt[best_idx])

        answer = best.answer
        if missing:
            answer += (
                "\n\n[Found in other sampled runs but not cited above -- not independently "
                "re-verified, check before relying on these: " + ", ".join(missing) + "]"
            )

        all_calls = [c for a in attempts for c in a.tool_calls_made]
        return MechanismResult(
            question=question,
            mechanism="v2-agent",
            answer=answer,
            tool_calls_made=all_calls,
            runs_sampled=len(attempts),
            union_ids_added=missing,
        )

    def _ask_agent(self, question: str) -> MechanismResult:
        messages: list[dict] = [{"role": "user", "content": question}]
        all_calls: list[ToolCall] = []

        for _ in range(MAX_AGENT_TURNS):
            turn = self.llm.complete(system=SYSTEM_PROMPT, messages=messages, tools=TOOL_SPECS)

            if not turn.tool_calls:
                return MechanismResult(
                    question=question,
                    mechanism="v2-agent",
                    answer=turn.text or "",
                    tool_calls_made=all_calls,
                )

            messages.append({"role": "assistant", "tool_calls": turn.tool_calls})
            for call in turn.tool_calls:
                all_calls.append(call)
                try:
                    result = self._dispatch[call.name](call.arguments)
                except Exception as exc:  # noqa: BLE001 -- surfaced to the model, not swallowed
                    result = {"error": str(exc)}
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

        raise AgentTurnLimitExceeded(f"no final answer from the model after {MAX_AGENT_TURNS} turns: {question!r}")


if __name__ == "__main__":
    mech = QueryMechanismV2()
    question = sys.argv[1] if len(sys.argv) > 1 else "Give me a one-paragraph summary of our overall compliance posture."
    try:
        result = mech.ask(question)
        print(f"mechanism: {result.mechanism}" + (f" (template={result.template})" if result.template else ""))
        print(result.answer)
    except RuntimeError as e:
        print(e)
