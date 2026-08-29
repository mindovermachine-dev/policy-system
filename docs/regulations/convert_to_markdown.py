"""Convert EU regulation PDFs in this folder to Markdown using markitdown."""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from markitdown import (  # pyright: ignore[reportMissingImports]  # markitdown: not a workspace dep
    MarkItDown,  # pyright: ignore[reportUnknownVariableType]  # markitdown: unresolved -> Unknown symbol
)

if TYPE_CHECKING:
    # markitdown ships no py.typed (and is not installed in the workspace venv);
    # this Protocol pins the slice of its surface this script touches.
    class _ConversionResult(Protocol):
        @property
        def text_content(self) -> str: ...

    class _MarkItDown(Protocol):
        def convert(self, source: str) -> _ConversionResult: ...


REGULATIONS_DIR = Path(__file__).parent


def main() -> None:
    converter = cast("_MarkItDown", MarkItDown())
    pdf_files = sorted(REGULATIONS_DIR.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDF files found in {REGULATIONS_DIR}")
        return

    for pdf_path in pdf_files:
        print(f"Converting {pdf_path.name}...")
        result = converter.convert(str(pdf_path))
        output_path = pdf_path.with_suffix(".md")
        output_path.write_text(result.text_content, encoding="utf-8")
        print(f"  -> {output_path.name}")


if __name__ == "__main__":
    main()
