"""OCR images and PDFs into Markdown using the Mistral OCR API.

When a Mistral API key is configured, ``ocr_file``/``ocr_tree`` send a PDF or
image to the Mistral OCR endpoint, save any images the API extracts into an
``attachments/<stem>/`` directory next to the source (the same layout as the
pandoc ``--extract-media`` directory), and rewrite the image references in the
returned Markdown to those relative paths.

This module never talks to the network at import time and never requires an API
key just to be imported. Error handling is best-effort: ``ocr_file`` and
``ocr_tree`` never raise.

Requests are throttled to about one page per second (configurable through the
``MISTRAL_OCR_INTERVAL`` environment variable or the ``min_interval``
argument) and retried with backoff on HTTP 429/5xx, because Mistral rate limits
the OCR endpoint aggressively. PDFs are uploaded once to the Mistral Files API
and then requested page range by page range (``MISTRAL_OCR_CHUNK_PAGES``,
default 1), so the wait happens while the document is being processed instead
of after one huge request.
"""

import base64
import os
import time

import requests

from naming import makeValidFilename

# Extensions the Mistral OCR API accepts here.
OCR_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    ".tif", ".tiff", ".avif", ".heic", ".heif",
}

MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
MISTRAL_FILES_URL = "https://api.mistral.ai/v1/files"
DEFAULT_OCR_MODEL = "mistral-ocr-latest"
DEFAULT_OCR_TIMEOUT = 120.0

# OCR is rate limited to one page per second by default; PDFs are uploaded once
# to the Files API and then requested one page range at a time, so the wait
# happens during the document instead of after one huge request.
DEFAULT_MIN_REQUEST_INTERVAL = 1.0
DEFAULT_PAGES_PER_REQUEST = 1
DEFAULT_MAX_RETRIES = 5
MAX_OCR_PAGES = 5000

# Earliest monotonic time at which the next OCR request may start.
_last_request_at = 0.0

# Mistral rejects data URIs above roughly 50 MB. The data URI is slightly
# larger than the base64 payload alone, so comparing against the encoded
# length is a safe, simple cap.
MAX_DATA_URI_BYTES = 50 * 1024 * 1024

_IMAGE_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def ocr_configured(api_key):
    """True when an API key is present."""
    return bool(api_key) and bool(str(api_key).strip())


def _resolve_interval(min_interval):
    """Seconds charged per OCR page (argument, MISTRAL_OCR_INTERVAL or default)."""
    if min_interval is not None:
        try:
            return max(0.0, float(min_interval))
        except (TypeError, ValueError):
            pass
    try:
        return max(0.0, float(os.environ.get(
            "MISTRAL_OCR_INTERVAL", DEFAULT_MIN_REQUEST_INTERVAL)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_REQUEST_INTERVAL


def _throttle(interval):
    """Sleep until the rate budget allows the next OCR request."""
    if interval <= 0:
        return
    wait = _last_request_at - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _note_pages(page_count, interval):
    """Charge processed pages against the rate budget (1 page/s by default)."""
    global _last_request_at
    if interval <= 0:
        return
    try:
        pages = max(1, int(page_count))
    except (TypeError, ValueError):
        pages = 1
    now = time.monotonic()
    _last_request_at = max(now, _last_request_at) + pages * interval


def _retry_delay(response, interval, attempt):
    retry_after = response.headers.get("Retry-After") if response.headers else None
    if retry_after:
        try:
            return max(float(retry_after), interval)
        except (TypeError, ValueError):
            pass
    return max(interval, 2.0 ** attempt)


def _post_ocr(payload, headers, timeout, interval, verbose=False):
    """POST the OCR request with throttling and 429/5xx retries."""
    response = None
    for attempt in range(1, DEFAULT_MAX_RETRIES + 1):
        _throttle(interval)
        response = requests.post(
            MISTRAL_OCR_URL, headers=headers, json=payload, timeout=timeout
        )
        status = getattr(response, "status_code", 200)
        if status == 429 or status >= 500:
            if attempt < DEFAULT_MAX_RETRIES:
                delay = _retry_delay(response, interval, attempt)
                if verbose:
                    print("      Mistral rate limit (HTTP %s); retrying in %.0fs (%d/%d)"
                          % (status, delay, attempt, DEFAULT_MAX_RETRIES - 1))
                time.sleep(delay)
                continue
        response.raise_for_status()
        return response
    response.raise_for_status()


def _resolve_pages_per_request(pages_per_request):
    """Pages sent per OCR request (argument, MISTRAL_OCR_CHUNK_PAGES or default)."""
    if pages_per_request is not None:
        try:
            return max(1, int(pages_per_request))
        except (TypeError, ValueError):
            pass
    try:
        return max(1, int(os.environ.get(
            "MISTRAL_OCR_CHUNK_PAGES", DEFAULT_PAGES_PER_REQUEST)))
    except (TypeError, ValueError):
        return DEFAULT_PAGES_PER_REQUEST


def _error_detail(exc):
    """Best-effort human-readable detail from a requests HTTP error."""
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        data = response.json()
        if isinstance(data, dict):
            for key in ("message", "detail", "error"):
                value = data.get(key)
                if value:
                    return " ".join(str(value).split())[:300]
    except Exception:
        pass
    text = getattr(response, "text", "") or ""
    return " ".join(text.split())[:300]


def _inline_document(path):
    """Build the inline data-URI document for a pdf/image, or None."""
    try:
        with open(path, "rb") as source:
            raw = source.read()
    except OSError:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    if len(encoded) > MAX_DATA_URI_BYTES:
        return None
    return _document_payload(path, encoded)


def _pdf_page_count(path):
    """Number of pages in a PDF, or None when it cannot be determined."""
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        count = len(PdfReader(path).pages)
    except Exception:
        return None
    return count if count > 0 else None


def _upload_ocr_file(path, key, timeout, verbose=False):
    """Upload a PDF once to the Mistral Files API; return its id or None."""
    try:
        with open(path, "rb") as handle:
            response = requests.post(
                MISTRAL_FILES_URL,
                headers={"Authorization": "Bearer " + key},
                files={"file": (os.path.basename(path), handle, "application/pdf")},
                data={"purpose": "ocr"},
                timeout=timeout,
            )
        response.raise_for_status()
        file_id = response.json().get("id")
        if file_id:
            if verbose:
                print("      Uploaded to the Mistral Files API: %s" % file_id)
            return file_id
    except Exception as e:
        print("    Note: could not upload %s to the Mistral Files API (%s); "
              "sending it inline instead." % (path, e))
    return None


def _delete_ocr_file(file_id, key, timeout):
    """Best-effort cleanup of a file uploaded for OCR."""
    try:
        requests.delete(MISTRAL_FILES_URL + "/" + str(file_id),
                        headers={"Authorization": "Bearer " + key},
                        timeout=timeout)
    except Exception:
        pass


def _append_page(page, page_parts, path, stem, attachment_dir):
    """Save a page's images and append its Markdown. Returns the media dir."""
    page_md = page.get("markdown") or ""
    for img in page.get("images") or []:
        raw_id = img.get("id")
        img_b64 = img.get("image_base64")
        if not raw_id or not img_b64:
            continue

        # Never let an API-provided id escape the attachments directory.
        img_id = os.path.basename(str(raw_id)).strip()
        if not img_id or img_id in (".", ".."):
            continue

        try:
            img_bytes = base64.b64decode(img_b64)
        except Exception as e:
            print("    ERROR: could not decode image %s from %s: %s" % (img_id, path, e))
            continue
        if not img_bytes:
            continue

        if attachment_dir is None:
            attachment_dir = _attachment_dir_for(path, stem)
        if not os.path.exists(attachment_dir):
            os.makedirs(attachment_dir)

        img_path = os.path.join(attachment_dir, img_id)
        try:
            with open(img_path, "wb") as img_file:
                img_file.write(img_bytes)
        except Exception as e:
            print("    ERROR: could not save image %s for %s: %s" % (img_id, path, e))
            continue

        # Rewrite only the exact ``(<id>)`` reference, using POSIX
        # separators in the Markdown.
        rel = "attachments/" + stem + "/" + img_id
        page_md = page_md.replace("(" + str(raw_id) + ")", "(" + rel + ")")

    if page_md.strip():
        page_parts.append("<!-- page %d -->\n\n%s" % (len(page_parts) + 1, page_md.strip()))
    return attachment_dir


def _document_payload(path, encoded):
    """Build the ``document`` object for the OCR request body."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return {
            "type": "document_url",
            "document_url": "data:application/pdf;base64," + encoded,
        }
    mime = _IMAGE_MIME_TYPES.get(ext, "application/octet-stream")
    return {
        "type": "image_url",
        "image_url": "data:%s;base64,%s" % (mime, encoded),
    }


def _stem_for(path):
    """Sanitized filename stem used for the .md and attachments directory."""
    stem = makeValidFilename(os.path.splitext(os.path.basename(path))[0])
    return stem or "file"


def _md_path_for(path):
    # Keep the original stem so the markdown stays a discoverable sibling of
    # the source (the NotebookLM uploader looks for <stem>.md next to it).
    return os.path.splitext(path)[0] + ".md"


def _attachment_dir_for(path, stem):
    return os.path.join(os.path.dirname(path), "attachments", stem)


def _sync_mtime(source_path, output_path):
    """Give the generated Markdown the source file's modification time.

    Course recency (NotebookLM) is derived from file mtimes, so an OCR result
    must not look newer than the document it came from.
    """
    try:
        mtime = os.path.getmtime(source_path)
        os.utime(output_path, (mtime, mtime))
    except OSError:
        pass


def ocr_file(path, api_key, model=DEFAULT_OCR_MODEL,
             timeout=DEFAULT_OCR_TIMEOUT, force=False, verbose=False,
             min_interval=None, pages_per_request=None):
    """OCR one pdf/image into ``<stem>.md`` next to the source.

    PDFs are uploaded once to the Mistral Files API and then requested page
    range by page range, so the one-page-per-second throttle waits during the
    document instead of after one huge request (and the file is not re-uploaded
    per chunk). Images are sent inline. Returns the md path written, or None
    (unsupported / skipped / failed). Never raises.
    """
    file_id = None
    key = str(api_key).strip() if api_key else ""
    try:
        if not key:
            print("    Note: No Mistral API key configured; skipping OCR.")
            return None

        ext = os.path.splitext(path)[1].lower()
        if ext not in OCR_EXTENSIONS:
            return None
        if not os.path.isfile(path):
            print("    Skipping: %s (not a file or missing)" % path)
            return None

        stem = _stem_for(path)
        md_path = _md_path_for(path)

        if os.path.exists(md_path) and not force:
            if verbose:
                print("      ✓ Already exists: %s" % md_path)
            return None

        interval = _resolve_interval(min_interval)
        chunk_pages = _resolve_pages_per_request(pages_per_request)
        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }

        if ext == ".pdf":
            file_id = _upload_ocr_file(path, key, timeout, verbose)
        chunked = bool(file_id)
        if file_id:
            document = {"type": "file", "file_id": file_id}
        else:
            document = _inline_document(path)
            if document is None:
                print("    Note: %s is larger than Mistral's ~50 MB inline limit; skipping." % path)
                return None

        total_pages = _pdf_page_count(path) if chunked else None
        page_parts = []
        attachment_dir = None
        start = 0
        tried_inline = False
        while True:
            payload = {
                "model": model,
                "document": document,
                "include_image_base64": True,
            }
            if chunked:
                if total_pages is not None and start >= total_pages:
                    break
                if chunk_pages == 1:
                    payload["pages"] = str(start)
                else:
                    payload["pages"] = "%d-%d" % (start, start + chunk_pages - 1)

            try:
                response = _post_ocr(payload, headers, timeout, interval, verbose)
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if chunked and start > 0 and status in (400, 404, 416, 422):
                    # Asked for pages past the end of the document: keep what
                    # has been extracted so far.
                    break
                if chunked and not tried_inline and status == 422:
                    # The Files API route rejected this document; retry once
                    # inline as a single request.
                    inline = _inline_document(path)
                    if inline is not None:
                        tried_inline = True
                        chunked = False
                        document = inline
                        if verbose:
                            print("      File-based OCR rejected; retrying inline: %s" % path)
                        continue
                raise
            data = response.json()
            pages = data.get("pages") or []
            _note_pages(len(pages), interval)

            for page in pages:
                attachment_dir = _append_page(page, page_parts, path, stem, attachment_dir)

            if not chunked or not pages:
                break
            indexes = []
            for offset, page in enumerate(pages):
                try:
                    indexes.append(int(page.get("index", start + offset)))
                except (TypeError, ValueError):
                    indexes.append(start + offset)
            if len(pages) < chunk_pages or max(indexes) < start:
                break
            start = max(indexes) + 1
            if start >= MAX_OCR_PAGES:
                break

        full_md = "\n\n---\n\n".join(page_parts)
        if not full_md.strip():
            print("    Note: OCR returned no text for %s; not writing a markdown file." % path)
            return None

        # Write atomically so a failed run never leaves a partial .md behind.
        tmp_path = md_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as out:
                out.write(full_md)
            os.replace(tmp_path, md_path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

        print("      ✓ OCR saved: %s" % md_path)
        _sync_mtime(path, md_path)
        return md_path
    except Exception as e:
        detail = _error_detail(e)
        print("    ERROR: OCR failed for %s: %s%s"
              % (path, e, (" [%s]" % detail) if detail else ""))
        if verbose:
            import traceback
            traceback.print_exc()
        return None
    finally:
        if file_id:
            _delete_ocr_file(file_id, key, timeout)


def ocr_tree(root, api_key, model=DEFAULT_OCR_MODEL,
             timeout=DEFAULT_OCR_TIMEOUT, force=False, verbose=False,
             min_interval=None, pages_per_request=None):
    """Walk root and OCR every OCR_EXTENSIONS file without a sibling ``.md``.

    Returns the list of md paths written. Never raises.
    """
    written = []
    if not ocr_configured(api_key):
        print("    Note: No Mistral API key configured; skipping OCR.")
        return written

    for dirpath, dirnames, filenames in os.walk(root):
        # Extracted media lives under attachments/<stem>/ and is referenced
        # from its parent .md; do not recurse into it and re-OCR it.
        dirnames[:] = sorted(d for d in dirnames if d != "attachments")
        for filename in sorted(filenames):
            ext = os.path.splitext(filename)[1].lower()
            if ext not in OCR_EXTENSIONS:
                continue
            path = os.path.join(dirpath, filename)
            if os.path.exists(_md_path_for(path)) and not force:
                continue
            result = ocr_file(path, api_key, model=model, timeout=timeout,
                              force=force, verbose=verbose,
                              min_interval=min_interval,
                              pages_per_request=pages_per_request)
            if result:
                written.append(result)
    return written
