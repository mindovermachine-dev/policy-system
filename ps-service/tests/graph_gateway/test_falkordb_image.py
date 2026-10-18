from pathlib import Path

import pytest
import yaml

from graph_gateway.falkordb_image import falkordb_image_ref, falkordb_image_tag

_CHART_VALUES = Path(__file__).resolve().parents[3] / "charts" / "policy-system" / "values.yaml"


def _write_values(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "values.yaml"
    path.write_text(body)
    return path


def test_tag_equals_chart_values_yaml_tag() -> None:
    chart = yaml.safe_load(_CHART_VALUES.read_text())

    assert falkordb_image_tag() == chart["falkordb"]["image"]["tag"]


def test_image_ref_is_repository_colon_tag() -> None:
    chart = yaml.safe_load(_CHART_VALUES.read_text())["falkordb"]["image"]

    assert falkordb_image_ref() == f"{chart['repository']}:{chart['tag']}"


def test_tag_is_read_from_the_given_values_file(tmp_path: Path) -> None:
    values = _write_values(tmp_path, "falkordb:\n  image:\n    repository: r/x\n    tag: v1.2.3\n")

    assert falkordb_image_tag(values) == "v1.2.3"
    assert falkordb_image_ref(values) == "r/x:v1.2.3"


def test_raises_when_tag_is_latest(tmp_path: Path) -> None:
    values = _write_values(tmp_path, "falkordb:\n  image:\n    repository: r/x\n    tag: latest\n")

    with pytest.raises(ValueError, match="latest"):
        falkordb_image_tag(values)


@pytest.mark.parametrize(
    "body",
    [
        "falkordb:\n  image:\n    repository: r/x\n",
        "falkordb:\n  image:\n    repository: r/x\n    tag: ''\n",
        "other: 1\n",
    ],
    ids=["tag-missing", "tag-empty", "falkordb-missing"],
)
def test_raises_when_tag_is_missing_or_empty(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match=r"falkordb\.image\.tag"):
        falkordb_image_tag(_write_values(tmp_path, body))


def test_raises_when_repository_is_missing(tmp_path: Path) -> None:
    values = _write_values(tmp_path, "falkordb:\n  image:\n    tag: v1.2.3\n")

    with pytest.raises(ValueError, match=r"falkordb\.image\.repository"):
        falkordb_image_ref(values)
