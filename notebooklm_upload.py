"""Upload a course's downloaded/converted content into a NotebookLM notebook.

The exporter (``export.py``) calls :func:`upload_course` per course after the
content has been downloaded and, when enabled, converted to Markdown. This
module uploads the files that should become NotebookLM sources, keeping a
persistent JSON state file so an interrupted run can be resumed without
re-uploading the same bytes.

Auth is read ONLY from the ``NOTEBOOKLM_AUTH_JSON`` environment variable
(inline Playwright storage-state JSON); we never install or drive Playwright
and never call ``login``. The optional ``NOTEBOOKLM_PROFILE`` environment
variable is forwarded to :meth:`NotebookLMClient.from_storage` as ``profile=``.

The ``notebooklm`` package is imported lazily, so this module (and the rest of
the exporter) imports and runs fine when it is not installed. Error handling is
best-effort and matches the exporter's tone: ``upload_course`` and
``upload_courses`` never raise.
"""

import asyncio
import hashlib
import html
import importlib.util
import json
import os
import re
import time

from naming import makeValidFilename

# NotebookLM supported upload types (exact set), with leading dots, lowercase.
SUPPORTED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".docx", ".csv", ".pptx", ".epub",
    ".3g2", ".3gp", ".aac", ".aif", ".aifc", ".aiff", ".amr", ".au",
    ".avi", ".m4a", ".mp3", ".mp4", ".mpeg", ".ogg", ".opus", ".ra",
    ".snd", ".wav", ".wma", ".avif", ".bmp", ".gif", ".ico", ".jp2",
    ".png", ".webp", ".tif", ".tiff", ".heic", ".heif", ".jpeg",
    ".jpg", ".jpe",
}

DEFAULT_MAX_SOURCES = 300
DEFAULT_MAX_AGE_DAYS = 90
STATE_FILE_NAME = ".notebooklm_state.json"

# Extensions whose Markdown sibling is a conversion artefact (pandoc/OCR).
# A `.md` next to one of these is skipped in favour of its source; a `.md`
# next to anything else (e.g. `.json`, `.zip`) is a real file and is kept.
_CONVERSION_SOURCE_EXTENSIONS = SUPPORTED_EXTENSIONS | {
    ".html", ".htm", ".doc", ".docx", ".odt", ".epub", ".rtf", ".txt",
    ".pptx", ".xlsx", ".ipynb", ".tex", ".rst", ".org", ".csv",
}

# Markers shared with the markdown exporter. These are duplicated here so this
# module keeps working even if markdown_export.py has not been written yet; the
# real values are preferred from markdown_export when it is importable.
GENERATED_HTML_MARKER = 'name="generator" content="canvas-student-data-export"'
GENERATED_MD_MARKER = "<!-- generated-by: canvas-student-data-export -->"

_MAX_TITLE_LENGTH = 250


def _get(obj, key, default=None):
    """Read ``key`` from a plain object or a dict, tolerating either."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _markdown_export():
    """Import markdown_export lazily; None when it is unavailable."""
    try:
        import markdown_export
        return markdown_export
    except Exception:
        return None


def _md_marker():
    mod = _markdown_export()
    if mod is not None and getattr(mod, "GENERATED_MD_MARKER", None):
        return mod.GENERATED_MD_MARKER
    return GENERATED_MD_MARKER


def _dateutil_parser():
    try:
        import dateutil.parser
        return dateutil.parser
    except Exception:
        return None


def _parse_date_to_epoch(value):
    """Epoch seconds for an exporter date string, or 0.0 when unparseable."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text or text.lower() in ("none", "null"):
        return 0.0

    parser = _dateutil_parser()
    if parser is not None:
        try:
            return parser.parse(text).timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0.0

    # Fallback for environments without python-dateutil: the exporter always
    # formats dates with this exact template.
    try:
        from datetime import datetime
        return datetime.strptime(text, "%B %d, %Y %I:%M %p").timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def notebooklm_enabled(auth_json=None):
    """True when NOTEBOOKLM_AUTH_JSON or NOTEBOOKLM_PROFILE is configured.

    ``NOTEBOOKLM_AUTH_JSON`` is inline storage-state JSON (CI friendly);
    ``NOTEBOOKLM_PROFILE`` selects a profile directory on disk. Either one is
    enough for the notebooklm package to authenticate without a browser.
    """
    if auth_json is None:
        auth_json = os.environ.get("NOTEBOOKLM_AUTH_JSON", "")
    if auth_json and str(auth_json).strip():
        return True
    profile = os.environ.get("NOTEBOOKLM_PROFILE", "")
    return bool(profile and str(profile).strip())


def course_last_modified(course_view, course_dir):
    """Epoch seconds of the most recent activity for a course.

    Takes the max over parseable assignment dates, page dates, announcement and
    discussion post dates (format "%B %d, %Y %I:%M %p"), and the mtime of every
    file under ``course_dir``. Returns 0.0 when nothing is parseable.
    """
    latest = 0.0

    def consider(value):
        nonlocal latest
        epoch = _parse_date_to_epoch(value)
        if epoch > latest:
            latest = epoch

    for assignment in _get(course_view, "assignments", []) or []:
        consider(_get(assignment, "assigned_date", ""))
        consider(_get(assignment, "due_date", ""))

    for page in _get(course_view, "pages", []) or []:
        consider(_get(page, "last_updated_date", ""))
        consider(_get(page, "created_date", ""))

    threads = (_get(course_view, "announcements", []) or []) + (
        _get(course_view, "discussions", []) or []
    )
    for thread in threads:
        consider(_get(thread, "posted_date", ""))

    try:
        for dirpath, dirnames, filenames in os.walk(course_dir):
            # Extracted media (pandoc/OCR) is written now and says nothing
            # about when the course was last active.
            dirnames[:] = [
                name for name in dirnames
                if name != "attachments" and not name.startswith(".")
            ]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                path = os.path.join(dirpath, filename)
                ext = os.path.splitext(filename)[1].lower()
                # Files this exporter just generated are not course activity:
                # their mtime is "now" and would make every course look recent.
                if ext == ".json":
                    continue
                if ext in (".html", ".htm") and _is_generated_html(path):
                    continue
                if ext == ".md" and _is_generated_markdown(path):
                    continue
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if mtime > latest:
                    latest = mtime
    except OSError:
        pass

    return latest


def is_recent(last_modified, max_age_days=DEFAULT_MAX_AGE_DAYS, now=None):
    """True when ``last_modified`` is within ``max_age_days`` of ``now``.

    ``now`` defaults to ``time.time()``. A ``last_modified`` of 0 or less means
    "no activity known" and returns False. Timestamps in the future (e.g. a
    Canvas due date of a course that is currently running) count as recent.
    """
    if last_modified is None:
        return False
    try:
        last = float(last_modified)
        max_days = float(max_age_days)
    except (TypeError, ValueError):
        return False
    if last <= 0:
        return False
    if now is None:
        now = time.time()
    try:
        age_seconds = float(now) - last
    except (TypeError, ValueError):
        return False
    return age_seconds <= max_days * 86400.0


# Extensions whose content is inspected for substance. Everything else
# (binary media, archives, ...) only needs to be non-empty; reading a large
# video into memory just to look for text would be wasteful.
_TEXT_SUBSTANCE_EXTENSIONS = {".md", ".txt", ".html", ".htm", ".csv", ".rst", ".org", ".tex"}


def _read_head(path, size=64 * 1024):
    """Read at most ``size`` characters from the start of a text file."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read(size)


def _read_text(path):
    """Read a text file, stripping a leading UTF-8 BOM if present."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.read()
    return content.lstrip("\ufeff")


def _is_generated_html(path):
    """True when the file is an HTML page written by html_export.py."""
    mod = _markdown_export()
    if mod is not None and hasattr(mod, "is_generated_html"):
        try:
            return bool(mod.is_generated_html(path))
        except Exception:
            pass
    try:
        head = _read_head(path)
        return GENERATED_HTML_MARKER in head
    except OSError:
        return False


def _is_generated_markdown(path):
    """True when the first bytes of a markdown file carry the exporter marker."""
    marker = _md_marker()
    try:
        head = _read_head(path, len(marker) + 64)
        return marker in head
    except OSError:
        return False


def _html_text(content):
    """Visible text of an HTML document, ignoring scripts/styles/tags."""
    body = content
    match = re.search(r"<body[^>]*>(.*)</body>", content, re.IGNORECASE | re.DOTALL)
    if match:
        body = match.group(1)
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    return html.unescape(text).replace("\u00a0", " ").strip()


def _has_substance(path):
    """True when a file carries more than boilerplate.

    Text formats (``.md``/``.txt``/``.html``/``.htm``/...) whose stripped
    content is empty (or only the generated-markdown marker) are treated as
    empty, as is an HTML document whose body has no visible text. Binary
    formats only need to be non-empty; their bytes are never read here.
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if os.path.getsize(path) == 0:
            return False
    except OSError:
        return False

    if ext not in _TEXT_SUBSTANCE_EXTENSIONS:
        return True

    try:
        content = _read_text(path)
    except OSError:
        return False

    if ext in (".html", ".htm"):
        text = _html_text(content)
    else:
        text = content.strip()

    if not text:
        return False
    if text == _md_marker().strip():
        return False
    return True


def _sha256(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _flattened_title(course_dir, source_path, used_titles):
    """Flatten a relative path into a display title, resolving collisions.

    Relative components are sanitized per component and joined with " - ";
    the extension is kept. Colliding titles get " (2)", " (3)", ... in
    deterministic (sorted-walk) order.
    """
    rel = os.path.relpath(source_path, course_dir)
    parts = rel.split(os.sep)
    clean = []
    for part in parts:
        name = makeValidFilename(part)
        clean.append(name if name else part)
    base = " - ".join(part for part in clean if part)
    if not base:
        base = makeValidFilename(os.path.basename(source_path)) or "file"

    count = used_titles.get(base, 0)
    title = base if not count else "%s (%d)" % (base, count + 1)
    used_titles[base] = count + 1
    return title


def collect_candidates(course_dir):
    """Walk ``course_dir`` and return the files that should become sources.

    Each candidate is ``{"path": abs path, "sha256": hexdigest, "title":
    flattened display title, "kind": "original" | "markdown"}``. Extracted
    media, hidden/empty files, JSON, generated HTML/Markdown, and files with no
    substance are skipped. Unsupported extensions are replaced by their sibling
    ``<stem>.md``; identical sha256 values are deduplicated (first wins).
    """
    candidates = []
    seen_sha256 = set()
    used_titles = {}

    for dirpath, dirnames, filenames in os.walk(course_dir):
        # Never recurse into extracted media or hidden directories.
        dirnames[:] = sorted(
            name for name in dirnames
            if name != "attachments" and not name.startswith(".")
        )

        for filename in sorted(filenames):
            if filename.startswith("."):
                continue

            path = os.path.join(dirpath, filename)
            ext = os.path.splitext(filename)[1].lower()

            # JSON is either an export this tool generated (<course>.json) or a
            # data file NotebookLM cannot ingest; never upload either.
            if ext == ".json":
                continue

            # Skip HTML pages written by this exporter.
            if ext in (".html", ".htm") and _is_generated_html(path):
                continue

            # Skip Markdown generated from our own HTML pages.
            if ext == ".md" and _is_generated_markdown(path):
                continue

            # A plain .md whose <stem>.html sibling is a generated page is the
            # same content and must not become a separate source.
            if ext == ".md":
                sibling_html = os.path.splitext(path)[0] + ".html"
                if os.path.isfile(sibling_html) and _is_generated_html(sibling_html):
                    continue

            # A markdown file with a convertible sibling of the same stem is a
            # conversion artefact (pandoc/OCR): the sibling is what gets
            # uploaded -- as the original when its type is supported, or as
            # this .md when it is not. Never upload both. A .md next to a
            # non-source file (json, zip, ...) is a real file and is kept.
            if ext == ".md":
                stem = os.path.splitext(filename)[0]
                if any(
                    other != filename
                    and os.path.splitext(other)[0] == stem
                    and os.path.splitext(other)[1].lower() in _CONVERSION_SOURCE_EXTENSIONS
                    for other in filenames
                ):
                    continue

            # Choose exactly one source per original file.
            source_path = path
            kind = "original"
            if ext not in SUPPORTED_EXTENSIONS:
                md_sibling = os.path.splitext(path)[0] + ".md"
                if os.path.isfile(md_sibling):
                    source_path = md_sibling
                    kind = "markdown"
                else:
                    continue

            try:
                if os.path.getsize(source_path) == 0:
                    continue
            except OSError:
                continue

            if not _has_substance(source_path):
                continue

            digest = _sha256(source_path)
            if digest is None:
                continue
            if digest in seen_sha256:
                continue
            seen_sha256.add(digest)

            candidates.append({
                "path": os.path.abspath(source_path),
                "sha256": digest,
                "title": _flattened_title(course_dir, source_path, used_titles),
                "kind": kind,
            })

    return candidates


def _notebooklm_available():
    """True when the notebooklm package can be imported."""
    try:
        return importlib.util.find_spec("notebooklm") is not None
    except Exception:
        return False


def _load_client():
    """Import notebooklm lazily and return its NotebookLMClient class."""
    import notebooklm
    return notebooklm.NotebookLMClient


def _notebooklm_profile():
    value = os.environ.get("NOTEBOOKLM_PROFILE", "")
    return value.strip() or None


def _new_state():
    return {"version": 2, "notebooks": {}, "notebook_ids": {}}


def _load_state(state_path):
    """Load the upload state, migrating the legacy flat format if needed.

    Current format: ``{"version": 2, "notebooks": {<title>: {<sha256>:
    {"title": ...}}}}``. The legacy format was ``{"<sha256>": {"notebook":
    <title>, "title": ...}}``, which could only record one notebook per hash
    and therefore re-uploaded files shared between courses on every run.
    """
    if not os.path.isfile(state_path):
        return _new_state()
    try:
        with open(state_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return _new_state()
    if not isinstance(data, dict):
        return _new_state()
    if isinstance(data.get("notebooks"), dict):
        notebook_ids = data.get("notebook_ids")
        return {
            "version": 2,
            "notebooks": data["notebooks"],
            "notebook_ids": notebook_ids if isinstance(notebook_ids, dict) else {},
        }

    migrated = _new_state()
    for sha, record in data.items():
        if isinstance(record, dict) and record.get("notebook"):
            notebook = str(record["notebook"])
            migrated["notebooks"].setdefault(notebook, {})[sha] = {
                "title": record.get("title", "")
            }
    return migrated


def _save_state(state, state_path):
    """Persist state atomically (write temp, then os.replace)."""
    directory = os.path.dirname(os.path.abspath(state_path))
    try:
        if directory and not os.path.exists(directory):
            os.makedirs(directory)
    except OSError:
        pass

    tmp_path = state_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, state_path)
    except Exception as e:
        print("    ERROR: could not persist NotebookLM state to %s: %s" % (state_path, e))
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


async def _upload_course_async(title, course_dir, state_path, max_sources, verbose, stats):
    NotebookLMClient = _load_client()
    profile = _notebooklm_profile()
    if profile:
        client_context = NotebookLMClient.from_storage(profile=profile)
    else:
        client_context = NotebookLMClient.from_storage()

    async with client_context as client:
        state = _load_state(state_path)
        notebook_ids = state.setdefault("notebook_ids", {})

        # Reuse a notebook with the same title (by cached id first, so reuse
        # does not depend on NotebookLM's "recently viewed" ordering), or
        # create one.
        notebook = None
        cached_id = notebook_ids.get(title)
        if cached_id:
            try:
                notebook = await client.notebooks.get(cached_id)
            except Exception:
                notebook = None
        if notebook is None:
            for candidate in await client.notebooks.list():
                if str(getattr(candidate, "title", "") or "") == title:
                    notebook = candidate
                    break
        created = False
        if notebook is None:
            notebook = await client.notebooks.create(title)
            created = True

        notebook_id = getattr(notebook, "id", None)
        if not notebook_id:
            raise RuntimeError("NotebookLM returned a notebook without an id")
        if notebook_ids.get(title) != notebook_id:
            notebook_ids[title] = notebook_id
            _save_state(state, state_path)

        stats["notebook"] = title
        stats["created"] = bool(created)

        existing = await client.sources.list(notebook_id)
        existing_titles = set()
        for source in existing:
            source_title = getattr(source, "title", None)
            if source_title:
                existing_titles.add(str(source_title))

        # The 300-source cap counts sources already in the notebook.
        remaining = int(max_sources) - len(existing)
        if remaining <= 0:
            candidates = collect_candidates(course_dir)
            stats["capped"] = len(candidates)
            print("    Note: notebook '%s' already has %d sources (cap %d); "
                  "skipping %d candidate uploads." % (title, len(existing), max_sources, len(candidates)))
            return stats

        candidates = sorted(collect_candidates(course_dir), key=lambda c: c["path"])
        uploaded_shas = state["notebooks"].setdefault(title, {})

        for cand in candidates:
            if cand["sha256"] in uploaded_shas:
                stats["deduped"] += 1
                continue

            if cand["title"] in existing_titles:
                stats["skipped"] += 1
                continue

            if stats["uploaded"] >= remaining:
                stats["capped"] += 1
                continue

            try:
                await client.sources.add_file(
                    notebook_id, cand["path"], title=cand["title"], wait=False
                )
                stats["uploaded"] += 1
                uploaded_shas[cand["sha256"]] = {"title": cand["title"]}
                existing_titles.add(cand["title"])
                _save_state(state, state_path)
                if verbose:
                    print("      ✓ Uploaded: %s" % cand["title"])
            except Exception as e:
                stats["failed"] += 1
                print("    ERROR: upload failed for %s: %s" % (cand["path"], e))
                if verbose:
                    import traceback
                    traceback.print_exc()

    return stats


def upload_course(title, course_dir, last_modified, state_path, *,
                  max_age_days=DEFAULT_MAX_AGE_DAYS,
                  max_sources=DEFAULT_MAX_SOURCES,
                  verbose=False):
    """Sync entry point called by export.py per course.

    Returns None when the course is not recent, auth is not configured, or the
    ``notebooklm`` package is not importable. Otherwise returns stats:
    ``{"notebook": title, "created": bool, "uploaded": int, "skipped": int,
    "failed": int, "capped": int, "deduped": int}``. Never raises.
    """
    title = str(title if title is not None else "").strip()
    if not title:
        title = os.path.basename(os.path.abspath(course_dir)) or "Notebook"
    title = title[:_MAX_TITLE_LENGTH]

    if not is_recent(last_modified, max_age_days):
        print("    Note: skipping NotebookLM upload for '%s' (not recent)." % title)
        return None

    if not notebooklm_enabled():
        print("    Note: NotebookLM is not configured (set NOTEBOOKLM_AUTH_JSON or "
              "NOTEBOOKLM_PROFILE); skipping NotebookLM upload.")
        return None

    if not _notebooklm_available():
        print("    Note: notebooklm-py is not installed; skipping NotebookLM upload "
              "(pip install notebooklm-py).")
        return None

    stats = {
        "notebook": title,
        "created": False,
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "capped": 0,
        "deduped": 0,
        "error": False,
    }
    try:
        asyncio.run(_upload_course_async(title, course_dir, state_path, max_sources, verbose, stats))
    except Exception as e:
        stats["error"] = True
        print("    ERROR: NotebookLM upload failed for '%s': %s" % (title, e))
        if verbose:
            import traceback
            traceback.print_exc()
    return stats


def _empty_stats():
    return {
        "notebook": "",
        "created": 0,
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "capped": 0,
        "deduped": 0,
        "error": False,
        "notebooks": 0,
        "skipped_courses": 0,
    }


def upload_courses(entries, state_path, *,
                   max_age_days=DEFAULT_MAX_AGE_DAYS,
                   max_sources=DEFAULT_MAX_SOURCES,
                   verbose=False):
    """Convenience for export.py over a list of course entries.

    ``entries`` are dicts with keys "title", "course_dir", "last_modified".
    Aggregates upload_course stats plus "notebooks" (notebooks created/reused)
    and "skipped_courses". Returns empty stats when auth is not configured.
    """
    if not notebooklm_enabled():
        print("    Note: NotebookLM is not configured (set NOTEBOOKLM_AUTH_JSON or "
              "NOTEBOOKLM_PROFILE); skipping NotebookLM upload.")
        return _empty_stats()

    totals = _empty_stats()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        result = upload_course(
            entry.get("title", ""),
            entry.get("course_dir", ""),
            entry.get("last_modified", 0.0),
            state_path,
            max_age_days=max_age_days,
            max_sources=max_sources,
            verbose=verbose,
        )
        if result is None:
            totals["skipped_courses"] += 1
            continue
        totals["notebooks"] += 1
        for key in ("uploaded", "skipped", "failed", "capped", "deduped"):
            totals[key] += int(result.get(key, 0))
    return totals
