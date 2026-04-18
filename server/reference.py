"""Per-schema reference-docs via UC Volume + ai_parse_document.

Slim design (no chunking, no BM25, no retrieval):
    - For `<catalog>.<schema>.<table>` generate requests, look in
      `/Volumes/<catalog>/<schema>/<volume_name>/` for reference docs.
    - Parse .pdf via a single `ai_parse_document` SQL call over the folder.
    - Read .md / .txt natively via the Files API.
    - Concatenate full markdown (clipped to per-doc + total char caps) and
      hand the whole thing to the LLM as context — the LLM does its own
      "find the relevant bits".

Caching:
    In-memory dict on the service instance keyed on the Volume path; each
    entry tracks the file's modification_time, so unchanged files are
    re-served from cache without another parse. Thread-safe via a lock.

Governance:
    Native UC. Whoever owns the schema grants READ VOLUME on their own
    Volume to the app service principal. If the Volume doesn't exist or
    the SP can't read it, `get_reference_context` returns an empty result
    and the app degrades silently to today's behaviour.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Any

from databricks.sdk.service.sql import StatementState

logger = logging.getLogger(__name__)

# Extensions we know how to handle.
_PDF_EXTS = {"pdf"}
_TEXT_EXTS = {"md", "txt", "markdown"}

# Snippet length surfaced to the UI for each source in the "Informed by N sources"
# disclosure. Short enough to stay compact in the UI, long enough to convey what
# each doc contributed. The LLM still sees the full per_doc_max slice.
_UI_SNIPPET_CHARS = 200


@dataclass
class _CachedDoc:
    mtime: str
    markdown: str
    parse_status: str = "parsed"        # parsed | parsed_with_warnings | failed
    parse_error: str = ""


class ReferenceService:
    """Fetch + cache reference docs from per-schema UC Volumes."""

    def __init__(
        self,
        volume_name: str,
        warehouse_id: str,
        workspace_client,
        per_doc_max_chars: int = 8000,
        total_max_chars: int = 40000,
    ):
        self.volume_name = volume_name
        self.warehouse_id = warehouse_id
        self.w = workspace_client
        self.per_doc_max = per_doc_max_chars
        self.total_max = total_max_chars
        self._cache: dict[str, _CachedDoc] = {}
        # Failed-parse tracking keyed on path → last error string.
        self._failures: dict[str, str] = {}
        self._lock = threading.Lock()

    # ── Path helpers ────────────────────────────────────────────────────

    def _volume_path(self, catalog: str, schema: str) -> str:
        return f"/Volumes/{catalog}/{schema}/{self.volume_name}"

    @staticmethod
    def _ext_of(path: str) -> str:
        _, _, ext = path.rpartition(".")
        return ext.lower() if ext else ""

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Strip ``dbfs:`` prefix so paths from ``read_files()``
        (``dbfs:/Volumes/...``) and the Files API (``/Volumes/...``) can be
        compared directly.
        """
        if path.startswith("dbfs:"):
            return path[len("dbfs:"):]
        return path

    # ── Volume listing ──────────────────────────────────────────────────

    def _list_volume(self, path: str) -> list[dict[str, Any]]:
        """Return a flat list of file entries at `path`, or [] on any error.

        Uses the Files API (`w.files.list_directory_contents`).  Expected
        errors (volume missing, no permission, etc.) are logged at debug
        level and return []; unexpected errors are logged at warning.
        """
        try:
            entries = list(self.w.files.list_directory_contents(path))
        except Exception as e:
            # Most common: ResourceDoesNotExist (volume missing) or
            # PermissionDenied (SP lacks READ VOLUME). We swallow both.
            logger.debug("Reference Volume not available at %s: %s", path, e)
            return []

        files: list[dict[str, Any]] = []
        for entry in entries:
            # DirectoryEntry fields: path, is_directory, file_size, modification_time, name
            if getattr(entry, "is_directory", False):
                continue
            fpath = getattr(entry, "path", "") or ""
            if not fpath:
                continue
            fpath = self._normalize_path(fpath)
            ext = self._ext_of(fpath)
            if ext not in _PDF_EXTS and ext not in _TEXT_EXTS:
                logger.debug("Skipping unsupported file type in reference volume: %s", fpath)
                continue
            # modification_time: int ms since epoch
            mtime_raw = getattr(entry, "modification_time", 0) or 0
            files.append({
                "path": fpath,
                "name": getattr(entry, "name", fpath.rsplit("/", 1)[-1]),
                "size": getattr(entry, "file_size", 0) or 0,
                "mtime": str(mtime_raw),
                "ext": ext,
            })
        return files

    # ── PDF parsing via ai_parse_document ───────────────────────────────

    def _parse_pdfs(self, volume_path: str, paths_to_parse: list[str]) -> dict[str, str]:
        """Parse every PDF in `volume_path` with one SQL call, then filter.

        `ai_parse_document` returns a VARIANT whose shape is
        `{ document: { elements: [{ content: "..." }, ...] } }`. We use
        VARIANT accessor syntax (`:`) to reach `document:elements`, cast to
        a typed array, and flatten the `content` fields into a single
        markdown string with blank-line separators.

        We try several extraction forms in order; the first that succeeds
        and yields non-empty content is used. This hedges against
        `ai_parse_document` return-shape drift across workspace versions.
        """
        if not paths_to_parse:
            return {}

        # `read_files(...)` returns paths as `dbfs:/Volumes/...`, whereas the
        # Files API lists them as `/Volumes/...`. Normalise for comparison.
        target_paths = {self._normalize_path(p) for p in paths_to_parse}
        logger.info("Parsing %d PDF(s) via ai_parse_document from %s", len(paths_to_parse), volume_path)

        extraction_sqls = [
            # Preferred: flatten the nested elements[*].content array into a
            # single readable markdown string (best readability for the LLM).
            (
                "concat_ws('\\n\\n', "
                "from_json(CAST(ai_parse_document(content):document:elements AS STRING), "
                "'array<struct<content:string>>').content)"
            ),
            # Fallback #1: some workspace versions expose a flat text field.
            "CAST(ai_parse_document(content):document:text AS STRING)",
            # Fallback #2: whole VARIANT cast to string (JSON blob — usable but ugly).
            "CAST(ai_parse_document(content) AS STRING)",
        ]

        for expr in extraction_sqls:
            sql = (
                f"SELECT path, {expr} AS parsed "
                f"FROM read_files('{volume_path}', "
                f"format => 'binaryFile', pathGlobFilter => '*.pdf')"
            )
            try:
                resp = self.w.statement_execution.execute_statement(
                    warehouse_id=self.warehouse_id,
                    statement=sql,
                    wait_timeout="50s",
                )
            except Exception as e:
                logger.warning("ai_parse_document SQL call raised for expr %r: %s", expr, e)
                continue

            if not resp.status or resp.status.state != StatementState.SUCCEEDED:
                err = resp.status.error.message if resp.status and resp.status.error else "unknown"
                logger.warning("ai_parse_document form failed (%s): %s", expr[:60], err)
                continue

            rows = (resp.result.data_array or []) if resp.result else []
            if not rows:
                logger.debug("ai_parse_document returned no rows for %s — volume may be empty", volume_path)
                continue

            cols = [c.name for c in resp.manifest.schema.columns]
            out: dict[str, str] = {}
            for row in rows:
                r = dict(zip(cols, row))
                fpath = self._normalize_path(r.get("path") or "")
                parsed = r.get("parsed") or ""
                if fpath in target_paths and isinstance(parsed, str) and parsed.strip():
                    out[fpath] = parsed
            if out:
                logger.info("ai_parse_document succeeded for %d PDF(s) using form: %s", len(out), expr[:60])
                return out
            logger.warning(
                "ai_parse_document form yielded empty content (rows=%d, target_paths=%s), trying next",
                len(rows), list(target_paths)[:3],
            )

        logger.warning("All ai_parse_document extraction forms failed for %s", volume_path)
        return {}

    # ── Text (md / txt) parsing via Files API ───────────────────────────

    def _parse_text(self, path: str) -> str:
        """Read an md/txt file directly from the Volume via Files API."""
        resp = self.w.files.download(path)
        body = resp.contents.read() if hasattr(resp, "contents") else resp
        if isinstance(body, (bytes, bytearray)):
            return body.decode("utf-8", errors="replace")
        return str(body)

    # ── Internal cache refresh ──────────────────────────────────────────

    def _refresh_schema(self, catalog: str, schema: str, force: bool = False, preview: bool = False) -> tuple[str, list[dict[str, Any]]]:
        """Ensure the cache is up to date for `<catalog>.<schema>`.

        Returns ``(volume_path, files_with_status)`` — one entry per file
        present in the Volume (whether or not parsing succeeded). Callers
        hold ``self._lock``.

        ``preview=True`` skips the parse step entirely — just lists files
        and reports cache state. Uncached files show ``status = "pending"``.
        Used by the UI to render a "Parsing N PDFs…" indicator before the
        slow parse call that follows.
        """
        vpath = self._volume_path(catalog, schema)
        files = self._list_volume(vpath)
        if not files:
            return vpath, []

        # Partition: cache-hit vs needs-parse (force=True re-parses everything).
        needs_parse_pdf: list[str] = []
        needs_parse_text: list[str] = []
        for f in files:
            if force:
                if f["ext"] in _PDF_EXTS:
                    needs_parse_pdf.append(f["path"])
                elif f["ext"] in _TEXT_EXTS:
                    needs_parse_text.append(f["path"])
                continue
            cached = self._cache.get(f["path"])
            if cached and cached.mtime == f["mtime"]:
                continue
            if f["ext"] in _PDF_EXTS:
                needs_parse_pdf.append(f["path"])
            elif f["ext"] in _TEXT_EXTS:
                needs_parse_text.append(f["path"])

        n_parse = len(needs_parse_pdf) + len(needs_parse_text)

        # Preview mode: skip the expensive parsing. The UI will follow up
        # with a real status call immediately after rendering its indicator.
        if preview:
            needs_parse_pdf = []
            needs_parse_text = []

        if n_parse and not preview:
            logger.info(
                "Parsing %d file(s) from reference volume %s (cached: %d)",
                n_parse, vpath, len(files) - n_parse,
            )

        # PDFs: one batched SQL call; record per-path success/failure.
        if needs_parse_pdf:
            try:
                parsed = self._parse_pdfs(vpath, needs_parse_pdf)
            except Exception as e:
                logger.warning("PDF parsing raised: %s", e)
                parsed = {}
            for p in needs_parse_pdf:
                md = parsed.get(p, "")
                if md and md.strip():
                    mtime = next((f["mtime"] for f in files if f["path"] == p), "")
                    self._cache[p] = _CachedDoc(mtime=mtime, markdown=md, parse_status="parsed")
                    self._failures.pop(p, None)
                else:
                    self._failures[p] = "ai_parse_document returned no usable content"

        # md/txt: one-by-one via Files API.
        for p in needs_parse_text:
            try:
                md = self._parse_text(p)
                mtime = next((f["mtime"] for f in files if f["path"] == p), "")
                if md.strip():
                    self._cache[p] = _CachedDoc(mtime=mtime, markdown=md, parse_status="parsed")
                else:
                    self._cache[p] = _CachedDoc(
                        mtime=mtime, markdown=md,
                        parse_status="parsed_with_warnings", parse_error="empty file",
                    )
                self._failures.pop(p, None)
            except Exception as e:
                logger.warning("Failed to read text file %s: %s", p, e)
                self._failures[p] = str(e)

        # Compose per-file status for the UI.
        status_files: list[dict[str, Any]] = []
        for f in files:
            cached = self._cache.get(f["path"])
            failed = self._failures.get(f["path"])
            if cached and not failed:
                status = cached.parse_status
                error = cached.parse_error
                char_count = len(cached.markdown)
            elif failed:
                status = "failed"
                error = failed
                char_count = 0
            else:
                status = "pending"
                error = ""
                char_count = 0
            status_files.append({
                "path": f["path"],
                "filename": f["name"],
                "size_bytes": f["size"],
                "ext": f["ext"],
                "status": status,
                "error": error,
                "char_count": char_count,
            })

        return vpath, status_files

    # ── Public API ──────────────────────────────────────────────────────

    def get_reference_context(
        self, catalog: str, schema: str
    ) -> tuple[str, list[dict[str, str]]]:
        """Return ``(concatenated_markdown, [sources])`` for ``<catalog>.<schema>``.

        ``sources`` is a list of ``{filename, snippet}`` dicts — one entry
        per doc that actually contributed context to the LLM prompt.
        ``snippet`` is the first ~200 chars of that doc's contribution,
        with whitespace collapsed so it renders cleanly in a single line.
        Empty string + ``[]`` when the volume is missing, inaccessible, or
        produced no usable content.
        """
        with self._lock:
            _, status_files = self._refresh_schema(catalog, schema, force=False)
            if not status_files:
                return "", []

            parts: list[str] = []
            sources: list[dict[str, str]] = []
            for f in status_files:
                cached = self._cache.get(f["path"])
                if not cached or not cached.markdown.strip():
                    continue
                snippet_full = cached.markdown[: self.per_doc_max]
                filename = f["filename"] or f["path"].rsplit("/", 1)[-1]
                parts.append(f"[Source: {filename}]\n{snippet_full}")
                # Compact preview for the UI: collapse whitespace so it reads
                # nicely in a single paragraph.
                ui_preview = " ".join(snippet_full[: _UI_SNIPPET_CHARS].split())
                sources.append({"filename": filename, "snippet": ui_preview})

            if not parts:
                return "", []

            combined = "\n\n---\n".join(parts)
            if len(combined) > self.total_max:
                logger.warning(
                    "Reference context truncated: %d -> %d chars",
                    len(combined), self.total_max,
                )
                combined = combined[: self.total_max]
            return combined, sources

    def get_status(
        self, catalog: str, schema: str, force_refresh: bool = False, preview: bool = False
    ) -> dict[str, Any]:
        """Return the reference-doc panel data for ``<catalog>.<schema>``.

        Shape::

            {
                "volume_path": "/Volumes/<c>/<s>/reference_docs",
                "volume_name": "reference_docs",
                "files": [
                    {
                        "filename": "...", "status": "parsed"|...,
                        "error": "", "char_count": 12345,
                        "size_bytes": ..., "ext": "pdf"|"md"|"txt",
                    }, ...
                ],
                "total_char_count": 12345,
                "total_char_budget": 40000,
            }

        ``force_refresh=True`` re-parses every file (ignores mtime cache).
        """
        with self._lock:
            vpath, status_files = self._refresh_schema(catalog, schema, force=force_refresh, preview=preview)
            total_chars = sum(f["char_count"] for f in status_files)
            if total_chars > self.total_max:
                total_chars = self.total_max
            return {
                "volume_path": vpath,
                "volume_name": self.volume_name,
                "files": status_files,
                "total_char_count": total_chars,
                "total_char_budget": self.total_max,
            }
