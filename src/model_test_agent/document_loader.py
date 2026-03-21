from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class DocumentContent:
    path: Path
    media_type: str
    text: str


class DocumentLoader:
    def load(self, path: str | Path) -> DocumentContent:
        target = Path(path).expanduser().resolve()
        suffix = target.suffix.lower()
        if suffix in {".md", ".txt"}:
            return DocumentContent(path=target, media_type="text/markdown", text=target.read_text())
        if suffix == ".pdf":
            return DocumentContent(path=target, media_type="application/pdf", text=self._read_pdf(target))
        raise ValueError(f"Unsupported document type: {target.suffix}")

    def _read_pdf(self, path: Path) -> str:
        try:
            import fitz  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "PDF support requires PyMuPDF. Install with: pip install 'model-test-agent[pdf]'"
            ) from exc
        parts: list[str] = []
        with fitz.open(path) as pdf:
            for page in pdf:
                parts.append(page.get_text("text"))
        return "\n\n".join(parts)
