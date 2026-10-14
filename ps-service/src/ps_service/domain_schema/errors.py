"""Domain-specific exception types for `ps_service.domain_schema`.

One exception type per distinct failure boundary this component owns.
"""

from __future__ import annotations


class DomainSchemaError(Exception):
    """The domain schema (or an artifact rendered from it) is structurally invalid.

    Raised for an unknown label or property, a non-enum property used as an enum, or malformed
    generated-region markers in a document. The caller should fix the schema definition or the
    document; retrying does not help.
    """


class SchemaProfileError(Exception):
    """A profile cannot be applied to the schema (unknown target, illegal narrowing, bad order).

    Raised by `apply_profile`. The caller should correct the profile; the schema is unchanged.
    """


class MissingArtifactError(Exception):
    """A generated artifact the freshness check compares is absent from the repository.

    Attributes:
        artifact: The repository-relative path of the missing artifact.

    The caller should create the artifact (for example with the `write-docs` command) and rerun.
    """

    def __init__(self, artifact: str) -> None:
        """Remember the missing artifact.

        Args:
            artifact: Repository-relative path of the artifact that is absent.
        """
        super().__init__(f"missing artifact {artifact}")
        self.artifact = artifact
