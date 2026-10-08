"""Frozen dataclasses describing the PS compliance-graph schema.

The schema is a code artifact: node labels, their properties (name, type,
presence) and, in later slices, edges. Everything is immutable
(`frozen=True`, tuples) so a constructed `Schema` can be shared freely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from ps_service.domain_schema.errors import DomainSchemaError

NOTE_MAX_CHARS = 200
_LABEL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_EDGE_TYPE_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
_MULTIPLICITY_PATTERN = re.compile(r"^(\d+)(?:\.\.(\d+|\*))?$")


class Presence(Enum):
    """Whether a property must be present on a node.

    Named `Presence` (not `Requirement`) because `Requirement` is a domain
    node label.
    """

    REQUIRED = "required"
    OPTIONAL = "optional"
    CONDITIONAL = "conditional"


@dataclass(frozen=True, slots=True)
class StringType:
    """A string property; `min_length` raised above 0 forbids the empty string."""

    min_length: int = 0

    def __post_init__(self) -> None:
        """Reject a negative minimum length."""
        if self.min_length < 0:
            raise DomainSchemaError(f"string min_length must be >= 0, got {self.min_length}")


@dataclass(frozen=True, slots=True)
class DateType:
    """An ISO 8601 calendar date property."""


@dataclass(frozen=True, slots=True)
class EnumType:
    """A string property restricted to a non-empty, duplicate-free set of values."""

    values: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject an empty enum and duplicate values."""
        if not self.values:
            raise DomainSchemaError("enum values must not be empty")
        for value in self.values:
            if self.values.count(value) > 1:
                raise DomainSchemaError(f"enum has duplicate value {value!r}")


@dataclass(frozen=True, slots=True)
class FloatRangeType:
    """A float property bounded to the closed interval [`minimum`, `maximum`]."""

    minimum: float
    maximum: float

    def __post_init__(self) -> None:
        """Reject an inverted range."""
        if self.minimum > self.maximum:
            raise DomainSchemaError(
                f"float range minimum {self.minimum} exceeds maximum {self.maximum}"
            )


@dataclass(frozen=True, slots=True)
class ConstType:
    """A string property fixed to one value; produced only by profiles that narrow an enum."""

    value: str


type PropertyType = StringType | DateType | FloatRangeType | EnumType | ConstType


def _check_note(owner: str, field: str, text: str) -> None:
    """Raise unless `text` is a single line of at most `NOTE_MAX_CHARS` characters."""
    if "\n" in text or "\r" in text:
        raise DomainSchemaError(f"{field} of {owner} must be a single line")
    if len(text) > NOTE_MAX_CHARS:
        raise DomainSchemaError(
            f"{field} of {owner} is {len(text)} characters, limit is {NOTE_MAX_CHARS}"
        )


def _check_label(label: str) -> None:
    if not _LABEL_PATTERN.fullmatch(label):
        raise DomainSchemaError(f"malformed label {label!r}")


def _check_unique(kind: str, owner: str, names: tuple[str, ...]) -> None:
    for name in names:
        if names.count(name) > 1:
            raise DomainSchemaError(f"duplicate {kind} {name!r} in {owner}")


@dataclass(frozen=True, slots=True)
class Property:
    """One named property of a node, with its type, presence and write-ownership flags.

    `is_identity_bearing` marks values that name or own the node (title, owner fields); the
    system sets them, content tools never patch them. `is_lifecycle_managed` marks values the
    governance lifecycle sets (status, version). A property with neither flag is patchable.
    """

    name: str
    type: PropertyType
    presence: Presence
    note: str = ""
    is_identity_bearing: bool = field(default=False, kw_only=True)
    is_lifecycle_managed: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        """Validate the note."""
        _check_note(f"property {self.name!r}", "note", self.note)


@dataclass(frozen=True, slots=True)
class Node:
    """A node label together with its properties, in declaration order."""

    label: str
    properties: tuple[Property, ...]
    note: str = ""

    def __post_init__(self) -> None:
        """Validate the label, property uniqueness and the note."""
        _check_label(self.label)
        _check_unique("property", f"node {self.label}", tuple(p.name for p in self.properties))
        _check_note(f"node {self.label}", "note", self.note)


@dataclass(frozen=True, slots=True)
class Multiplicity:
    """How many nodes sit at one end of an edge: `minimum`..`maximum` (None is unbounded)."""

    minimum: int
    maximum: int | None

    def __post_init__(self) -> None:
        """Require 0 <= minimum <= maximum."""
        if self.minimum < 0 or (self.maximum is not None and self.maximum < self.minimum):
            raise DomainSchemaError(f"bad multiplicity {self.minimum}..{self.maximum}")

    @classmethod
    def parse(cls, text: str) -> Multiplicity:
        """Parse `"1"`, `"0..1"`, `"1..*"` or `"0..*"`."""
        matched = _MULTIPLICITY_PATTERN.match(text.strip())
        if matched is None:
            raise DomainSchemaError(f"malformed cardinality side {text!r}")
        low, high = matched.group(1), matched.group(2)
        if high is None:
            return cls(int(low), int(low))
        return cls(int(low), None if high == "*" else int(high))

    def __str__(self) -> str:
        """Render in the doc's notation: `1`, `0..1`, `1..*`."""
        if self.maximum == self.minimum:
            return str(self.minimum)
        return f"{self.minimum}..{'*' if self.maximum is None else self.maximum}"


@dataclass(frozen=True, slots=True)
class Cardinality:
    """Edge cardinality `"S : T"`: S source nodes per target node, T target nodes per source."""

    sources_per_target: Multiplicity
    targets_per_source: Multiplicity

    @classmethod
    def parse(cls, text: str) -> Cardinality:
        """Parse the doc's `"1 : 0..*"` notation."""
        sides = text.split(":")
        if len(sides) != 2:  # noqa: PLR2004  # exactly two sides around the colon
            raise DomainSchemaError(f"malformed cardinality {text!r}")
        return cls(Multiplicity.parse(sides[0]), Multiplicity.parse(sides[1]))

    def flipped(self) -> Cardinality:
        """Return the cardinality seen from the target node (inbound view)."""
        return Cardinality(self.targets_per_source, self.sources_per_target)

    def __str__(self) -> str:
        """Render in the doc's notation: `1 : 0..*`."""
        return f"{self.sources_per_target} : {self.targets_per_source}"


@dataclass(frozen=True, slots=True)
class EdgeProperty:
    """One named property carried by an edge."""

    name: str
    type: PropertyType
    presence: Presence


@dataclass(frozen=True, slots=True)
class Edge:
    """A typed, directed edge between two node labels."""

    type: str
    source: str
    target: str
    cardinality: Cardinality
    properties: tuple[EdgeProperty, ...] = ()
    note: str = ""
    provenance_rule: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity of the edge: type plus both ends."""
        return (self.type, self.source, self.target)

    def __post_init__(self) -> None:
        """Validate type shape, edge-property uniqueness and the free-text fields."""
        if not _EDGE_TYPE_PATTERN.fullmatch(self.type):
            raise DomainSchemaError(f"malformed edge type {self.type!r}")
        owner = f"edge {self.type}"
        _check_unique("edge property", owner, tuple(p.name for p in self.properties))
        _check_note(owner, "note", self.note)
        _check_note(owner, "provenance_rule", self.provenance_rule)


@dataclass(frozen=True, slots=True)
class Schema:
    """The whole graph schema: nodes and edges in declaration order."""

    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...] = ()

    def __post_init__(self) -> None:
        """Reject duplicate labels, duplicate edges and edges naming an undefined label."""
        labels = tuple(node.label for node in self.nodes)
        _check_unique("label", "schema", labels)
        keys = tuple("/".join(edge.key) for edge in self.edges)
        _check_unique("edge", "schema", keys)
        for edge in self.edges:
            for end in (edge.source, edge.target):
                if end not in labels:
                    raise DomainSchemaError(f"edge {edge.type} names undefined label {end!r}")
