"""Generate HTML pages from data already fetched with the Canvas API.

The output mirrors the JSON export layout, one self-contained HTML file per
Canvas page. Canvas-provided rich text (page bodies, assignment descriptions,
announcements, discussion posts) is embedded as-is; links inside it still
point back to Canvas.
"""

import html
import os
import re
import urllib.parse

from bs4 import BeautifulSoup

from naming import MAX_FOLDER_NAME_SIZE, makeValidFilename, shortenFileName

# Canvas shows roughly 50 discussion/announcement entries per page.
ENTRIES_PER_PAGE = 50

# Embedded in every page we generate so downstream tooling (markdown
# conversion, NotebookLM uploads) can recognise files written by this exporter.
GENERATOR_META = '<meta name="generator" content="canvas-student-data-export">'

STYLESHEET = """
:root { color-scheme: light dark; }
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       line-height: 1.5; margin: 0 auto; padding: 1.5rem; max-width: 60rem; }
h1 { border-bottom: 1px solid rgba(128,128,128,.35); padding-bottom: .4rem; }
h2 { margin-top: 2rem; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid rgba(128,128,128,.35); padding: .4rem .6rem;
         text-align: left; vertical-align: top; }
th { background: rgba(128,128,128,.12); }
.meta { color: rgba(128,128,128,.9); font-size: .9em; margin: .2rem 0 .8rem; }
.comment { background: rgba(128,128,128,.12); border-radius: .4rem;
           padding: .6rem .9rem; margin: .6rem 0; white-space: pre-wrap; }
.reply { margin-left: 1.5rem; border-left: 3px solid rgba(128,128,128,.35);
         padding-left: .8rem; }
ul.items { list-style: none; padding-left: 0; }
ul.items > li { padding: .3rem 0; }
"""


def _page(title, body, back_link=None):
    back = f'<p><a href="{back_link}">&larr; Back</a></p>\n' if back_link else ""
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{GENERATOR_META}\n"
        f"<title>{html.escape(title)}</title>\n<style>{STYLESHEET}</style>\n"
        f"</head>\n<body>\n<main>\n{back}<h1>{html.escape(title)}</h1>\n{body}\n</main>\n</body>\n</html>\n"
    )


def _write(path, title, body, back_link=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_page(title, body, back_link))


def _rel_link(from_path, to_path):
    """Return a relative, URL-encoded link between two generated pages."""
    rel = os.path.relpath(to_path, os.path.dirname(from_path)).replace(os.sep, "/")
    return urllib.parse.quote(rel)


def _folder_name(value):
    """Sanitize a Canvas title the same way the file/folder exporters do."""
    name = makeValidFilename(str(value)) if value else ""
    name = name or "untitled"
    return shortenFileName(name, len(name) - MAX_FOLDER_NAME_SIZE)


def _safe_name(value):
    name = makeValidFilename(str(value)) if value else ""
    return name or "untitled"


def _unique_path(path, used_paths):
    """Return a path not present in ``used_paths`` (adds " (2)", " (3)", ...).

    Distinct Canvas titles can sanitize to the same filename; without this the
    second page would silently overwrite the first one.
    """
    if path not in used_paths:
        used_paths.add(path)
        return path
    root, ext = os.path.splitext(path)
    counter = 2
    while True:
        candidate = f"{root} ({counter}){ext}"
        if candidate not in used_paths:
            used_paths.add(candidate)
            return candidate
        counter += 1


def _unique_folder(path, used_folders):
    """Like ``_unique_path`` but for page folders (adds " (2)", ...)."""
    if path not in used_folders:
        used_folders.add(path)
        return path
    counter = 2
    while True:
        candidate = f"{path} ({counter})"
        if candidate not in used_folders:
            used_folders.add(candidate)
            return candidate
        counter += 1


def _escape(value):
    return html.escape(str(value)) if value else ""


def _anchor(url, label):
    if not url:
        return _escape(label)
    return f'<a href="{html.escape(str(url), quote=True)}">{_escape(label)}</a>'


def _rich_text_html(value, empty_message):
    if not value or str(value) == "None":
        return f"<p><em>{html.escape(empty_message)}</em></p>"
    return str(value)


_MEDIA_TAG_RE = re.compile(
    r"<\s*(img|video|audio|iframe|svg|embed|object|figure)\b", re.IGNORECASE
)


def _text_content(value):
    """Return the visible text of a Canvas rich text value ("" when blank)."""
    if value is None:
        return ""
    text = str(value)
    if text.strip().lower() in ("", "none", "null"):
        return ""
    try:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).replace("\u00a0", " ").strip()


def _has_text(value):
    """True when a Canvas rich text value holds actual information.

    Canvas happily returns empty paragraphs, bare metadata and literal "None"
    bodies; pages that would only repeat that boilerplate are not worth saving.
    Non-text content (images, embedded video) also counts as information: an
    assignment that is only a diagram should not be skipped.
    """
    if _text_content(value):
        return True
    if value is None:
        return False
    return bool(_MEDIA_TAG_RE.search(str(value)))


def _doc_link(from_path, to_path, label):
    """Link to a generated page, or render plain text when it was not written."""
    if to_path:
        return f'<a href="{_rel_link(from_path, to_path)}">{_escape(label)}</a>'
    return _escape(label)


def _meta_line(*parts):
    values = [str(part) for part in parts if part]
    if not values:
        return ""
    return '<p class="meta">' + " &middot; ".join(_escape(v) for v in values) + "</p>"


def _user_submission(assignment_view, user_id):
    target = str(user_id)
    for submission in assignment_view.submissions:
        if str(submission.user_id) == target:
            return submission
    return assignment_view.submissions[0] if assignment_view.submissions else None


def _comments_html(sub_view):
    comments = getattr(sub_view, "submission_comments_raw", None)
    if isinstance(comments, list) and comments:
        blocks = []
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            blocks.append(
                _meta_line(comment.get("author_name", ""), comment.get("created_at", ""))
                + f'<div class="comment">{html.escape(str(comment.get("comment", "")))}</div>'
            )
        if blocks:
            return "<h3>Comments</h3>" + "".join(blocks)

    legacy = getattr(sub_view, "submission_comments", "")
    if legacy and legacy not in ("None", "[]", "{}"):
        return f'<h3>Comments</h3><div class="comment">{_escape(legacy)}</div>'
    return ""


def _attachments_html(sub_view, output_dir, page_path):
    if not sub_view.attachments:
        return ""
    rows = []
    for attachment in sub_view.attachments:
        local_path = getattr(attachment, "local_path", "")
        if local_path:
            url = _rel_link(page_path, os.path.join(output_dir, local_path))
            rows.append(f'<li><a href="{url}">{_escape(attachment.filename)}</a></li>')
        else:
            rows.append(f"<li>{_anchor(attachment.url, attachment.filename)}</li>")
    return "<h3>Attachments</h3><ul>" + "".join(rows) + "</ul>"


def _submission_has_content(sub_view):
    """True when a submission is worth a page of its own.

    Grades/scores alone are metadata, not content: a submission needs text,
    attachments or comments to justify a generated page.
    """
    if _has_text(getattr(sub_view, "body", "")):
        return True
    if getattr(sub_view, "attachments", None):
        return True
    comments = getattr(sub_view, "submission_comments_raw", None)
    if isinstance(comments, list) and any(
        isinstance(comment, dict) and _has_text(comment.get("comment", ""))
        for comment in comments
    ):
        return True
    legacy = getattr(sub_view, "submission_comments", "")
    if not legacy or str(legacy) in ("None", "[]", "{}"):
        return False
    return _has_text(legacy)


def _submission_html(sub_view, output_dir, page_path, attempt_links=()):
    parts = ["<h2>My Submission</h2>"]
    parts.append(
        _meta_line(
            f"Grade: {sub_view.grade}" if sub_view.grade else "",
            f"Score: {sub_view.raw_score} / {sub_view.total_possible_points}"
            if sub_view.raw_score != ""
            else "",
            f"Attempt: {sub_view.attempt}" if sub_view.attempt else "",
        )
    )

    if getattr(sub_view, "body", ""):
        parts.append("<h3>Submission text</h3>" + str(sub_view.body))

    attachments = _attachments_html(sub_view, output_dir, page_path)
    if attachments:
        parts.append(attachments)

    parts.append(_comments_html(sub_view))

    if attempt_links:
        links = "".join(f'<li><a href="{href}">Attempt {number}</a></li>' for number, href in attempt_links)
        parts.append("<h3>Attempt history</h3><ul>" + links + "</ul>")
    return "".join(parts)


def _write_attempt_pages(sub_view, submission_path, submission_title):
    """Write attempts/attempt_N.html files. Returns [(number, href)]."""
    history = getattr(sub_view, "submission_history", None)
    if not (isinstance(history, list) and history):
        return []

    attempts_dir = os.path.join(os.path.dirname(submission_path), "attempts")
    links = []
    for index, entry in enumerate(history):
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("attempt") or index + 1)
        except (TypeError, ValueError):
            number = index + 1

        attempt_path = os.path.join(attempts_dir, f"attempt_{number}.html")
        body = _meta_line(f"Attempt {number}", entry.get("submitted_at", ""))
        body += _rich_text_html(entry.get("body"), "No text submission for this attempt.")

        attachments = entry.get("attachments") or []
        rows = "".join(
            f"<li>{_anchor(a.get('url'), a.get('filename', 'attachment'))}</li>"
            for a in attachments
            if isinstance(a, dict)
        )
        if rows:
            body += "<h3>Attachments</h3><ul>" + rows + "</ul>"

        _write(
            attempt_path,
            f"Attempt {number} - {submission_title}",
            body,
            back_link=_rel_link(attempt_path, submission_path),
        )
        links.append((number, _rel_link(submission_path, attempt_path)))
    return links


def _entry_html(entry):
    replies = "".join(
        f'<div class="reply">{_meta_line(reply.author, reply.posted_date)}'
        f'{_rich_text_html(reply.body, "No content.")}</div>'
        for reply in entry.topic_replies
    )
    return (
        f'<div class="comment">{_meta_line(entry.author, entry.posted_date)}'
        f"{_rich_text_html(entry.body, 'No content.')}{replies}</div>"
    )


def _write_entry_pages(directory, base_name, page_title, header_html, entries, back_link):
    """Write paginated <base_name>_N.html files. Returns the page count."""
    if entries:
        chunks = [entries[i:i + ENTRIES_PER_PAGE] for i in range(0, len(entries), ENTRIES_PER_PAGE)]
    else:
        chunks = [[]]

    count = 0
    for index, chunk in enumerate(chunks):
        page_path = os.path.join(directory, f"{base_name}_{index + 1}.html")
        nav = []
        if index > 0:
            nav.append(f'<a href="{base_name}_{index}.html">&larr; Previous</a>')
        if index < len(chunks) - 1:
            nav.append(f'<a href="{base_name}_{index + 2}.html">Next &rarr;</a>')
        body = header_html + "".join(chunk)
        if nav:
            body += '<p class="meta">' + " &middot; ".join(nav) + "</p>"
        _write(page_path, page_title, body, back_link=back_link)
        count += 1
    return count


def _course_dir(output_dir, course_view):
    return os.path.join(output_dir, course_view.term, course_view.course_code)


def export_course_list_html(course_views, output_dir):
    """Write an index of all courses. Returns the number of pages written."""
    index_path = os.path.join(output_dir, "course_list.html")

    terms = {}
    for course_view in course_views:
        terms.setdefault(course_view.term or "No Term", []).append(course_view)

    sections = []
    for term in sorted(terms):
        rows = []
        for course_view in sorted(terms[term], key=lambda c: c.course_code):
            home = os.path.join(_course_dir(output_dir, course_view), "homepage.html")
            rows.append(
                f'<tr><td><a href="{_rel_link(index_path, home)}">'
                f"{_escape(course_view.course_code)}</a></td>"
                f"<td>{_escape(course_view.name)}</td></tr>"
            )
        sections.append(
            f"<h2>{_escape(term)}</h2>"
            '<table><tr><th>Course</th><th>Name</th></tr>' + "".join(rows) + "</table>"
        )

    _write(index_path, "Canvas Data Export", "".join(sections) or "<p><em>No courses.</em></p>")
    return 1


def _section_links(course_dir, home_path, course_view):
    candidates = [
        ("Grades", os.path.join(course_dir, "grades.html"), bool(course_view.assignments)),
        ("Assignments", os.path.join(course_dir, "assignments", "assignment_list.html"), bool(course_view.assignments)),
        ("Announcements", os.path.join(course_dir, "announcements", "announcement_list.html"), bool(course_view.announcements)),
        ("Discussions", os.path.join(course_dir, "discussions", "discussion_list.html"), bool(course_view.discussions)),
        ("Modules", os.path.join(course_dir, "modules", "modules_list.html"), bool(course_view.modules)),
    ]
    return "".join(
        f'<li><a href="{_rel_link(home_path, path)}">{label}</a></li>'
        for label, path, available in candidates
        if available
    )


def _thread_entries_has_content(entries):
    for entry in entries:
        if _has_text(getattr(entry, "body", "")):
            return True
        for reply in getattr(entry, "topic_replies", []) or []:
            if _has_text(getattr(reply, "body", "")):
                return True
    return False


def _thread_has_content(thread):
    """True when an announcement/discussion has a body or non-empty entries."""
    return _has_text(getattr(thread, "body", "")) or _thread_entries_has_content(
        thread.topic_entries
    )


def _module_item_has_page(item, context):
    """True when a module item page would contain something worth saving.

    Module items such as quizzes or external links would only render an
    "Open in Canvas" boilerplate page; those are linked from the module list
    instead of being written to disk.
    """
    content_type = item.content_type or ""
    if content_type == "Page":
        page = context["pages"].get(item.content_id)
        return page is not None and _has_text(page.body)
    if content_type == "Assignment":
        return item.content_id in context["assignment_paths"]
    if content_type == "Discussion":
        return item.content_id in context["discussion_paths"]
    if content_type == "File":
        return bool(getattr(item, "local_path", ""))
    return False


def export_course_html(course_view, output_dir, user_id):
    """Write every HTML page for one course. Returns the page count."""
    count = 0
    course_dir = _course_dir(output_dir, course_view)
    home_path = os.path.join(course_dir, "homepage.html")
    title = f"{course_view.course_code} - {course_view.name}".strip(" -")

    # Paths used by multiple sections (and by module item pages).
    used_paths = set()
    used_folders = set()
    assignment_paths = {}
    assignment_views = {}
    for assignment in course_view.assignments:
        folder = _unique_folder(
            os.path.join(course_dir, "assignments", _folder_name(assignment.title)),
            used_folders,
        )
        assignment_paths[assignment.id] = os.path.join(folder, "assignment.html")
        assignment_views[assignment.id] = assignment

    announcement_folders = {}
    announcement_paths = {}
    for announcement in course_view.announcements:
        folder = _unique_folder(
            os.path.join(course_dir, "announcements", _folder_name(announcement.title)),
            used_folders,
        )
        announcement_folders[announcement.id] = folder
        announcement_paths[announcement.id] = os.path.join(folder, "announcement_1.html")

    discussion_folders = {}
    discussion_paths = {}
    for discussion in course_view.discussions:
        folder = _unique_folder(
            os.path.join(course_dir, "discussions", _folder_name(discussion.title)),
            used_folders,
        )
        discussion_folders[discussion.id] = folder
        discussion_paths[discussion.id] = os.path.join(folder, "discussion_1.html")

    page_views = {page.id: page for page in course_view.pages}

    # Which pages hold actual information? Pages that would only contain a
    # title plus metadata (e.g. "Quiz 1 / Assigned: ... / No description.") are
    # not written at all, so links to them fall back to plain text below.
    assignment_page_paths = {}
    submission_page_paths = {}
    for assignment in course_view.assignments:
        if _has_text(assignment.description):
            assignment_page_paths[assignment.id] = assignment_paths[assignment.id]
        submission = _user_submission(assignment, user_id)
        if submission is not None and _submission_has_content(submission):
            submission_page_paths[assignment.id] = os.path.join(
                os.path.dirname(assignment_paths[assignment.id]), "submission.html"
            )

    announcement_page_paths = {
        announcement.id: announcement_paths[announcement.id]
        for announcement in course_view.announcements
        if _thread_has_content(announcement)
    }
    discussion_page_paths = {
        discussion.id: discussion_paths[discussion.id]
        for discussion in course_view.discussions
        if _thread_has_content(discussion)
    }

    # --- homepage -----------------------------------------------------------
    body = _rich_text_html(course_view.homepage_html, "No front page is available for this course.")
    section_links = _section_links(course_dir, home_path, course_view)
    if section_links:
        body += f"<h2>Contents</h2><ul>{section_links}</ul>"
    _write(home_path, title, body)
    count += 1

    # --- grades -------------------------------------------------------------
    if course_view.assignments:
        grades_path = os.path.join(course_dir, "grades.html")
        rows = []
        for assignment in course_view.assignments:
            submission = _user_submission(assignment, user_id)
            grade = "" if submission is None else submission.grade
            score = (
                f"{submission.raw_score} / {submission.total_possible_points}"
                if submission is not None and submission.raw_score != ""
                else ""
            )
            rows.append(
                f'<tr><td>{_doc_link(grades_path, assignment_page_paths.get(assignment.id), assignment.title)}</td>'
                f"<td>{_escape(assignment.due_date)}</td><td>{_escape(grade)}</td>"
                f"<td>{_escape(score)}</td></tr>"
            )
        _write(
            grades_path,
            f"Grades - {title}",
            '<table><tr><th>Assignment</th><th>Due</th><th>Grade</th><th>Score</th></tr>'
            + "".join(rows) + "</table>",
            back_link=_rel_link(grades_path, home_path),
        )
        count += 1

    # --- assignment list, details, submissions and attempts -----------------
    if course_view.assignments:
        assignment_list = os.path.join(course_dir, "assignments", "assignment_list.html")
        rows = []
        for assignment in course_view.assignments:
            rows.append(
                f'<tr><td>{_doc_link(assignment_list, assignment_page_paths.get(assignment.id), assignment.title)}</td>'
                f"<td>{_escape(assignment.due_date)}</td></tr>"
            )
        _write(
            assignment_list,
            f"Assignments - {title}",
            '<table><tr><th>Assignment</th><th>Due</th></tr>' + "".join(rows) + "</table>",
            back_link=_rel_link(assignment_list, home_path),
        )
        count += 1

    for assignment in course_view.assignments:
        assignment_path = assignment_page_paths.get(assignment.id)
        if assignment_path:
            body = _meta_line(f"Assigned: {assignment.assigned_date}", f"Due: {assignment.due_date}")
            body += _rich_text_html(assignment.description, "No description.")
            _write(
                assignment_path,
                assignment.title,
                body,
                back_link=_rel_link(assignment_path, os.path.join(course_dir, "assignments", "assignment_list.html")),
            )
            count += 1

        submission = _user_submission(assignment, user_id)
        submission_path = submission_page_paths.get(assignment.id)
        if submission is not None and submission_path:
            attempt_links = _write_attempt_pages(submission, submission_path, assignment.title)
            back_path = assignment_path or os.path.join(course_dir, "assignments", "assignment_list.html")
            _write(
                submission_path,
                f"Submission - {assignment.title}",
                _submission_html(submission, output_dir, submission_path, attempt_links),
                back_link=_rel_link(submission_path, back_path),
            )
            count += len(attempt_links) + 1

    # --- announcements ------------------------------------------------------
    if course_view.announcements:
        announcement_list = os.path.join(course_dir, "announcements", "announcement_list.html")
        rows = []
        for announcement in course_view.announcements:
            rows.append(
                f'<tr><td>{_doc_link(announcement_list, announcement_page_paths.get(announcement.id), announcement.title)}</td>'
                f"<td>{_escape(announcement.author)}</td>"
                f"<td>{_escape(announcement.posted_date)}</td></tr>"
            )
        _write(
            announcement_list,
            f"Announcements - {title}",
            '<table><tr><th>Announcement</th><th>Author</th><th>Posted</th></tr>'
            + "".join(rows) + "</table>",
            back_link=_rel_link(announcement_list, home_path),
        )
        count += 1

    for announcement in course_view.announcements:
        if announcement.id not in announcement_page_paths:
            continue
        folder = announcement_folders[announcement.id]
        header = _meta_line(announcement.author, announcement.posted_date)
        header += _rich_text_html(announcement.body, "No content.")
        count += _write_entry_pages(
            folder,
            "announcement",
            announcement.title,
            header,
            [_entry_html(entry) for entry in announcement.topic_entries],
            back_link=_rel_link(announcement_paths[announcement.id], os.path.join(course_dir, "announcements", "announcement_list.html")),
        )

    # --- discussions --------------------------------------------------------
    if course_view.discussions:
        discussion_list = os.path.join(course_dir, "discussions", "discussion_list.html")
        rows = []
        for discussion in course_view.discussions:
            rows.append(
                f'<tr><td>{_doc_link(discussion_list, discussion_page_paths.get(discussion.id), discussion.title)}</td>'
                f"<td>{_escape(discussion.author)}</td>"
                f"<td>{_escape(discussion.posted_date)}</td></tr>"
            )
        _write(
            discussion_list,
            f"Discussions - {title}",
            '<table><tr><th>Discussion</th><th>Author</th><th>Posted</th></tr>'
            + "".join(rows) + "</table>",
            back_link=_rel_link(discussion_list, home_path),
        )
        count += 1

    for discussion in course_view.discussions:
        if discussion.id not in discussion_page_paths:
            continue
        folder = discussion_folders[discussion.id]
        header = _meta_line(discussion.author, discussion.posted_date)
        header += _rich_text_html(discussion.body, "No content.")
        count += _write_entry_pages(
            folder,
            "discussion",
            discussion.title,
            header,
            [_entry_html(entry) for entry in discussion.topic_entries],
            back_link=_rel_link(discussion_paths[discussion.id], os.path.join(course_dir, "discussions", "discussion_list.html")),
        )

    # --- modules ------------------------------------------------------------
    if course_view.modules:
        modules_dir = os.path.join(course_dir, "modules")
        modules_list = os.path.join(modules_dir, "modules_list.html")
        context = {
            "pages": page_views,
            "assignments": assignment_views,
            "assignment_paths": assignment_page_paths,
            "discussion_paths": discussion_page_paths,
        }
        sections = []
        module_item_paths = {}
        for module in course_view.modules:
            module_folder = os.path.join(modules_dir, _folder_name(module.name))
            for item in module.items:
                if item.content_type == "SubHeader":
                    continue
                module_item_paths[id(item)] = _unique_path(
                    os.path.join(module_folder, _safe_name(item.title) + ".html"),
                    used_paths,
                )
        for module in course_view.modules:
            items = []
            for item in module.items:
                if item.content_type == "SubHeader":
                    items.append(f"<li><strong>{_escape(item.title)}</strong></li>")
                    continue
                item_path = module_item_paths[id(item)]
                meta = f' <span class="meta">({_escape(item.content_type or "link")})</span>'
                if _module_item_has_page(item, context):
                    items.append(
                        f'<li><a href="{_rel_link(modules_list, item_path)}">{_escape(item.title)}</a>{meta}</li>'
                    )
                else:
                    canvas_url = item.external_url or item.url
                    label = _anchor(canvas_url, item.title) if canvas_url else _escape(item.title)
                    items.append(f"<li>{label}{meta}</li>")
            sections.append(
                f"<h2>{_escape(module.name)}</h2>"
                '<ul class="items">' + "".join(items) + "</ul>"
            )
        _write(
            modules_list,
            f"Modules - {title}",
            "".join(sections),
            back_link=_rel_link(modules_list, home_path),
        )
        count += 1

        for module in course_view.modules:
            for item in module.items:
                if not _module_item_has_page(item, context):
                    continue
                item_path = module_item_paths[id(item)]
                _write(
                    item_path,
                    item.title,
                    _module_item_body(item, context, item_path, output_dir),
                    back_link=_rel_link(item_path, modules_list),
                )
                count += 1

    return count


def _module_item_link(item):
    url = item.external_url or item.url
    if url:
        return f'<p><a href="{html.escape(str(url), quote=True)}">Open in Canvas</a></p>'
    return "<p><em>No content is available for this item.</em></p>"


def _module_item_body(item, context, item_path, output_dir):
    content_type = item.content_type or ""
    if content_type == "Page":
        page = context["pages"].get(item.content_id)
        if page:
            return _rich_text_html(page.body, "No content.")
        return "<p><em>Page content is not available.</em></p>"

    if content_type == "Assignment":
        assignment = context["assignments"].get(item.content_id)
        if assignment:
            body = _meta_line(f"Due: {assignment.due_date}")
            body += _rich_text_html(assignment.description, "No description.")
            if item.content_id in context["assignment_paths"]:
                link = _rel_link(item_path, context["assignment_paths"][item.content_id])
                body += f'<p><a href="{link}">Open the full assignment page</a></p>'
            return body
        return _module_item_link(item)

    if content_type == "Discussion":
        if item.content_id in context["discussion_paths"]:
            link = _rel_link(item_path, context["discussion_paths"][item.content_id])
            return f'<p><a href="{link}">Open the discussion</a></p>'
        return _module_item_link(item)

    if content_type == "File" and getattr(item, "local_path", ""):
        link = _rel_link(item_path, os.path.join(output_dir, item.local_path))
        return f'<p><a href="{link}">Download the file</a></p>'

    return _module_item_link(item)
