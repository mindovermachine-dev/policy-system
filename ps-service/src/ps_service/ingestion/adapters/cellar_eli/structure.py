"""Cellar/ELI XHTML -> native structural graph.

Produces `StructuralNode`/`StructuralEdge` (from
`ps_service.ingestion.models`). Ports `spikes/cellar1/parse_structure.py`'s
tree-walking logic (`_walk_body`/`_walk_article`/`_mint_paragraph`/
`parse_structure`), retyped to the project's real types instead of the
spike's untyped `Node`/`Edge`.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from defusedxml.ElementTree import fromstring as parse_xml

from ps_service.ingestion.adapters.errors import CellarParseError
from ps_service.ingestion.models import StructuralEdge, StructuralNode

if TYPE_CHECKING:
    import xml.etree.ElementTree as ET

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
_LABEL_ONLY_CLASSES = {
    "oj-ti-art",
    "oj-ti-section-1",
    "oj-doc-ti",
    "title-article-norm",
    "stitle-article-norm",
}

_PARAGRAPH_NORM_CLASS = "norm"
"""Consolidated convention: every article paragraph is wrapped in `<div class="norm">`."""

_NO_PARAG_CLASS = "no-parag"
"""Consolidated convention: a paragraph's number sits in a `<span class="no-parag">`."""

_PARAGRAPH_NUMBER_LEADING_STRIP = "\u2018\u2019'\u25ba\u25bc "
"""Leading noise a `no-parag` marker can carry: U+2018/U+2019 typographic quotes,
an ASCII apostrophe, a U+25BA/U+25BC amendment change-marker, and whitespace."""


def _normalise_paragraph_number(raw: str) -> str:
    """A clean paragraph number from a raw id group or `no-parag` marker text.

    Strips surrounding whitespace, a leading typographic quote / change-marker,
    and the trailing `.`; an all-digit result has its leading zeros dropped
    (`"001"` -> `"1"`). An alphanumeric result (`"1a"`, an amendment-inserted
    paragraph) is kept verbatim — never `int()`-parsed.
    """
    cleaned = raw.strip().lstrip(_PARAGRAPH_NUMBER_LEADING_STRIP).rstrip(". ").strip()
    return str(int(cleaned)) if cleaned.isdigit() else cleaned


def _paragraph_number(child: ET.Element, child_id: str) -> str | None:
    """The paragraph number for a direct child of an ARTICLE div, or `None`.

    Two markup conventions, one predicate:
    * base-act: `child_id` matches `NNN.NNN` -> the trailing group, normalised.
    * consolidated: the child's own class list contains `norm` AND it has a
      descendant `<span class="no-parag">` -> that span's text, normalised.
    Anything else (an unnumbered `norm` chapeau, a heading, a points list) -> `None`.
    """
    base_match = _PARAGRAPH_ID_RE.match(child_id)
    if base_match is not None:
        return _normalise_paragraph_number(base_match.group(2))
    if _PARAGRAPH_NORM_CLASS not in _own_class(child).split():
        return None
    for span in child.iter("span"):
        if _own_class(span) == _NO_PARAG_CLASS:
            marker = "".join(span.itertext()).strip()
            if marker:
                return _normalise_paragraph_number(marker)
    return None


def _is_label_only(element: ET.Element) -> bool:
    """Whether `element`'s first CSS class marks it as a bare label (Article N).

    Matches on the first class only, so a single-class `<p class="title-article-norm">`
    is suppressed while a compound `norm inline-element` paragraph body never is.
    """
    classes = _own_class(element).split()
    return bool(classes) and classes[0] in _LABEL_ONLY_CLASSES


_ELEMENT_LABEL = {"cpt": CHAPTER, "sct": SECTION, "anx": ANNEX, "rct": RECITAL}
_CITATION_WORD = {
    CHAPTER: "Chapter",
    SECTION: "Section",
    ARTICLE: "Art.",
    ANNEX: "Annex",
    RECITAL: "Recital",
}


@dataclass(frozen=True)
class _StructuralSink:
    """Regulation-scoped accumulator threaded through every tree-walk helper.

    `frozen` protects the three references from rebinding; `nodes`/`edges`
    are still appended to in place.
    """

    regulatory_instrument_id: str
    nodes: list[StructuralNode]
    edges: list[StructuralEdge]
    annexes: list[tuple[ET.Element, str]]
    """`(element, number)` for every `anx_*` div found nested in the main container
    (the consolidated convention). Merged with the base-act separate-container
    annexes in `parse_structure` and minted there, deduped by number."""


def _strip_namespace(root: ET.Element) -> None:
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]


def _own_class(element: ET.Element) -> str:
    return element.get("class", "")


def _full_text(element: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _heading_text(element: ET.Element) -> str:
    """Flattened text of `element`'s direct child heading div, or `""` if none.

    The heading div is the direct child that is `eli-title`-classed or has
    a `.tit_1`-suffixed id.
    """
    for child in element:
        if _own_class(child) == _TITLE_CLASS or child.get("id", "").endswith(".tit_1"):
            return _full_text(child)
    return ""


class _OrderCounter:
    """One counter per (parent_label, parent_id) scope.

    Yields 'position among siblings' per the container architecture doc's
    attribute table.
    """

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
    para_number: str,
    order: int,
    sink: _StructuralSink,
) -> None:
    """Mint one PARAGRAPH node + its ARTICLE->PARAGRAPH edge.

    `para_number` is already normalised (`_paragraph_number`). When the div
    carries no `id` (the consolidated convention), a stable article-scoped
    id is synthesised (`{article_local_id}.p{para_number}`) so re-ingest
    stays idempotent and sibling paragraphs never collide.
    """
    citation_ref = f"Art. {article_number}({para_number})"
    paragraph_id = div.get("id") or f"{article_local_id}.p{para_number}"
    node_id = f"{sink.regulatory_instrument_id}#{paragraph_id}"
    sink.nodes.append(
        StructuralNode(
            PARAGRAPH,
            node_id,
            {"text": _full_text(div), "citation_ref": citation_ref, "order": order},
        )
    )
    sink.edges.append(
        StructuralEdge(
            ARTICLE, f"{sink.regulatory_instrument_id}#{article_local_id}", PARAGRAPH, node_id
        )
    )


def _walk_article(
    div: ET.Element,
    local_id: str,
    number: str,
    parent_label: str,
    parent_id: str,
    order: int,
    sink: _StructuralSink,
) -> None:
    node_id = f"{sink.regulatory_instrument_id}#{local_id}"
    heading = ""
    own_text_parts: list[str] = []
    paragraph_children: list[tuple[ET.Element, str]] = []
    para_order = _OrderCounter()

    for child in div:
        child_id = child.get("id") or ""
        if _own_class(child) == _TITLE_CLASS or child_id.endswith(".tit_1"):
            heading = _full_text(child)
        elif _is_label_only(child):
            continue
        else:
            para_number = _paragraph_number(child, child_id)
            if para_number is not None:
                paragraph_children.append((child, para_number))
            elif text := _full_text(child):
                own_text_parts.append(text)

    sink.nodes.append(
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
    sink.edges.append(StructuralEdge(parent_label, parent_id, ARTICLE, node_id))

    for paragraph_child, paragraph_number in paragraph_children:
        _mint_paragraph(
            paragraph_child,
            local_id,
            number,
            paragraph_number,
            para_order.next(ARTICLE, node_id),
            sink,
        )


def _mint_struct_node(
    child: ET.Element,
    kind: str,
    number: str,
    local_id: str,
    parent_label: str,
    parent_id: str,
    order: int,
    sink: _StructuralSink,
) -> None:
    """Mint one CHAPTER/SECTION/RECITAL node and recurse for CHAPTER/SECTION.

    ARTICLE is minted by `_walk_article` instead. For CHAPTER/SECTION,
    recurses into the element's children with itself as the new parent.
    Split out of `_walk_body`'s id-dispatch loop — S1 fix, see module
    docstring.
    """
    label = _ELEMENT_LABEL[kind]
    node_id = f"{sink.regulatory_instrument_id}#{local_id}"
    properties: dict[str, str | int] = {
        "citation_ref": f"{_CITATION_WORD[label]} {number}",
        "order": order,
    }
    if label == RECITAL:
        properties["text"] = _full_text(child)
    else:
        properties["heading"] = _heading_text(child)

    sink.nodes.append(StructuralNode(label, node_id, properties))
    sink.edges.append(StructuralEdge(parent_label, parent_id, label, node_id))

    if label in (CHAPTER, SECTION):
        _walk_body(child, label, node_id, _OrderCounter(), sink)


def _walk_body(
    elem: ET.Element,
    parent_label: str,
    parent_id: str,
    order_counter: _OrderCounter,
    sink: _StructuralSink,
) -> None:
    """Recurse through the enacting-terms tree, minting a node per structural div.

    Divs whose id names a structural element (CHAPTER/SECTION/ARTICLE/
    RECITAL, per `_STRUCT_ID_RE`) mint a node; everything else — including
    a TITLE-shaped div, which never matches `_STRUCT_ID_RE` — is a
    transparent pass-through, walked but never minted. This is what lets
    the same call handle both a Chapter/Article-only document and one with
    an extra Section (or TITLE) layer in between.
    """
    for child in elem:
        if child.tag != "div":
            continue
        local_id = child.get("id", "")
        match = _STRUCT_ID_RE.match(local_id)
        if not match:
            _walk_body(child, parent_label, parent_id, order_counter, sink)
            continue

        kind = match.group(1)
        if kind == "art":
            order = order_counter.next(parent_label, parent_id)
            _walk_article(child, local_id, match.group(2), parent_label, parent_id, order, sink)
            continue
        if kind == "anx":
            # consolidated convention: anx_* is a div nested in the main container,
            # not a separate eli-container. Collect it (don't recurse); parse_structure
            # merges it with the base-act separate-container annexes and mints both.
            sink.annexes.append((child, match.group(2)))
            continue

        order = order_counter.next(parent_label, parent_id)
        _mint_struct_node(
            child, kind, match.group(2), local_id, parent_label, parent_id, order, sink
        )


def _split_containers(
    containers: list[ET.Element],
) -> tuple[ET.Element, list[tuple[ET.Element, str]]]:
    """Split the main enacting-terms container from the per-Annex containers.

    Cellar/ELI puts each Annex in its own top-level `eli-container`,
    separate from the document's main enacting-terms container — split them
    apart before walking either.
    """
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


def _mint_annexes(
    annexes: list[tuple[ET.Element, str]],
    regulatory_instrument_id: str,
    sink: _StructuralSink,
) -> None:
    """Mint one ANNEX node + its RegulatoryInstrument->ANNEX edge per distinct annex number.

    Fed from both markup conventions — a base-act separate `eli-container`
    and a consolidated `anx_*` div nested in the main container — and deduped
    by annex number so a document carrying both forms mints each annex once.
    Node/edge shape is identical for both conventions.
    """
    order = _OrderCounter()
    seen_numbers: set[str] = set()
    for annex_element, number in annexes:
        if number in seen_numbers:
            continue
        seen_numbers.add(number)
        local_id = annex_element.get("id") or ""
        node_id = f"{regulatory_instrument_id}#{local_id}"
        sink.nodes.append(
            StructuralNode(
                ANNEX,
                node_id,
                {
                    "text": _full_text(annex_element),
                    "citation_ref": f"Annex {number}",
                    "order": order.next(REGULATORY_INSTRUMENT, regulatory_instrument_id),
                },
            )
        )
        sink.edges.append(
            StructuralEdge(REGULATORY_INSTRUMENT, regulatory_instrument_id, ANNEX, node_id)
        )


def parse_structure(
    xhtml: bytes, regulatory_instrument_id: str
) -> tuple[tuple[StructuralNode, ...], tuple[StructuralEdge, ...]]:
    """Native structural graph for one regulation.

    CHAPTER/SECTION/ARTICLE/PARAGRAPH/RECITAL nested under
    `regulatory_instrument_id`, plus ANNEX as top-level children. An annex is
    either its own top-level `eli-container` (base-act convention) or an
    `anx_*` div nested in the main container (consolidated convention); both
    forms are collected and minted the same way, deduped by annex number.
    `regulatory_instrument_id` is an opaque prefix for structural node ids
    (`f"{regulatory_instrument_id}#{local_id}"`) — it is not required to
    already be the final `{SHORT}-{VERSION}` RegulatoryInstrument id.
    """
    root = parse_xml(xhtml)
    _strip_namespace(root)
    sink = _StructuralSink(regulatory_instrument_id, [], [], [])

    containers = [element for element in root.iter("div") if _own_class(element) == "eli-container"]
    main, separate_annexes = _split_containers(containers)

    _walk_body(main, REGULATORY_INSTRUMENT, regulatory_instrument_id, _OrderCounter(), sink)
    _mint_annexes([*separate_annexes, *sink.annexes], regulatory_instrument_id, sink)

    return tuple(sink.nodes), tuple(sink.edges)
