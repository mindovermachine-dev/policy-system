"""Per-skill token-cost measurement for the `ps-plugin` product skills (issue #200, AC-BI-001).

For every skill under `ps-skills/ps-plugin/skills` this reports:

- the `SKILL.md` size (characters, plus an approximate token count of characters / 4);
- the size of each file `SKILL.md` instructs the model to read (any `*.md` path it mentions that
  resolves inside the skill directory or the plugin's `rubrics/` directory), and any it names that
  does not resolve inside the plugin;
- the tools `SKILL.md`'s `## On Load` section tells the model to call or fetch (a sentence naming
  ``the `x` tool`` together with call/fetch; "does not expose `x` tool" checks are not counted).

The report is Markdown, sorted by `SKILL.md` size (largest first, name as tie-break), and depends
only on file contents -- so a rerun over unchanged files is byte-identical. It also marks the
smallest set of largest skills whose `SKILL.md` sizes sum to at least 60% of the total.

Usage: `uv run python scripts/measure_skills.py [--skills-root DIR] [--output FILE]`.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SKILLS_ROOT = Path("ps-skills/ps-plugin/skills")
COVERAGE_TARGET = 0.60
_CHARS_PER_TOKEN = 4
_MD_REFERENCE = re.compile(r"(?<![\w./-])((?:[\w-]+/)*[\w-]+\.md)\b")
_TOOL_MENTION = re.compile(r"`([\w-]+)`\s+(?:MCP\s+)?tool\b")
_FETCH_VERB = re.compile(r"\b(?:call|calls|calling|fetch|refetch)\b", re.IGNORECASE)


@dataclass(frozen=True)
class SkillMeasurement:
    """One skill's measured sizes, named files and on-load tool calls."""

    name: str
    skill_chars: int
    references: dict[str, int] = field(default_factory=dict)
    unresolved_references: tuple[str, ...] = ()
    on_load_tool_calls: tuple[str, ...] = ()

    @property
    def reference_chars(self) -> int:
        """Total size of the resolved files `SKILL.md` names."""
        return sum(self.references.values())


def _tokens(chars: int) -> int:
    return round(chars / _CHARS_PER_TOKEN)


def _on_load_section(text: str) -> str:
    lines: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## On Load"
            continue
        if in_section:
            lines.append(line)
    return " ".join(" ".join(lines).split())


def _on_load_tool_calls(text: str) -> tuple[str, ...]:
    """Tools named in an `## On Load` sentence that says to call or fetch them."""
    tools: list[str] = []
    for sentence in re.split(r"(?<=[.:;])\s+", _on_load_section(text)):
        if _FETCH_VERB.search(sentence):
            tools += _TOOL_MENTION.findall(sentence)
    return tuple(dict.fromkeys(tools))


def _resolve(reference: str, skill_dir: Path, plugin_dir: Path) -> Path | None:
    candidates = (
        skill_dir / reference,
        plugin_dir / reference,
        plugin_dir / "rubrics" / Path(reference).name,
    )
    return next((c for c in candidates if c.is_file()), None)


def measure_skill(skill_dir: Path, plugin_dir: Path) -> SkillMeasurement:
    """Measure one skill directory."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    references: dict[str, int] = {}
    unresolved: set[str] = set()
    for match in _MD_REFERENCE.finditer(text):
        reference = match.group(1)
        if reference == "SKILL.md" or reference.endswith("/SKILL.md"):
            continue
        resolved = _resolve(reference, skill_dir, plugin_dir)
        if resolved is None:
            unresolved.add(reference)
        else:
            references[resolved.relative_to(plugin_dir).as_posix()] = len(
                resolved.read_text(encoding="utf-8")
            )
    return SkillMeasurement(
        name=skill_dir.name,
        skill_chars=len(text),
        references=dict(sorted(references.items())),
        unresolved_references=tuple(sorted(unresolved)),
        on_load_tool_calls=_on_load_tool_calls(text),
    )


def measure_all(skills_root: Path) -> list[SkillMeasurement]:
    """Measure every skill under `skills_root`, largest `SKILL.md` first."""
    plugin_dir = skills_root.parent
    measurements = [
        measure_skill(d, plugin_dir)
        for d in sorted(skills_root.iterdir())
        if (d / "SKILL.md").is_file()
    ]
    return sorted(measurements, key=lambda m: (-m.skill_chars, m.name))


def coverage_set(
    measurements: list[SkillMeasurement], target: float = COVERAGE_TARGET
) -> list[str]:
    """Smallest set of largest skills whose sizes sum to at least `target` of the total."""
    total = sum(m.skill_chars for m in measurements)
    chosen: list[str] = []
    running = 0
    for m in measurements:
        if running >= target * total:
            break
        chosen.append(m.name)
        running += m.skill_chars
    return chosen


def render_report(measurements: list[SkillMeasurement]) -> str:
    """Render the deterministic Markdown report."""
    total = sum(m.skill_chars for m in measurements)
    in_set = coverage_set(measurements)
    set_chars = sum(m.skill_chars for m in measurements if m.name in in_set)
    names = ", ".join(f"`{n}`" for n in in_set)
    out = [
        "# ps-plugin skill token-cost measurement",
        "",
        (
            f"Skills: {len(measurements)}. Total `SKILL.md`: {total} chars "
            f"(~{_tokens(total)} tokens, chars / {_CHARS_PER_TOKEN})."
        ),
        "",
        (
            f"Target set (smallest set of largest skills reaching {COVERAGE_TARGET:.0%} "
            f"of the total): {names} = {set_chars} chars ({set_chars / total:.1%})."
        ),
        "",
        (
            "| Skill | SKILL.md chars | ~tokens | % of total | Referenced chars "
            "| In target set | On-load tool calls |"
        ),
        "| --- | ---: | ---: | ---: | ---: | :---: | --- |",
    ]
    for m in measurements:
        tools = ", ".join(f"`{t}`" for t in m.on_load_tool_calls) or "none"
        flag = "yes" if m.name in in_set else "no"
        out.append(
            f"| `{m.name}` | {m.skill_chars} | {_tokens(m.skill_chars)} "
            f"| {m.skill_chars / total:.1%} | {m.reference_chars} | {flag} | {tools} |"
        )
    out += ["", "## Files named by SKILL.md", ""]
    any_refs = False
    for m in measurements:
        if not m.references and not m.unresolved_references:
            continue
        any_refs = True
        out.append(f"### `{m.name}`")
        out.append("")
        out += [f"- `{path}`: {chars} chars" for path, chars in m.references.items()]
        out += [f"- `{r}`: not in the plugin (unresolved)" for r in m.unresolved_references]
        out.append("")
    if not any_refs:
        out += ["None.", ""]
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the measurement and print or write the report."""
    parser = argparse.ArgumentParser(
        description="Measure per-skill token cost of ps-plugin skills."
    )
    parser.add_argument("--skills-root", type=Path, default=DEFAULT_SKILLS_ROOT)
    parser.add_argument("--output", type=Path, help="write the report here instead of stdout")
    args = parser.parse_args(argv)
    report = render_report(measure_all(args.skills_root))
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
