"""Cellar/ELI XHTML -> native structural graph (`StructuralNode`/
`StructuralEdge`, from `ps_service.ingestion.models`). Ports `spikes/
cellar1/parse_structure.py`'s tree-walking logic (`_walk_body`/
`_walk_article`/`_mint_paragraph`/`parse_structure`), retyped to the
project's real types instead of the spike's untyped `Node`/`Edge`.

Real DOM tree, not PDF-style regex heading detection: Cellar's XHTML nests
chapters/articles/paragraphs as actual parent/child elements
(``eli-container`` > ``eli-subdivision`` > ...), tagged with stable ids
(``art_9``, ``cpt_I``, ``rct_12``, ``anx_I``) and CSS classes
(``eli-subdivision``, ``eli-title``) — verified directly against CRA/NIS2
(see `spikes/cellar1/LEARNINGS.md`). This parser just walks the tree; no
branch anywhere on which regulation a document is, only on what each
document's own markup contains (AC-006).

Mints ``CHAPTER``/``SECTION``/``ARTICLE``/``PARAGRAPH``/``ANNEX``/
``RECITAL``. ``TITLE``-shaped div ids (``ttl_*``) are NOT in the
id-dispatch table below (`_STRUCT_ID_RE`/`_ELEMENT_LABEL`) — a TITLE div is
therefore a transparent pass-through container: `_walk_body` recurses into
it, mints nothing for it, and attaches its children to whichever parent was
already current. This is a deliberate, tested gap (PLAN_REVIEWED.md §2.4/
§7 Increment 4, "S2"): AC-003 only requires every recital/article/annex,
and no real CRA/GDPR/NIS2 document exercises TITLE-level markup per
`spikes/cellar1/LEARNINGS.md`.

S1 fix (PLAN_REVIEWED.md §7): the spike's `_walk_body` is cyclomatic
complexity 10, failing this repo's `max-complexity = 8` gate. The "mint a
CHAPTER/SECTION/RECITAL node" branch is split out into `_mint_struct_node`
(ARTICLE is handled separately by `_walk_article`, unchanged) so
`_walk_body` itself stays a thin id-dispatch loop.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ps_service.ingestion.adapters.errors import CellarParseError
from ps_service.ingestion.models import StructuralEdge, StructuralNode

REGULATORY_INSTRUMENT = "RegulatoryInstrument"
CHAPTER = "CHAPTER"
SECTION = "SECTION"
ARTICLE = "ARTICLE"
PARAGRAPH = "PARAGRAPH"
ANNEX = "ANNEX"
RECITAL = "RECITAL"

_STRUCT_ID_RE = re.compile(r"^(cpt|sct|art|anx|rct)_([A-Za-z0-9]+)$")
_PARAGRAPH_ID_RE = re.compile(r"^(\d{3})\.(\d{3})$")
_TITLE_CLASS = "eli-title"
_LABEL_ONLY_CLASSES = {"oj-ti-art", "oj-ti-section-1", "oj-doc-ti"}

_ELEMENT_LABEL = {"cpt": CHAPTER, "sct": SECTION, "anx": ANNEX, "rct": RECITAL}
_CITATION_WORD = {CHAPTER: "Chapter", SECTION: "Section", ARTICLE: "Art.", ANNEX: "Annex", RECITAL: "Recital"}


def _strip_namespace(root: ET.Element) -> None:
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]


def _own_class(element: ET.Element) -> str:
    return element.get("class", "")


def _full_text(element: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _heading_text(element: ET.Element) -> str:
    """The flattened text of `element`'s `eli-title`-classed (or
    `.tit_1`-id-suffixed) direct child heading div, or `""` if it has none."""
    for child in element:
        if _own_class(child) == _TITLE_CLASS or child.get("id", "").endswith(".tit_1"):
            return _full_text(child)
    return ""


class _OrderCounter:
    """One counter per (parent_label, parent_id) scope — 'position among
    siblings' per the container architecture doc's attribute table."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}

    def next(self, parent_label: str, parent_id: str) -> int:
        key = (parent_label, parent_id)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]


def _mint_paragraph(
    div: ET.Element,
    article_local_id: str,
    article_number: str,
    paragraph_group: str,
    regulatory_instrument_id: str,
    order: int,
    nodes: list[StructuralNode],
    edges: list[StructuralEdge],
) -> None:
    para_number = str(int(paragraph_group))
    citation_ref = f"Art. {article_number}({para_number})"
    paragraph_id = div.get("id") or ""
    node_id = f"{regulatory_instrument_id}#{paragraph_id}"
    nodes.append(
        StructuralNode(
            PARAGRAPH,
            node_id,
            {"text": _full_text(div), "citation_ref": citation_ref, "order": order},
        )
    )
    edges.append(StructuralEdge(ARTICLE, f"{regulatory_instrument_id}#{article_local_id}", PARAGRAPH, node_id))


def _walk_article(
    div: ET.Element,
    local_id: str,
    number: str,
    regulatory_instrument_id: str,
    parent_label: str,
    parent_id: str,
    order: int,
    nodes: list[StructuralNode],
    edges: list[StructuralEdge],
) -> None:
    node_id = f"{regulatory_instrument_id}#{local_id}"
    heading = ""
    own_text_parts: list[str] = []
    paragraph_children: list[tuple[ET.Element, str]] = []
    para_order = _OrderCounter()

    for child in div:
        child_id = child.get("id") or ""
        cls = _own_class(child)
        if cls == _TITLE_CLASS or child_id.endswith(".tit_1"):
            heading = _full_text(child)
        elif cls in _LABEL_ONLY_CLASSES:
            continue
        else:
            paragraph_match = _PARAGRAPH_ID_RE.match(child_id)
            if paragraph_match is not None:
                paragraph_children.append((child, paragraph_match.group(2)))
            else:
                text = _full_text(child)
                if text:
                    own_text_parts.append(text)

    nodes.append(
        StructuralNode(
            ARTICLE,
            node_id,
            {
                "text": "" if paragraph_children else " ".join(own_text_parts),
                "citation_ref": f"Art. {number}",
                "heading": heading,
                "order": order,
            },
        )
    )
    edges.append(StructuralEdge(parent_label, parent_id, ARTICLE, node_id))

    for paragraph_child, paragraph_group in paragraph_children:
        _mint_paragraph(
            paragraph_child,
            local_id,
            number,
            paragraph_group,
            regulatory_instrument_id,
            para_order.next(ARTICLE, node_id),
            nodes,
            edges,
        )


def _mint_struct_node(
    child: ET.Element,
    kind: str,
    number: str,
    local_id: str,
    regulatory_instrument_id: str,
    parent_label: str,
    parent_id: str,
    order: int,
    nodes: list[StructuralNode],
    edges: list[StructuralEdge],
) -> None:
    """Mints one CHAPTER/SECTION/RECITAL node (ARTICLE is minted by
    `_walk_article` instead) and, for CHAPTER/SECTION, recurses into its
    children with itself as the new parent. Split out of `_walk_body`'s
    id-dispatch loop — S1 fix, see module docstring."""
    label = _ELEMENT_LABEL[kind]
    node_id = f"{regulatory_instrument_id}#{local_id}"
    properties: dict[str, str | int] = {"citation_ref": f"{_CITATION_WORD[label]} {number}", "order": order}
    if label == RECITAL:
        properties["text"] = _full_text(child)
    else:
        properties["heading"] = _heading_text(child)

    nodes.append(StructuralNode(label, node_id, properties))
    edges.append(StructuralEdge(parent_label, parent_id, label, node_id))

    if label in (CHAPTER, SECTION):
        _walk_body(child, regulatory_instrument_id, label, node_id, _OrderCounter(), nodes, edges)


def _walk_body(
    elem: ET.Element,
    regulatory_instrument_id: str,
    parent_label: str,
    parent_id: str,
    order_counter: _OrderCounter,
    nodes: list[StructuralNode],
    edges: list[StructuralEdge],
) -> None:
    """Recurse through the enacting-terms tree. Divs whose id names a
    structural element (CHAPTER/SECTION/ARTICLE/RECITAL, per
    `_STRUCT_ID_RE`) mint a node; everything else — including a
    TITLE-shaped div, which never matches `_STRUCT_ID_RE` — is a
    transparent pass-through, walked but never minted. This is what lets
    the same call handle both a Chapter/Article-only document and one with
    an extra Section (or TITLE) layer in between."""
    for child in elem:
        if child.tag != "div":
            continue
        local_id = child.get("id", "")
        match = _STRUCT_ID_RE.match(local_id)
        if not match:
            _walk_body(child, regulatory_instrument_id, parent_label, parent_id, order_counter, nodes, edges)
            continue

        kind = match.group(1)
        if kind == "art":
            order = order_counter.next(parent_label, parent_id)
            _walk_article(child, local_id, match.group(2), regulatory_instrument_id, parent_label, parent_id, order, nodes, edges)
            continue
        if kind == "anx":
            continue  # annexes are separate top-level eli-containers, handled by parse_structure

        order = order_counter.next(parent_label, parent_id)
        _mint_struct_node(child, kind, match.group(2), local_id, regulatory_instrument_id, parent_label, parent_id, order, nodes, edges)


def _split_containers(containers: list[ET.Element]) -> tuple[ET.Element, list[tuple[ET.Element, str]]]:
    """Cellar/ELI puts each Annex in its own top-level `eli-container`,
    separate from the document's main enacting-terms container — split
    them apart before walking either."""
    annexes: list[tuple[ET.Element, str]] = []
    main: ET.Element | None = None
    for container in containers:
        local_id = container.get("id") or ""
        match = _STRUCT_ID_RE.match(local_id)
        if match and match.group(1) == "anx":
            annexes.append((container, match.group(2)))
        elif main is None:
            main = container
    if main is None:
        raise CellarParseError("no main eli-container found in document")
    return main, annexes


def parse_structure(
    xhtml: bytes, regulatory_instrument_id: str
) -> tuple[tuple[StructuralNode, ...], tuple[StructuralEdge, ...]]:
    """Native structural graph for one regulation: CHAPTER/SECTION/ARTICLE/
    PARAGRAPH/RECITAL nested under `regulatory_instrument_id`, plus ANNEX as separate
    top-level children (each annex is its own top-level `eli-container` in
    Cellar's XHTML, not nested under the main one). `regulatory_instrument_id` is an
    opaque prefix for structural node ids (`f"{regulatory_instrument_id}#{local_id}"`)
    — it is not required to already be the final `{SHORT}-{VERSION}`
    RegulatoryInstrument id.
    """
    root = ET.fromstring(xhtml)
    _strip_namespace(root)
    nodes: list[StructuralNode] = []
    edges: list[StructuralEdge] = []

    containers = [element for element in root.iter("div") if _own_class(element) == "eli-container"]
    main, annexes = _split_containers(containers)

    _walk_body(main, regulatory_instrument_id, REGULATORY_INSTRUMENT, regulatory_instrument_id, _OrderCounter(), nodes, edges)

    annex_order = _OrderCounter()
    for annex_element, number in annexes:
        local_id = annex_element.get("id") or ""
        node_id = f"{regulatory_instrument_id}#{local_id}"
        nodes.append(
            StructuralNode(
                ANNEX,
                node_id,
                {
                    "text": _full_text(annex_element),
                    "citation_ref": f"Annex {number}",
                    "order": annex_order.next(REGULATORY_INSTRUMENT, regulatory_instrument_id),
                },
            )
        )
        edges.append(StructuralEdge(REGULATORY_INSTRUMENT, regulatory_instrument_id, ANNEX, node_id))

    return tuple(nodes), tuple(edges)
