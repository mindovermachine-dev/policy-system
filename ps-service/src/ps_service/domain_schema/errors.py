"""Domain-specific exception types for `ps_service.domain_schema`.

One exception type per distinct failure boundary this component owns
(L2 "one exception type per distinct failure boundary").
"""

from __future__ import annotations


class DomainSchemaError(Exception):
    """The domain schema (or an artifact rendered from it) is structurally invalid."""


class SchemaProfileError(Exception):
    """A profile cannot be applied to the schema (unknown target, illegal narrowing, bad order)."""


class MissingArtifactError(Exception):
    """A generated artifact the freshness check compares is absent from the repository."""

    def __init__(self, artifact: str) -> None:
        """Remember the repository-relative path of the missing artifact."""
        super().__init__(f"missing artifact {artifact}")
        self.artifact = artifact
