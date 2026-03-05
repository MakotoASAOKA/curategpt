"""Full-document file wrapper — reads files without chunking.

Unlike :class:`FilesystemWrapper`, this wrapper does **not** call
``split_objects()`` and therefore preserves the entire document as a single
vector-store record.  This is required by :class:`PaperToDDIAgent`, which
performs its own section-aware splitting before calling the LLM.

Supported formats (same as FilesystemWrapper):
  * Plain text / Markdown / Python source — read directly
  * PDF — pdfplumber (preferred, preserves layout) → textract (fallback)
  * Word, Excel, PowerPoint, etc. — textract
"""

import glob as glob_module
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Dict, Iterable, Iterator, Optional

from curategpt.wrappers.base_wrapper import BaseWrapper

logger = logging.getLogger(__name__)

# Extensions handled with plain open() — no binary extraction needed
_PLAIN_TEXT_EXTENSIONS = {".py", ".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml"}


def _read_pdf_pdfplumber(path: str) -> str:
    """Extract text from a PDF using pdfplumber (preferred).

    Returns the concatenated text of all pages.  Raises ImportError when
    pdfplumber is not installed so the caller can fall through to textract.
    """
    import pdfplumber  # optional dependency

    pages = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                pages.append(page_text)
    return "\n\n".join(pages)


def _read_file_textract(path: str) -> str:
    """Extract text using textract (supports many binary formats)."""
    import textract  # optional dependency

    raw = textract.process(path)
    return raw.decode("utf-8", errors="replace")


@dataclass
class FullDocumentWrapper(BaseWrapper):
    """A file-system wrapper that returns each file as a **single** document.

    Unlike :class:`~curategpt.wrappers.general.filesystem_wrapper.FilesystemWrapper`,
    this wrapper skips the ``split_objects()`` step so the full text is stored
    in one vector-store record.  This makes it suitable for use as the
    ``document_adapter`` of :class:`~curategpt.agents.paper_to_ddi_agent.PaperToDDIAgent`.

    Parameters
    ----------
    root_directory:
        Root directory to scan for files.
    glob:
        Optional glob pattern (e.g. ``"*.pdf"``).  When omitted, all files
        under *root_directory* are processed.
    skip_unprocessable:
        When *True* (default), files that cannot be extracted are logged as
        warnings instead of raising an exception.
    prefer_pdfplumber:
        When *True* (default), PDF files are extracted with *pdfplumber*
        first, falling back to *textract* on error.
    """

    name: ClassVar[str] = "full_document"

    root_directory: Optional[str] = None
    glob: Optional[str] = None
    skip_unprocessable: bool = True
    prefer_pdfplumber: bool = True

    # Raise max_text_length so parent-class helpers never truncate
    max_text_length: int = 200_000

    def objects(
        self,
        collection: str = None,
        object_ids: Optional[Iterable[str]] = None,
        **kwargs,
    ) -> Iterator[Dict]:
        """Yield one dict per file with the full document text."""
        if object_ids is not None:
            yield from self.objects_by_ids(list(object_ids))
            return

        root = self.root_directory or "."
        if self.glob:
            files = glob_module.glob(
                os.path.join(root, "**", self.glob), recursive=True
            )
        else:
            files = []
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    files.append(os.path.join(dirpath, filename))

        for file_path in sorted(set(files)):
            obj = self._file_to_object(file_path)
            if obj is not None:
                yield obj

    def objects_by_ids(self, object_ids: Iterable[str]) -> Iterator[Dict]:
        """Yield objects for specific file paths."""
        for file_path in object_ids:
            obj = self._file_to_object(file_path)
            if obj is not None:
                yield obj

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _file_to_object(self, file_path: str) -> Optional[Dict]:
        """Read *file_path* and return a dict, or *None* on failure."""
        try:
            text = self._read_file(file_path)
        except Exception as exc:
            if self.skip_unprocessable:
                logger.warning("Could not extract text from %s: %s", file_path, exc)
                return None
            raise

        path = Path(file_path)
        try:
            stat = path.lstat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            size = None
            mtime = None

        return {
            "id": file_path,
            "name": path.name,
            "text": text,
            "parent": str(path.parent),
            "size": size,
            "mtime": mtime,
        }

    def _read_file(self, file_path: str) -> str:
        """Return full text of *file_path* as a UTF-8 string."""
        suffix = Path(file_path).suffix.lower()

        # Plain-text formats — fast path
        if suffix in _PLAIN_TEXT_EXTENSIONS:
            return Path(file_path).read_text(encoding="utf-8", errors="replace")

        # PDF: pdfplumber preferred
        if suffix == ".pdf" and self.prefer_pdfplumber:
            try:
                text = _read_pdf_pdfplumber(file_path)
                if text.strip():
                    return text
                logger.debug(
                    "pdfplumber returned empty text for %s; falling back to textract",
                    file_path,
                )
            except ImportError:
                logger.debug("pdfplumber not installed; using textract for %s", file_path)
            except Exception as exc:
                logger.debug(
                    "pdfplumber failed for %s (%s); falling back to textract", file_path, exc
                )

        # Generic binary formats via textract
        return _read_file_textract(file_path)
