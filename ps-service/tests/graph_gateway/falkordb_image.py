"""The FalkorDB image the chart ships, read from the chart's `values.yaml`.

`charts/policy-system/values.yaml` is the single source of truth for the image tag, so tests
that start their own FalkorDB use the same image the chart deploys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_CHART_VALUES = Path(__file__).resolve().parents[3] / "charts" / "policy-system" / "values.yaml"


def _image_settings(values_path: Path) -> dict[str, Any]:
    """Return the `falkordb.image` mapping, or an empty one when it is absent."""
    node: object = yaml.safe_load(values_path.read_text())
    for key in ("falkordb", "image"):
        if not isinstance(node, dict):
            return {}
        node = cast("dict[str, object]", node).get(key)
    return cast("dict[str, Any]", node) if isinstance(node, dict) else {}


def falkordb_image_tag(values_path: Path = _CHART_VALUES) -> str:
    """Return the pinned FalkorDB image tag.

    Args:
        values_path: Helm values file to read; defaults to the chart's `values.yaml`.

    Returns:
        The value of `falkordb.image.tag`.

    Raises:
        ValueError: If the tag is missing, empty, or `latest`.
    """
    tag = _image_settings(values_path).get("tag")
    if not isinstance(tag, str) or not tag:
        raise ValueError(f"falkordb.image.tag is missing or empty in {values_path}")
    if tag == "latest":
        raise ValueError(f"falkordb.image.tag must be a pinned version, not latest ({values_path})")
    return tag


def falkordb_image_ref(values_path: Path = _CHART_VALUES) -> str:
    """Return the pinned FalkorDB image reference as `repository:tag`.

    Args:
        values_path: Helm values file to read; defaults to the chart's `values.yaml`.

    Returns:
        The image reference the chart deploys.

    Raises:
        ValueError: If the repository is missing or the tag is invalid.
    """
    repository = _image_settings(values_path).get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError(f"falkordb.image.repository is missing or empty in {values_path}")
    return f"{repository}:{falkordb_image_tag(values_path)}"
