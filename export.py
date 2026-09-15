# built in
import functools
import json
import math
import os
import argparse
import sys

# external
from canvasapi import Canvas
from canvasapi.exceptions import ResourceDoesNotExist, Unauthorized, Forbidden, InvalidAccessToken, CanvasException
import dateutil.parser
import jsonpickle
import requests
import yaml

# local
import markdown_export
import mistral_ocr
import notebooklm_upload
from html_export import export_course_html, export_course_list_html
from naming import MAX_FOLDER_NAME_SIZE, makeValidFilename, makeValidFolderPath, shortenFileName

# Default network timeout (seconds) for Canvas API and file requests.
# requests has no timeout by default, so a stalled connection (e.g. after a
# network drop) can block an export run forever.
DEFAULT_HTTP_TIMEOUT = 60.0

# Canvas API Error Handling Utility
class CanvasErrorHandler:
    @staticmethod
    def handle_canvas_exception(e, operation_description="operation"):
        """
        Handle Canvas API exceptions with appropriate messaging and classification.
        Returns (error_type, message)
        """
        if isinstance(e, InvalidAccessToken):
            return "authentication", f"Invalid Canvas API token. Please check your credentials.yaml file."
        
        elif isinstance(e, Unauthorized):
            # Check if this is a known student limitation
            if "class submission" in operation_description.lower():
                return "student_limitation", f"Not authorized to download every student's assignment submission. This is normal for student accounts."
            elif "file" in operation_description.lower():
                return "student_limitation", f"Not authorized to download some course files. This is normal for student accounts."
            else:
                return "authorization", f"Not authorized to perform {operation_description}. Check your Canvas permissions."
        
        elif isinstance(e, Forbidden):
            return "student_limitation", f"Access forbidden for {operation_description}. This may be normal for student accounts."
        
        elif isinstance(e, ResourceDoesNotExist):
            return "not_found", f"Resource not found for {operation_description}. It may have been deleted or moved."
        
        elif isinstance(e, CanvasException):
            return "canvas_error", f"Canvas API error during {operation_description}: {str(e)}"
        
        else:
            return "unknown_error", f"Unexpected error during {operation_description}: {str(e)}"
    
    @staticmethod
    def log_error(error_type, message, show_details=True, verbose=False):
        """Log error messages with appropriate formatting"""
        if error_type == "student_limitation":
            if show_details:
                print(f"    Note: {message}")
        elif error_type == "not_found":
            print(f"    Skipping: {message}")
        elif error_type in ["authentication", "authorization", "canvas_error", "unknown_error"]:
            print(f"    ERROR: {message}")
            if verbose:
                import traceback
                traceback.print_exc()
        else:
            print(f"    {message}")
            
    @staticmethod
    def is_fatal_error(error_type):
        """Check if an error type should stop execution"""
        return error_type in ["authentication", "canvas_error", "authorization"]

# Add counters for tracking successful extractions
class ExtractionStats:
    def __init__(self):
        self.assignments_found = 0
        self.submissions_found = 0
        self.announcements_found = 0
        self.discussions_found = 0
        self.pages_found = 0
        self.modules_found = 0
        self.module_items_found = 0
        self.files_downloaded = 0
        self.attachments_downloaded = 0
        self.html_pages_generated = 0
        self.markdown_files_created = 0
        self.ocr_files_created = 0
        self.notebooklm_notebooks = 0
        self.notebooklm_sources_uploaded = 0
        self.json_files_created = 0
        self.student_limitation_warnings = 0
        self.error_count = 0
        
    def summary(self, dl_location, html_enabled=False):
        summary_text = f"""
Data Extraction Summary:
  • {self.assignments_found} assignments found
  • {self.submissions_found} submissions found (your own)
  • {self.announcements_found} announcements found
  • {self.discussions_found} discussions found
  • {self.pages_found} pages found
  • {self.modules_found} modules found
  • {self.module_items_found} module items found

Files Downloaded:
  • {self.files_downloaded} course files downloaded
  • {self.attachments_downloaded} assignment attachments downloaded"""

        if html_enabled:
            summary_text += f"\n  • {self.html_pages_generated} HTML pages generated"

        if self.markdown_files_created or self.ocr_files_created:
            summary_text += f"""

Markdown Conversion:
  • {self.markdown_files_created} files converted to Markdown
  • {self.ocr_files_created} images/PDFs OCR'd to Markdown"""

        if self.notebooklm_notebooks or self.notebooklm_sources_uploaded:
            summary_text += f"""

NotebookLM Uploads:
  • {self.notebooklm_notebooks} notebooks used
  • {self.notebooklm_sources_uploaded} sources uploaded"""

        summary_text += f"""

Data Exports Created:
  • {self.json_files_created} JSON data files created
  • Individual course data: {dl_location}/[Term]/[Course]/[Course].json
  • Combined data: {dl_location}/all_output.json

Student Account Limitations: {self.student_limitation_warnings} (expected)
Errors Encountered: {self.error_count}
"""
        return summary_text

# Global stats tracker
extraction_stats = ExtractionStats()

def _load_credentials(path: str) -> dict:
    """Return a dict with API_URL, API_KEY, USER_ID or empty dict if file missing."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.full_load(f) or {}
    except FileNotFoundError:
        return {}


# Optional environment variables for environments where mounting a YAML file is
# inconvenient (e.g. Docker/CI). Values from the environment override the file.
ENV_CREDENTIAL_KEYS = {
    "API_URL": "CANVAS_API_URL",
    "API_KEY": "CANVAS_API_KEY",
    "USER_ID": "CANVAS_USER_ID",
    "HTTP_TIMEOUT": "CANVAS_HTTP_TIMEOUT",
}


def _apply_env_overrides(creds: dict) -> dict:
    """Overlay credentials loaded from YAML with CANVAS_* environment variables."""
    for key, env_name in ENV_CREDENTIAL_KEYS.items():
        value = os.environ.get(env_name)
        if value:
            creds[key] = value.strip()
    return creds

# Placeholder globals – will be overwritten in __main__ once we have parsed CLI args.
API_URL = ""
API_KEY = ""
USER_ID = 0

# Directory in which to download course information to (will be created if not
# present)
DL_LOCATION = "./output"
# List of Course IDs that should be skipped
COURSES_TO_SKIP = []

DATE_TEMPLATE = "%B %d, %Y %I:%M %p"

# Max PATH length is 260 characters on Windows. See naming.py for the shared
# filename helpers used by the JSON and HTML exporters.


class moduleItemView():
    id = 0
    
    title = ""
    content_type = ""
    content_id = 0
    
    url = ""
    external_url = ""
    local_path = ""


class moduleView():
    id = 0

    name = ""
    items = []

    def __init__(self):
        self.items = []


class pageView():
    id = 0

    title = ""
    body = ""
    created_date = ""
    last_updated_date = ""
    url = ""


class topicReplyView():
    id = 0

    author = ""
    posted_date = ""
    body = ""


class topicEntryView():
    id = 0

    author = ""
    posted_date = ""
    body = ""
    topic_replies = []

    def __init__(self):
        self.topic_replies = []


class discussionView():
    id = 0

    title = ""
    author = ""
    posted_date = ""
    body = ""
    topic_entries = []

    url = ""
    amount_pages = 0

    def __init__(self):
        self.topic_entries = []


class submissionView():
    id = 0

    attachments = []
    grade = ""
    raw_score = ""
    submission_comments = ""
    submission_comments_raw = None
    submission_history = None
    body = ""
    total_possible_points = ""
    attempt = 0
    user_id = "no-id"

    preview_url = ""
    ext_url = ""

    def __init__(self):
        self.attachments = []

class attachmentView():
    id = 0

    filename = ""
    url = ""
    local_path = ""

class assignmentView():
    id = 0

    title = ""
    description = ""
    assigned_date = ""
    due_date = ""
    submissions = []

    html_url = ""
    ext_url = ""
    updated_url = ""
    
    def __init__(self):
        self.submissions = []


class courseView():
    course_id = 0
    
    term = ""
    course_code = ""
    name = ""
    assignments = []
    announcements = []
    discussions = []
    modules = []
    homepage_html = ""

    def __init__(self):
        self.assignments = []
        self.announcements = []
        self.discussions = []
        self.modules = []


def findCourseModules(course, course_view):
    modules_dir = os.path.join(DL_LOCATION, course_view.term,
                               course_view.course_code, "modules")

    # Create modules directory if not present
    if not os.path.exists(modules_dir):
        os.makedirs(modules_dir)

    module_views = []

    try:
        modules = course.get_modules()
        modules_list = list(modules)  # Convert to list to get count
        
        if not modules_list:
            print("    No modules found in this course")
        else:
            print(f"    Found {len(modules_list)} modules")

        for module in modules_list:
            module_view = moduleView()

            # ID
            module_view.id = module.id if hasattr(module, "id") else 0

            # Name
            module_view.name = str(module.name) if hasattr(module, "name") else ""
            print(f"      Processing module: {module_view.name}")

            try:
                # Get module items
                module_items = module.get_module_items()
                module_items_list = list(module_items)
                
                if module_items_list:
                    print(f"        Found {len(module_items_list)} items")
                
                for module_item in module_items_list:
                    module_item_view = moduleItemView()

                    # ID
                    module_item_view.id = module_item.id if hasattr(module_item, "id") else 0

                    # Title
                    module_item_view.title = str(module_item.title) if hasattr(module_item, "title") else ""
                    # Type
                    module_item_view.content_type = str(module_item.type) if hasattr(module_item, "type") else ""
                    # ID of the referenced object (page, assignment, ...)
                    module_item_view.content_id = module_item.content_id if hasattr(module_item, "content_id") else 0

                    # URL
                    module_item_view.url = str(module_item.html_url) if hasattr(module_item, "html_url") else ""
                    # External URL
                    module_item_view.external_url = str(module_item.external_url) if hasattr(module_item, "external_url") else ""

                    if module_item_view.content_type == "File":
                        # If problems arise due to long pathnames, changing module.name to module.id might help
                        module_name = makeValidFilename(str(module.name))
                        module_name = shortenFileName(module_name, len(module_name) - MAX_FOLDER_NAME_SIZE)
                        module_dir = os.path.join(modules_dir, module_name, "files")

                        try:
                            # Create directory for current module if not present
                            if not os.path.exists(module_dir):
                                os.makedirs(module_dir)

                            # Get the file object
                            module_file = course.get_file(str(module_item.content_id))

                            # Create path for module file download
                            module_file_path = os.path.join(module_dir, makeValidFilename(str(module_file.display_name)))

                            # Download file if it doesn't already exist
                            if not os.path.exists(module_file_path):
                                module_file.download(module_file_path)
                                _preserve_mtime(module_file_path, module_file)
                                extraction_stats.files_downloaded += 1
                                print(f"        Downloaded: {module_file.display_name}")
                            else:
                                print(f"        File already exists: {module_file.display_name}")

                            module_item_view.local_path = os.path.relpath(module_file_path, DL_LOCATION)
                        except Exception as e:
                            error_type, message = CanvasErrorHandler.handle_canvas_exception(
                                e, "module file download"
                            )
                            if error_type == "student_limitation":
                                extraction_stats.student_limitation_warnings += 1
                            elif error_type == "not_found":
                                pass  # Already handled by log_error
                            else:
                                extraction_stats.error_count += 1
                            CanvasErrorHandler.log_error(error_type, message)

                    module_view.items.append(module_item_view)
                    extraction_stats.module_items_found += 1
            except Exception as e:
                error_type, message = CanvasErrorHandler.handle_canvas_exception(
                    e, "module item processing"
                )
                CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                extraction_stats.error_count += 1

            module_views.append(module_view)
            extraction_stats.modules_found += 1

    except Exception as e:
        error_type, message = CanvasErrorHandler.handle_canvas_exception(
            e, "module processing"
        )
        CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
        extraction_stats.error_count += 1

    return module_views


def downloadCourseFiles(course, course_view):
    # file full_name starts with "course files"
    dl_dir = os.path.join(DL_LOCATION, course_view.term,
                          course_view.course_code)

    # Create directory if not present
    if not os.path.exists(dl_dir):
        os.makedirs(dl_dir)

    try:
        files = course.get_files()
        files_list = list(files)  # Convert to list for consistency and count

        # Files usually share a handful of folders; cache the lookups.
        folder_cache = {}

        for file in files_list:
            file_folder = folder_cache.get(file.folder_id)
            if file_folder is None:
                try:
                    file_folder = course.get_folder(file.folder_id)
                except Exception as e:
                    error_type, message = CanvasErrorHandler.handle_canvas_exception(
                        e, f"folder lookup for {file.display_name}"
                    )
                    CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                    extraction_stats.error_count += 1
                    continue
                folder_cache[file.folder_id] = file_folder
            
            folder_dl_dir=os.path.join(dl_dir, makeValidFolderPath(file_folder.full_name))
            
            if not os.path.exists(folder_dl_dir):
                os.makedirs(folder_dl_dir)
        
            dl_path = os.path.join(folder_dl_dir, makeValidFilename(str(file.display_name)))
            
            print(f"    Downloading: {file.display_name}...")
            if not os.path.exists(dl_path):
                try:
                    file.download(dl_path)
                    _preserve_mtime(dl_path, file)
                    extraction_stats.files_downloaded += 1
                    print(f"      ✓ Saved: {file.display_name}")
                except Exception as e:
                    error_type, message = CanvasErrorHandler.handle_canvas_exception(e, f"file download for {file.display_name}")
                    CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                    extraction_stats.error_count += 1
            else:
                print(f"      ✓ Already exists: {file.display_name}")

    except Exception as e:
        error_type, message = CanvasErrorHandler.handle_canvas_exception(
            e, "course file download"
        )
        if error_type == "student_limitation":
            extraction_stats.student_limitation_warnings += 1
        else:
            extraction_stats.error_count += 1
        CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)


def download_submission_attachments(course, course_view):
    course_dir = os.path.join(DL_LOCATION, course_view.term,
                              course_view.course_code)

    # Create directory if not present
    if not os.path.exists(course_dir):
        os.makedirs(course_dir)

    for assignment in course_view.assignments:
        for submission in assignment.submissions:
            assignment_title = makeValidFilename(str(assignment.title))
            assignment_title = shortenFileName(assignment_title, len(assignment_title) - MAX_FOLDER_NAME_SIZE)
            attachment_dir = os.path.join(course_dir, "assignments", assignment_title)
            if(len(assignment.submissions)!=1):
                attachment_dir = os.path.join(attachment_dir,str(submission.user_id))
            if (not os.path.exists(attachment_dir)) and (submission.attachments):
                os.makedirs(attachment_dir)
            for attachment in submission.attachments:
                filepath = os.path.join(attachment_dir, makeValidFilename(str(attachment.id) +
                                        "_" + attachment.filename))
                
                print(f"    Downloading attachment: {attachment.filename}...")
                if not os.path.exists(filepath):
                    try:
                        r = requests.get(attachment.url, allow_redirects=True)
                        r.raise_for_status()
                        with open(filepath, 'wb') as f:
                            f.write(r.content)
                        _preserve_mtime(filepath, attachment)
                        attachment.local_path = os.path.relpath(filepath, DL_LOCATION)
                        extraction_stats.attachments_downloaded += 1
                        print(f"      ✓ Saved: {attachment.filename}")
                    except Exception as e:
                        print(f"      ❌ Failed to download {attachment.filename}: {e}")
                        extraction_stats.error_count += 1
                else:
                    attachment.local_path = os.path.relpath(filepath, DL_LOCATION)
                    print(f"      ✓ Already exists: {attachment.filename}")


def _page_view_from(page):
    """Build a pageView from a canvasapi Page object."""
    page_view = pageView()

    # ID
    page_view.id = page.id if hasattr(page, "id") else 0

    # Title
    page_view.title = str(page.title) if hasattr(page, "title") else ""
    # Body
    page_view.body = str(page.body) if hasattr(page, "body") else ""
    # URL
    page_view.url = str(page.html_url) if hasattr(page, "html_url") else ""
    # Date created
    try:
        page_view.created_date = dateutil.parser.parse(page.created_at).strftime(DATE_TEMPLATE) if \
            hasattr(page, "created_at") else ""
    except (ValueError, TypeError):
        page_view.created_date = ""

    # Date last updated
    try:
        page_view.last_updated_date = dateutil.parser.parse(page.updated_at).strftime(DATE_TEMPLATE) if \
            hasattr(page, "updated_at") else ""
    except (ValueError, TypeError):
        page_view.last_updated_date = ""

    return page_view


def findCoursePages(course):
    page_views = []

    # The list endpoint is paginated and can return page bodies in one request;
    # ask for them to avoid a detail request per page. PaginatedList fetches
    # lazily, so listing errors (e.g. a 404 for courses without pages) can also
    # surface while iterating, not only when the list is created.
    try:
        pages = course.get_pages(include=["body"])

        for listed_page in pages:
            # One failing page must not stop the remaining pages from exporting.
            try:
                page = listed_page
                # Some page types (e.g. block-editor pages) only expose their body
                # through the detail endpoint, so fall back to a detail request.
                if not getattr(listed_page, "body", None) and hasattr(listed_page, "url"):
                    page = course.get_page(listed_page.url)

                page_views.append(_page_view_from(page))
                extraction_stats.pages_found += 1
            except Exception as e:
                error_type, message = CanvasErrorHandler.handle_canvas_exception(
                    e, "page download"
                )
                CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                extraction_stats.error_count += 1
    except Exception as e:
        # Keep the pages found so far when the listing itself fails.
        error_msg = str(e)
        if "Not Found" not in error_msg:
            error_type, message = CanvasErrorHandler.handle_canvas_exception(
                e, "page URL retrieval"
            )
            CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
            if error_type != "student_limitation":
                extraction_stats.error_count += 1
            else:
                extraction_stats.student_limitation_warnings += 1

    return page_views


def findCourseAssignments(course):
    assignment_views = []

    # Get all assignments
    assignments = course.get_assignments()
    assignments_list = list(assignments)  # Convert to list for consistency

    # Fetch the user's own submissions for the whole course in one request.
    # Canvas only allows students to list their own submissions, so this
    # replaces one fallback request per assignment. If the endpoint is not
    # available the per-assignment fallback below is used instead.
    own_submissions = None
    if assignments_list:
        try:
            own_submissions = {
                submission.assignment_id: submission
                for submission in course.get_multiple_submissions(
                    include=["submission_history", "submission_comments"]
                )
            }
        except Exception:
            own_submissions = None

    class_submissions_forbidden = False
    
    try:
        for assignment in assignments_list:
            # Create a new assignment view
            assignment_view = assignmentView()

            #ID
            assignment_view.id = assignment.id if \
                hasattr(assignment, "id") else 0

            # Title
            assignment_view.title = makeValidFilename(str(assignment.name)) if \
                hasattr(assignment, "name") else ""
            # Description
            assignment_view.description = str(assignment.description) if \
                hasattr(assignment, "description") else ""
            
            # Assigned date
            try:
                assignment_view.assigned_date = dateutil.parser.parse(assignment.created_at).strftime(DATE_TEMPLATE) if \
                    hasattr(assignment, "created_at") and assignment.created_at else ""
            except (ValueError, TypeError):
                assignment_view.assigned_date = ""
            
            # Due date
            try:
                assignment_view.due_date = dateutil.parser.parse(assignment.due_at).strftime(DATE_TEMPLATE) if \
                    hasattr(assignment, "due_at") and assignment.due_at else ""
            except (ValueError, TypeError):
                assignment_view.due_date = ""

            # HTML Url
            assignment_view.html_url = assignment.html_url if \
                hasattr(assignment, "html_url") else ""   
            # External URL
            assignment_view.ext_url = str(assignment.url) if \
                hasattr(assignment, "url") else ""
            # Other URL (more up-to-date)
            assignment_view.updated_url = str(assignment.submissions_download_url).split("submissions?")[0] if \
                hasattr(assignment, "submissions_download_url") else ""

            submissions = None
            try: # Download all submissions for entire class
                if not class_submissions_forbidden:
                    class_submissions = assignment.get_submissions(include=["submission_history", "submission_comments"])
                    class_submissions[0] # Trigger Unauthorized if not allowed
                    submissions = class_submissions
            except (Unauthorized, Forbidden) as e:
                # Canvas only allows students to list their own submissions;
                # remember this and do not probe again for this course.
                class_submissions_forbidden = True
                error_type, message = CanvasErrorHandler.handle_canvas_exception(
                    e, "class submission download"
                )
                if error_type == "student_limitation":
                    extraction_stats.student_limitation_warnings += 1
                    if extraction_stats.student_limitation_warnings == 1:
                        print(f"    Note: Not authorized to download every student's assignment submission. Downloading submission for user {USER_ID} only.")
                else:
                    CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                    extraction_stats.error_count += 1
            except Exception as e:
                # A missing or failing class listing must not stop the export;
                # fall back to this user's own submission below.
                error_type, message = CanvasErrorHandler.handle_canvas_exception(
                    e, "submission retrieval"
                )
                CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                extraction_stats.error_count += 1

            if submissions is None:
                # Use the bulk-fetched submission when it contains this
                # assignment, otherwise ask for it individually.
                own_submission = own_submissions.get(assignment.id) if own_submissions is not None else None
                if own_submission is not None:
                    submissions = [own_submission]
                else:
                    # Download submission for this user only
                    try:
                        submissions = [assignment.get_submission(USER_ID, include=["submission_history", "submission_comments"])]
                    except (ResourceDoesNotExist, NameError, IndexError) as e:
                        error_type, message = CanvasErrorHandler.handle_canvas_exception(
                            e, "submission retrieval"
                        )
                        CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                        extraction_stats.error_count += 1
                        submissions = []
                    except Exception as e:
                        error_type, message = CanvasErrorHandler.handle_canvas_exception(
                            e, "submission retrieval"
                        )
                        CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                        extraction_stats.error_count += 1
                        submissions = []

            try:
                submissions[0] #throw error if no submissions found at all but without error
            except (ResourceDoesNotExist, NameError, IndexError) as e:
                error_type, message = CanvasErrorHandler.handle_canvas_exception(
                    e, "submission retrieval"
                )
                CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                extraction_stats.error_count += 1
            except Exception as e:
                error_type, message = CanvasErrorHandler.handle_canvas_exception(
                    e, "submission retrieval"
                )
                CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                extraction_stats.error_count += 1
            else:
                try:
                    for submission in submissions:

                        sub_view = submissionView()

                        # Submission ID
                        sub_view.id = submission.id if \
                            hasattr(submission, "id") else 0
                            
                        # My grade
                        sub_view.grade = str(submission.grade) if \
                            hasattr(submission, "grade") else ""
                        # My raw score
                        sub_view.raw_score = str(submission.score) if \
                            hasattr(submission, "score") else ""
                        # Total possible score
                        sub_view.total_possible_points = str(assignment.points_possible) if \
                            hasattr(assignment, "points_possible") else ""
                        # Submission comments
                        sub_view.submission_comments = str(submission.submission_comments) if \
                            hasattr(submission, "submission_comments") else ""
                        # Submission text (online entry), used by the HTML exporter
                        sub_view.body = str(submission.body) if \
                            hasattr(submission, "body") and submission.body else ""
                        # Structured comments for the HTML exporter
                        comments = getattr(submission, "submission_comments", None)
                        if isinstance(comments, list):
                            sub_view.submission_comments_raw = comments
                        # Attempt history, only returned when explicitly requested
                        history = getattr(submission, "submission_history", None)
                        if isinstance(history, list):
                            sub_view.submission_history = history
                        # Attempt
                        sub_view.attempt = submission.attempt if \
                            hasattr(submission, "attempt") and submission.attempt is not None else 0
                        # User ID
                        sub_view.user_id = str(submission.user_id) if \
                            hasattr(submission, "user_id") else ""
                            
                        # Submission URL
                        sub_view.preview_url = str(submission.preview_url) if \
                            hasattr(submission, "preview_url") else ""
                        #   External URL
                        sub_view.ext_url = str(submission.url) if \
                            hasattr(submission, "url") else ""

                        attachments = getattr(submission, "attachments", None) or []
                        if attachments:
                            print(f"        Found {len(attachments)} attachments")
                        for attachment in attachments:
                            attach_view = attachmentView()
                            attach_view.url = attachment.url
                            attach_view.id = attachment.id
                            attach_view.filename = attachment.filename
                            sub_view.attachments.append(attach_view)
                        assignment_view.submissions.append(sub_view)
                        extraction_stats.submissions_found += 1
                except Exception as e:
                    error_type, message = CanvasErrorHandler.handle_canvas_exception(
                        e, "submission processing"
                    )
                    CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                    extraction_stats.error_count += 1

            assignment_views.append(assignment_view)
            extraction_stats.assignments_found += 1
    except Exception as e:
        error_type, message = CanvasErrorHandler.handle_canvas_exception(
            e, "course assignments processing"
        )
        CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
        extraction_stats.error_count += 1

    return assignment_views


def findCourseAnnouncements(course):
    announcement_views = []

    try:
        announcements = course.get_discussion_topics(only_announcements=True)

        for announcement in announcements:
            discussion_view = getDiscussionView(announcement)

            announcement_views.append(discussion_view)
            extraction_stats.announcements_found += 1
    except Exception as e:
        error_type, message = CanvasErrorHandler.handle_canvas_exception(
            e, "announcement processing"
        )
        CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
        extraction_stats.error_count += 1

    return announcement_views


def getDiscussionView(discussion_topic):
    # Create discussion view
    discussion_view = discussionView()

    #ID
    discussion_view.id = discussion_topic.id if hasattr(discussion_topic, "id") else 0

    # Title
    discussion_view.title = str(discussion_topic.title) if hasattr(discussion_topic, "title") else ""
    # Author
    discussion_view.author = str(discussion_topic.user_name) if hasattr(discussion_topic, "user_name") else ""
    # Posted date
    try:
        discussion_view.posted_date = dateutil.parser.parse(discussion_topic.created_at).strftime("%B %d, %Y %I:%M %p") if \
            hasattr(discussion_topic, "created_at") and discussion_topic.created_at else ""
    except (ValueError, TypeError):
        discussion_view.posted_date = ""
    # Body
    discussion_view.body = str(discussion_topic.message) if hasattr(discussion_topic, "message") else ""

    # URL
    discussion_view.url = str(discussion_topic.html_url) if hasattr(discussion_topic, "html_url") else ""
    
    # Keeps track of how many topic_entries there are.
    topic_entries_counter = 0

    # Topic entries
    if hasattr(discussion_topic, "discussion_subentry_count") and discussion_topic.discussion_subentry_count > 0:
        # Need to get replies to entries recursively?

        discussion_topic_entries = discussion_topic.get_topic_entries()

        try:
            for topic_entry in discussion_topic_entries:
                topic_entries_counter += 1
                
                # Create new discussion view for the topic_entry
                topic_entry_view = topicEntryView()

                # ID
                topic_entry_view.id = topic_entry.id if hasattr(topic_entry, "id") else 0
                # Author
                topic_entry_view.author = str(topic_entry.user_name) if hasattr(topic_entry, "user_name") else ""
                # Posted date
                try:
                    topic_entry_view.posted_date = dateutil.parser.parse(topic_entry.created_at).strftime("%B %d, %Y %I:%M %p") if \
                        hasattr(topic_entry, "created_at") and topic_entry.created_at else ""
                except (ValueError, TypeError):
                    topic_entry_view.posted_date = ""
                # Body
                topic_entry_view.body = str(topic_entry.message) if hasattr(topic_entry, "message") else ""

                # Get this topic's replies
                topic_entry_replies = topic_entry.get_replies()

                try:
                    for topic_reply in topic_entry_replies:
                        # Create new topic reply view
                        topic_reply_view = topicReplyView()
                        
                        # ID
                        topic_reply_view.id = topic_reply.id if hasattr(topic_reply, "id") else 0

                        # Author
                        topic_reply_view.author = str(topic_reply.user_name) if hasattr(topic_reply, "user_name") else ""
                        # Posted Date
                        try:
                            topic_reply_view.posted_date = dateutil.parser.parse(topic_reply.created_at).strftime("%B %d, %Y %I:%M %p") if \
                                hasattr(topic_reply, "created_at") and topic_reply.created_at else ""
                        except (ValueError, TypeError):
                            topic_reply_view.posted_date = ""
                        # Body
                        topic_reply_view.body = str(topic_reply.message) if hasattr(topic_reply, "message") else ""

                        topic_entry_view.topic_replies.append(topic_reply_view)
                except Exception as e:
                    error_type, message = CanvasErrorHandler.handle_canvas_exception(
                        e, "discussion topic reply processing"
                    )
                    CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
                    if error_type == "student_limitation":
                        extraction_stats.student_limitation_warnings += 1
                    elif error_type == "not_found":
                        pass  # Already handled by log_error
                    else:
                        extraction_stats.error_count += 1

                discussion_view.topic_entries.append(topic_entry_view)
        except Exception as e:
            error_type, message = CanvasErrorHandler.handle_canvas_exception(
                e, "discussion topic entry processing"
            )
            CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
            if error_type == "student_limitation":
                extraction_stats.student_limitation_warnings += 1
            elif error_type == "not_found":
                pass  # Already handled by log_error
            else:
                extraction_stats.error_count += 1
        
    # Amount of pages  
    discussion_view.amount_pages = int(topic_entries_counter/50) + 1 # Typically 50 topic entries are stored on a page before it creates another page.
    
    return discussion_view


def findCourseDiscussions(course):
    discussion_views = []

    try:
        discussion_topics = course.get_discussion_topics()

        for discussion_topic in discussion_topics:
            discussion_view = None
            discussion_view = getDiscussionView(discussion_topic)

            discussion_views.append(discussion_view)
            extraction_stats.discussions_found += 1
    except Exception as e:
        error_type, message = CanvasErrorHandler.handle_canvas_exception(
            e, "discussion processing"
        )
        CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)
        extraction_stats.error_count += 1

    return discussion_views


def _course_term_name(course):
    """Return the term name for a course.

    canvasapi stores the term returned by the API as a plain dict, but a
    CanvasObject-like attribute is handled too.
    """
    term = getattr(course, "term", None)
    if isinstance(term, dict):
        return str(term.get("name") or "")
    return str(getattr(term, "name", "") or "") if term else ""


def getCourseView(course):
    course_view = courseView()

    # Course ID
    course_view.course_id = course.id if hasattr(course, "id") else 0

    # Course term
    course_view.term = makeValidFilename(_course_term_name(course))

    # Course code
    course_view.course_code = makeValidFilename(course.course_code if hasattr(course, "course_code") else "")

    # Course name
    course_view.name = course.name if hasattr(course, "name") else ""

    print(f"Working on: {course_view.term}: {course_view.name}")

    # Course assignments
    print("  Getting assignments")
    course_view.assignments = findCourseAssignments(course)
    print(f"    Found {len(course_view.assignments)} assignments")

    # Course announcements
    print("  Getting announcements")
    course_view.announcements = findCourseAnnouncements(course)
    print(f"    Found {len(course_view.announcements)} announcements")

    # Course discussions
    print("  Getting discussions")
    course_view.discussions = findCourseDiscussions(course)
    print(f"    Found {len(course_view.discussions)} discussions")

    # Course pages
    print("  Getting pages")
    course_view.pages = findCoursePages(course)
    print(f"    Found {len(course_view.pages)} pages")

    return course_view


def exportAllCourseData(course_view):
    json_str = json.dumps(json.loads(jsonpickle.encode(course_view, unpicklable = False)), indent = 4)

    course_output_dir = os.path.join(DL_LOCATION, course_view.term,
                                     course_view.course_code)

    # Create directory if not present
    if not os.path.exists(course_output_dir):
        os.makedirs(course_output_dir)

    course_output_path = os.path.join(course_output_dir,
                                      course_view.course_code + ".json")

    print(f"    Exporting JSON data for {course_view.course_code}...")
    with open(course_output_path, "w") as out_file:
        out_file.write(json_str)
        
    extraction_stats.json_files_created += 1
    print(f"      ✓ Data saved to: {course_output_path}")

def _install_default_http_timeout(timeout_seconds):
    """Give every canvasapi HTTP request a default timeout.

    canvasapi creates a plain requests.Session without a timeout, so a
    connection that stalls (dropped network, sleeping host, ...) can block an
    export forever. requests routes all HTTP verbs through Session.request,
    so wrapping it covers API calls and file downloads alike.
    """
    global _http_timeout_installed
    if _http_timeout_installed:
        return

    from canvasapi.requester import Requester

    original_init = Requester.__init__

    def init_with_timeout(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        session = getattr(self, "_session", None)
        if session is not None:
            session.request = functools.partial(session.request, timeout=timeout_seconds)

    Requester.__init__ = init_with_timeout
    _http_timeout_installed = True


_http_timeout_installed = False


def _env_flag(name):
    """Return True when an environment variable is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _fetch_front_page_body(course):
    """Return the course front page HTML, or an empty string when unavailable."""
    try:
        front_page = course.show_front_page()
        return str(getattr(front_page, "body", "") or "")
    except Exception:
        return ""


def _preserve_mtime(path, canvas_object):
    """Copy a Canvas object's timestamp onto a downloaded file.

    Canvas download responses do not carry the remote modification time, so
    without this every download would look brand new on disk. Keeping the
    Canvas ``updated_at`` makes the "recently active course" check (used for
    NotebookLM uploads) meaningful.
    """
    for attr in ("updated_at", "modified_at", "created_at"):
        value = getattr(canvas_object, attr, None)
        if not value:
            continue
        try:
            timestamp = dateutil.parser.parse(value).timestamp()
            os.utime(path, (timestamp, timestamp))
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        return


if __name__ == "__main__":

    print("Welcome to the Canvas Student Data Export Tool\n")

    parser = argparse.ArgumentParser(description="Export nearly all of a student's Canvas LMS data.")
    parser.add_argument("-c", "--config", default=os.environ.get("CANVAS_CONFIG", "credentials.yaml"), help="Path to YAML credentials file (default: credentials.yaml or $CANVAS_CONFIG)")
    parser.add_argument("-o", "--output", default="./output", help="Directory to store exported data (default: ./output)")
    parser.add_argument("--html", action="store_true", default=_env_flag("CANVAS_HTML"), help="Generate HTML pages from Canvas API data (also enabled with CANVAS_HTML=1).")
    parser.add_argument("--no-markdown", action="store_true", help="Do not convert HTML/Word/PPTX/... files to Markdown.")
    parser.add_argument("--no-ocr", action="store_true", help="Do not OCR images/PDFs even when MISTRAL_API_KEY is configured.")
    parser.add_argument("--notebooklm", action="store_true", help="Upload recent courses to NotebookLM (requires NOTEBOOKLM_AUTH_JSON).")
    parser.add_argument("--no-notebooklm", action="store_true", help="Do not upload courses to NotebookLM, even when NOTEBOOKLM_AUTH_JSON is set.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output for debugging.")
    parser.add_argument("--version", action="version", version="Canvas Student Data Export Tool 2.0")

    args = parser.parse_args()

    # Load credentials from YAML (if present) and let CANVAS_* env vars override.
    creds = _apply_env_overrides(_load_credentials(args.config))

    # Validate credentials
    required = ["API_URL", "API_KEY", "USER_ID"]
    missing = [k for k in required if not creds.get(k)]

    if missing:
        print(f"Error: {args.config} is missing required field(s): {', '.join(missing)}.")
        print("Please create the YAML file with the following structure, or set the\n"
              "equivalent CANVAS_* environment variables:\n"
              "API_URL: https://<your>.instructure.com\n"
              "API_KEY: <your key>\n"
              "USER_ID: 123456\n")
        sys.exit(1)

    # Populate globals expected throughout the script
    API_URL = creds["API_URL"].strip().rstrip('/')
    API_KEY = creds["API_KEY"].strip()  # Remove leading/trailing whitespace which is a common issue
    try:
        USER_ID = int(creds["USER_ID"])
    except (TypeError, ValueError):
        print(f"Error: USER_ID must be an integer (got {creds['USER_ID']!r}).")
        sys.exit(1)
    COURSES_TO_SKIP = creds.get("COURSES_TO_SKIP", [])

    # Update output directory
    DL_LOCATION = args.output

    # Network timeout (seconds): requests has no default, so without this a
    # dropped connection can hang a run forever.
    try:
        http_timeout = float(creds.get("HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT))
        if not math.isfinite(http_timeout) or http_timeout <= 0:
            http_timeout = DEFAULT_HTTP_TIMEOUT
    except (TypeError, ValueError):
        print(f"Warning: Invalid HTTP_TIMEOUT value; using {DEFAULT_HTTP_TIMEOUT:g}s.")
        http_timeout = DEFAULT_HTTP_TIMEOUT
    _install_default_http_timeout(http_timeout)
    if args.verbose:
        print(f"HTTP timeout: {http_timeout:g}s")

    # Markdown conversion: a single pandoc pipeline handles HTML/Word/PPTX/
    # EPUB/... including the pages written by this tool.
    markdown_enabled = not args.no_markdown
    pandoc_path = markdown_export.find_pandoc() if markdown_enabled else None
    markdown_effective = markdown_enabled and bool(pandoc_path)
    if markdown_enabled and not pandoc_path:
        print("Note: pandoc was not found; Markdown conversion is skipped. Install the "
              "full pandoc build (the Docker 'full' image bundles it).")

    # Mistral OCR: only active when an API key is configured (config file wins
    # over the environment so credentials can live in one place).
    mistral_api_key = ""
    if not args.no_ocr:
        mistral_api_key = str(creds.get("MISTRAL_API_KEY")
                              or os.environ.get("MISTRAL_API_KEY", "")).strip()
    if mistral_api_key:
        print("Mistral OCR enabled: images and PDFs will be converted to Markdown.")

    # NotebookLM uploads are opt-in (or automatic when NOTEBOOKLM_AUTH_JSON is set).
    notebooklm_upload_enabled = not args.no_notebooklm and (
        args.notebooklm or notebooklm_upload.notebooklm_enabled()
    )
    if notebooklm_upload_enabled:
        try:
            notebooklm_months = float(creds.get("NOTEBOOKLM_MONTHS")
                                      or os.environ.get("CANVAS_NOTEBOOKLM_MONTHS") or 3)
        except (TypeError, ValueError):
            notebooklm_months = 3.0
        if notebooklm_months <= 0:
            notebooklm_months = 3.0
        notebooklm_max_age_days = int(round(notebooklm_months * 30.44))
        try:
            notebooklm_max_sources = int(creds.get("NOTEBOOKLM_MAX_SOURCES")
                                         or os.environ.get("CANVAS_NOTEBOOKLM_MAX_SOURCES") or 300)
        except (TypeError, ValueError):
            notebooklm_max_sources = 300
        if notebooklm_max_sources <= 0:
            notebooklm_max_sources = 300
        notebooklm_state_path = os.path.join(DL_LOCATION, notebooklm_upload.STATE_FILE_NAME)
        print(f"NotebookLM upload enabled: courses active within the last "
              f"{notebooklm_months:g} months (max {notebooklm_max_sources} sources per notebook).")

    print("\nConnecting to Canvas…\n")

    # Initialize a new Canvas object
    canvas = Canvas(API_URL, API_KEY)

    # Test the connection and API key
    try:
        user = canvas.get_current_user()
        print(f"Successfully authenticated as: {user.name} (ID: {user.id})")
        if user.id != USER_ID:
            print(f"Warning: Authenticated user ID ({user.id}) does not match configured USER_ID ({USER_ID})")
    except Exception as e:
        error_type, message = CanvasErrorHandler.handle_canvas_exception(
            e, "Canvas authentication"
        )
        if CanvasErrorHandler.is_fatal_error(error_type):
            print(f"FATAL: {message}")
            sys.exit(1)
        else:
            CanvasErrorHandler.log_error(error_type, message, verbose=args.verbose)

    print(f"Creating output directory: {DL_LOCATION}\n")
    os.makedirs(DL_LOCATION, exist_ok=True)

    all_courses_views = []

    print("Getting list of all courses\n")
    courses_list = [
        canvas.get_courses(enrollment_state = "active", include=["term"]),
        canvas.get_courses(enrollment_state = "completed", include=["term"])
    ]

    skip = set(COURSES_TO_SKIP)

    if args.html:
        print("HTML export enabled: pages will be generated from Canvas API data\n")

    for courses in courses_list:
        for course in courses:
            if course.id in skip or not hasattr(course, "name") or not hasattr(course, "term"):
                continue

            course_view = getCourseView(course)

            if args.html:
                course_view.homepage_html = _fetch_front_page_body(course)

            all_courses_views.append(course_view)

            course_dir = os.path.join(DL_LOCATION, course_view.term,
                                      course_view.course_code)

            print("  Downloading all files")
            downloadCourseFiles(course, course_view)

            print("  Downloading submission attachments")
            download_submission_attachments(course, course_view)

            print("  Getting modules and downloading module files")
            course_view.modules = findCourseModules(course, course_view)

            print("  Exporting all course data")
            exportAllCourseData(course_view)

            html_pages_saved_in_course = 0
            if args.html:
                html_pages_saved_in_course = export_course_html(course_view, DL_LOCATION, USER_ID)
                extraction_stats.html_pages_generated += html_pages_saved_in_course

            # --- Markdown conversion (HTML/Word/... -> .md) ------------------
            if markdown_effective:
                converted = markdown_export.convert_tree(course_dir, pandoc=pandoc_path)
                if converted:
                    extraction_stats.markdown_files_created += len(converted)
                    print(f"  ✓ Converted {len(converted)} files to Markdown")

            # --- Mistral OCR (images/PDFs -> .md) ---------------------------
            if mistral_api_key:
                ocr_written = mistral_ocr.ocr_tree(
                    course_dir, mistral_api_key, timeout=http_timeout, verbose=args.verbose
                )
                if ocr_written:
                    extraction_stats.ocr_files_created += len(ocr_written)
                    print(f"  ✓ OCR'd {len(ocr_written)} images/PDFs to Markdown")

            # --- NotebookLM upload (recent courses only) --------------------
            if notebooklm_upload_enabled:
                last_modified = notebooklm_upload.course_last_modified(course_view, course_dir)
                if notebooklm_upload.is_recent(last_modified, max_age_days=notebooklm_max_age_days):
                    notebook_title = f"{course_view.term} - {course_view.course_code} - {course_view.name}".strip(" -")
                    upload_stats = notebooklm_upload.upload_course(
                        notebook_title, course_dir, last_modified, notebooklm_state_path,
                        max_age_days=notebooklm_max_age_days,
                        max_sources=notebooklm_max_sources,
                        verbose=args.verbose,
                    )
                    if upload_stats:
                        extraction_stats.notebooklm_notebooks += 1
                        extraction_stats.notebooklm_sources_uploaded += upload_stats.get("uploaded", 0)
                else:
                    print("  Note: course is older than the NotebookLM window; skipping upload")

            # Show mini-summary for this course
            assignments_count = len(course_view.assignments)
            submissions_count = sum(len(a.submissions) for a in course_view.assignments)
            modules_count = len(course_view.modules)
            pages_count = len(course_view.pages)
            announcements_count = len(course_view.announcements)
            discussions_count = len(course_view.discussions)

            print(f"  ✓ Course data exported:")
            print(f"    • {assignments_count} assignments with {submissions_count} submissions (JSON)")
            print(f"    • {modules_count} modules (JSON)")
            print(f"    • {pages_count} pages (JSON)")
            print(f"    • {announcements_count} announcements (JSON)")
            print(f"    • {discussions_count} discussions (JSON)")
            if args.html:
                print(f"    • {html_pages_saved_in_course} HTML pages generated")
            print()

    if args.html:
        extraction_stats.html_pages_generated += export_course_list_html(all_courses_views, DL_LOCATION)

    print("Exporting data from all courses combined as one file: "
          "all_output.json")
    json_str = jsonpickle.encode(all_courses_views, unpicklable=False, indent=4)

    all_output_path = os.path.join(DL_LOCATION, "all_output.json")

    with open(all_output_path, "w") as out_file:
        out_file.write(json_str)

    extraction_stats.json_files_created += 1
    print(f"Combined JSON data exported to: {all_output_path}")

    print("\nProcess complete. All canvas data exported!")
    print(extraction_stats.summary(DL_LOCATION, html_enabled=args.html))
