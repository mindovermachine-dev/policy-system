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

import re
import sys
from dataclasses import dataclass, field
from typing import Optional, Protocol

from query_mechanism_v1 import FIXTURE_ANCHOR_DATE, NoTemplateMatch, QueryMechanismV1

MAX_AGENT_TURNS = 8

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
        result = self.graph.query(query)
        columns = [c[1] for c in result.header] if result.header else []
        rows = [list(r) for r in result.result_set]
        return {"columns": columns, "rows": rows}

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


class QueryMechanismV2:
    def __init__(self, host="localhost", port=6379, graph_name="policy_system", llm: Optional[LLMClient] = None):
        self.v1 = QueryMechanismV1(host=host, port=port, graph_name=graph_name)
        self.tools = ToolBox(self.v1.graph)
        self.llm = llm or NoLLMConfigured()
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

        return self._ask_agent(question)

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
