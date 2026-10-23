"""Exceptions raised while building the metrics component."""

from __future__ import annotations


class MetricsConfigurationError(Exception):
    """The metric catalog and the observers handed to the builder do not agree."""


class SourceDeadlineExceededError(Exception):
    """A metric source did not answer within its deadline and was treated as failed."""
