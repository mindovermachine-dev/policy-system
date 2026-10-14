"""Per-skill token-cost measurement for the `ps-plugin` product skills.

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
    """One skill's measured sizes, named files and on-load tool calls.

    Attributes:
        name: Skill directory name.
        skill_chars: Character count of `SKILL.md`.
        references: Plugin-relative path of each resolved named file mapped to its character count.
        unresolved_references: Sorted names of `*.md` files `SKILL.md` mentions that do not
            resolve inside the plugin.
        on_load_tool_calls: Tools the `## On Load` section tells the model to call or fetch.
    """

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
    """Measure one skill directory.

    Args:
        skill_dir: Directory holding the skill's `SKILL.md`; the directory name is the skill name.
        plugin_dir: Plugin root against which referenced files are resolved (also searched in its
            `rubrics/` directory by file name).

    Returns:
        The skill's size, the sizes of the resolved files it names, the names it mentions that do
        not resolve, and the tools its `## On Load` section tells the model to call or fetch.

    Raises:
        OSError: `SKILL.md` or a resolved referenced file cannot be read.
    """
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
    """Measure every skill directory that holds a `SKILL.md`.

    Args:
        skills_root: Directory whose immediate subdirectories are skills; its parent is the
            plugin root.

    Returns:
        One measurement per skill, largest `SKILL.md` first with the skill name as tie-break.

    Raises:
        OSError: `skills_root` or a skill file cannot be read.
    """
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
    """Pick the smallest set of largest skills that carries most of the total size.

    Args:
        measurements: Measurements already ordered largest first (as `measure_all` returns).
        target: Fraction of the total `SKILL.md` size the set must reach, between 0 and 1.

    Returns:
        Skill names, largest first, taken until their sizes sum to at least `target` of the total;
        empty when `measurements` is empty.
    """
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
    """Render the Markdown report for a set of measurements.

    Args:
        measurements: Non-empty list of skill measurements, in the order to print them.

    Returns:
        A Markdown document with a summary, a per-skill table, and the files each `SKILL.md`
        names. The text depends only on `measurements`, so identical input renders identical text.

    Raises:
        ZeroDivisionError: `measurements` is empty (the total size is zero).
    """
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
    """Measure all skills and print or write the report.

    Args:
        argv: Command-line arguments without the program name; `None` reads `sys.argv`.
            `--skills-root` selects the skills directory and `--output` a file to write instead of
            standard output.

    Returns:
        0 after the report is emitted.

    Raises:
        OSError: The skills tree cannot be read or the output file cannot be written.
        ZeroDivisionError: The skills root holds no skill.

    Side Effects:
        Writes the report to `--output`, or to standard output when it is not given.
    """
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
