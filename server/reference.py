"""Reference-doc RAG: reads UC Volume, parses via ai_parse_document or native, BM25 retrieval.

Slim first cut — in-memory index only, no Delta cache, no separate endpoints.
Opt-in via config: when `reference.volume_path` is empty the service is disabled and
generate handlers behave exactly as before.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_SUPPORTED_EXTS = (".pdf", ".md", ".txt")


class ReferenceService:
    """Thread-safe, in-memory BM25 index over a UC Volume of reference docs."""

    def __init__(self, volume_path: str, warehouse_id: str, workspace_client):
        self.volume_path = volume_path.rstrip("/")
        self.warehouse_id = warehouse_id
        self.w = workspace_client
        self._lock = threading.Lock()
        self._chunks: list[dict] = []  # [{"text": str, "source": str}, ...]
        self._bm25 = None  # BM25Okapi | None
        self._built_at: float = 0.0
        self._file_sigs: dict[str, str] = {}  # path -> "size-mtime"

    # ── File listing ─────────────────────────────────────────────────────

    def _list_files(self) -> list[dict]:
        """List files in the Volume root + one level of subdirs. Returns [{"path","size","mtime"}]."""
        out: list[dict] = []

        def _add(entry) -> None:
            path = getattr(entry, "path", None)
            if not path:
                return
            if not any(path.lower().endswith(ext) for ext in _SUPPORTED_EXTS):
                return
            size = getattr(entry, "file_size", None) or 0
            mtime = getattr(entry, "modification_time", None) or 0
            out.append({"path": path, "size": int(size), "mtime": int(mtime)})

        top_entries = list(self.w.files.list_directory_contents(self.volume_path))
        for e in top_entries:
            if getattr(e, "is_directory", False):
                try:
                    for sub in self.w.files.list_directory_contents(e.path):
                        if not getattr(sub, "is_directory", False):
                            _add(sub)
                except Exception as sub_err:
                    logger.warning("Could not list sub-dir %s: %s", e.path, sub_err)
            else:
                _add(e)
        return out

    def _needs_rebuild(self, files: list[dict]) -> bool:
        sigs = {f["path"]: f"{f['size']}-{f['mtime']}" for f in files}
        return sigs != self._file_sigs

    # ── Parsing ──────────────────────────────────────────────────────────

    def _read_file(self, path: str) -> tuple[str, str]:
        """Return (parsed_markdown, parser_name). Handles .pdf/.md/.txt."""
        ext = path.rsplit(".", 1)[-1].lower()

        if ext in ("md", "txt"):
            resp = self.w.files.download(path)
            raw = resp.contents.read()
            try:
                return raw.decode("utf-8"), "native"
            except UnicodeDecodeError:
                return raw.decode("latin-1", errors="replace"), "native"

        if ext == "pdf":
            # Use CAST(...AS STRING) fallback — simpler shape, ai_parse_document
            # returns either a markdown string or a JSON-serialised struct; either
            # way the cast gives us something indexable.
            from databricks.sdk.service.sql import (
                StatementParameterListItem,
                StatementState,
            )
            sql = (
                "SELECT CAST(ai_parse_document(content) AS STRING) AS parsed "
                "FROM read_files(:path, format => 'binaryFile')"
            )
            params = [StatementParameterListItem(name="path", value=path)]
            resp = self.w.statement_execution.execute_statement(
                warehouse_id=self.warehouse_id,
                statement=sql,
                parameters=params,
                wait_timeout="50s",
            )
            if not resp.status or resp.status.state != StatementState.SUCCEEDED:
                msg = (resp.status.error.message if resp.status and resp.status.error else "unknown")
                raise RuntimeError(f"ai_parse_document failed: {msg}")
            if not resp.result or not resp.result.data_array:
                return "", "ai_parse_document"
            parsed = resp.result.data_array[0][0] or ""
            return str(parsed), "ai_parse_document"

        raise ValueError(f"Unsupported file extension: {ext}")

    # ── Chunking ─────────────────────────────────────────────────────────

    @staticmethod
    def _split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
        """Recursive character splitter modelled on langchain's defaults."""
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=overlap,
                separators=["\n## ", "\n### ", "\n\n", "\n", ". ", " "],
            )
            return [c for c in splitter.split_text(text) if c.strip()]
        except ImportError:
            # Lightweight fallback — simple char-size slicing with overlap.
            text = text.strip()
            if not text:
                return []
            out = []
            i = 0
            while i < len(text):
                out.append(text[i : i + chunk_size])
                i += max(1, chunk_size - overlap)
            return out

    # ── Index build ──────────────────────────────────────────────────────

    def _rebuild(self, files: list[dict]) -> None:
        from rank_bm25 import BM25Okapi

        chunks: list[dict] = []
        for f in files:
            try:
                text, parser = self._read_file(f["path"])
                logger.info("Parsed %s with %s (%d chars)", f["path"], parser, len(text))
                for piece in self._split_text(text):
                    chunks.append({"text": piece, "source": f["path"]})
            except Exception as e:
                logger.warning("Failed to parse %s: %s", f["path"], e)

        if not chunks:
            self._chunks, self._bm25 = [], None
            self._file_sigs = {f["path"]: f"{f['size']}-{f['mtime']}" for f in files}
            self._built_at = time.time()
            return

        tokenized = [c["text"].lower().split() for c in chunks]
        self._chunks = chunks
        self._bm25 = BM25Okapi(tokenized)
        self._file_sigs = {f["path"]: f"{f['size']}-{f['mtime']}" for f in files}
        self._built_at = time.time()
        logger.info("Built BM25 index: %d chunks from %d files", len(chunks), len(files))

    # ── Retrieval ────────────────────────────────────────────────────────

    def retrieve(self, table_info: dict, top_k: int = 3) -> list[dict]:
        """Return top_k chunks relevant to this table. Rebuilds index if Volume changed."""
        with self._lock:
            try:
                files = self._list_files()
            except Exception as e:
                logger.warning("Could not list volume %s: %s", self.volume_path, e)
                return []

            if self._needs_rebuild(files):
                self._rebuild(files)

            if not self._bm25 or not self._chunks:
                return []

            query = f"{table_info.get('full_name', '')} " + " ".join(
                c.get("name", "") for c in table_info.get("columns", [])
            )
            tokens = query.lower().split()
            if not tokens:
                return []
            scores = self._bm25.get_scores(tokens)
            ranked = sorted(zip(scores, self._chunks), key=lambda x: -x[0])[:top_k]
            return [
                {"text": c["text"], "source": c["source"], "score": float(s)}
                for s, c in ranked
                if s > 0
            ]


# ── Singleton ────────────────────────────────────────────────────────────

_service: Optional[ReferenceService] = None
_service_lock = threading.Lock()


def get_reference_service() -> Optional[ReferenceService]:
    """Return the initialised ReferenceService, or None if reference feature disabled."""
    global _service
    if _service is not None:
        return _service

    from .config import app_config, get_workspace_client
    if not app_config.reference_volume_path:
        return None

    with _service_lock:
        if _service is not None:
            return _service

        from .warehouse import resolve_warehouse_id
        try:
            wh = resolve_warehouse_id()
        except Exception as e:
            logger.warning("Reference service disabled: no warehouse (%s)", e)
            return None

        _service = ReferenceService(
            volume_path=app_config.reference_volume_path,
            warehouse_id=wh,
            workspace_client=get_workspace_client(),
        )
        logger.info(
            "Reference service initialised: volume=%s top_k=%d",
            _service.volume_path, app_config.reference_top_k,
        )
    return _service
