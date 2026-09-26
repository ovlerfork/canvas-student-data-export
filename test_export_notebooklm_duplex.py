import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).parent


def _module_stubs():
    canvasapi = types.ModuleType("canvasapi")
    canvasapi.Canvas = object
    exceptions = types.ModuleType("canvasapi.exceptions")
    for name in ("ResourceDoesNotExist", "Unauthorized", "Forbidden",
                 "InvalidAccessToken", "CanvasException"):
        setattr(exceptions, name, type(name, (Exception,), {}))
    canvasapi.exceptions = exceptions

    dateutil = types.ModuleType("dateutil")
    dateutil.parser = types.SimpleNamespace(
        parse=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")))
    html_export = types.ModuleType("html_export")
    html_export.export_combined_announcements = lambda *args: None
    html_export.export_course_html = lambda *args: 0
    html_export.export_course_list_html = lambda *args: 0
    naming = types.ModuleType("naming")
    naming.MAX_FOLDER_NAME_SIZE = 255
    naming.makeValidFilename = str
    naming.makeValidFolderPath = str
    naming.shortenFileName = str
    notebooklm = types.ModuleType("notebooklm_upload")
    notebooklm.STATE_FILE_NAME = ".notebooklm_state.json"
    notebooklm.notebooklm_enabled = lambda: True
    notebooklm.course_last_modified = lambda *args: 0
    notebooklm.is_recent = lambda *args, **kwargs: True
    notebooklm.upload_course = lambda *args, **kwargs: None
    return {
        "canvasapi": canvasapi,
        "canvasapi.exceptions": exceptions,
        "dateutil": dateutil,
        "dateutil.parser": dateutil.parser,
        "jsonpickle": types.SimpleNamespace(encode=lambda *args, **kwargs: "{}"),
        "requests": types.ModuleType("requests"),
        "yaml": types.SimpleNamespace(full_load=lambda *args: {}),
        "markdown_export": types.SimpleNamespace(find_pandoc=lambda: None),
        "mistral_ocr": types.ModuleType("mistral_ocr"),
        "notebooklm_upload": notebooklm,
        "html_export": html_export,
        "naming": naming,
    }


def _load_export():
    spec = importlib.util.spec_from_file_location("export_for_test", ROOT / "export.py")
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, _module_stubs(), clear=False):
        spec.loader.exec_module(module)
    return module


class NotebookLMDuplexWorkflowTest(unittest.TestCase):
    def test_next_course_and_combined_export_continue_while_upload_waits(self):
        exporter = _load_export()
        started = threading.Event()
        release_upload = threading.Event()
        second_downloaded = threading.Event()
        second_upload_started = threading.Event()
        combined_exported = threading.Event()
        output = io.StringIO()
        runner_errors = []

        class Course:
            def __init__(self, course_id):
                self.id = course_id
                self.name = "Course %s" % course_id
                self.term = object()

        class Canvas:
            def __init__(self, *args):
                pass

            def get_current_user(self):
                return types.SimpleNamespace(name="Student", id=1)

            def get_courses(self, enrollment_state, include):
                return [Course(1), Course(2)] if enrollment_state == "active" else []

        def course_view(course):
            return types.SimpleNamespace(
                term="Fall", course_code="C%s" % course.id, name=course.name,
                assignments=[], modules=[], pages=[], announcements=[], discussions=[])

        def upload(title, *args, **kwargs):
            if title.endswith("Course 1"):
                started.set()
                self.assertTrue(release_upload.wait(2), "test did not release upload")
                return {"uploaded": 1, "fallback": 0, "pruned": 0,
                        "report": "guide.md", "error": False}
            second_upload_started.set()
            print("ERROR: simulated NotebookLM upload failure")
            return {"uploaded": 1, "fallback": 0, "pruned": 0,
                    "report": "", "error": True}

        def download(course, view):
            if course.id == 2:
                second_downloaded.set()

        def encode(value, **kwargs):
            combined_exported.set()
            return "{}"

        with tempfile.TemporaryDirectory() as output_dir, \
             mock.patch.object(exporter, "Canvas", Canvas), \
             mock.patch.object(exporter, "_load_credentials", return_value={
                 "API_URL": "https://canvas.example", "API_KEY": "key", "USER_ID": 1}), \
             mock.patch.object(exporter, "_install_default_http_timeout"), \
             mock.patch.object(exporter, "getCourseView", side_effect=course_view), \
             mock.patch.object(exporter, "downloadCourseFiles", side_effect=download), \
             mock.patch.object(exporter, "download_submission_attachments"), \
             mock.patch.object(exporter, "findCourseModules", return_value=[]), \
             mock.patch.object(exporter, "exportAllCourseData"), \
             mock.patch.object(exporter, "export_combined_announcements", return_value=None), \
             mock.patch.object(exporter.notebooklm_upload, "upload_course", side_effect=upload), \
             mock.patch.object(exporter.jsonpickle, "encode", side_effect=encode), \
             mock.patch.object(sys, "argv", ["export.py", "--notebooklm", "--no-markdown",
                                              "--no-ocr", "-o", output_dir]):
            exporter.extraction_stats = exporter.ExtractionStats()

            def run_main():
                try:
                    with contextlib.redirect_stdout(output):
                        exporter.main()
                except BaseException as error:
                    runner_errors.append(error)

            runner = threading.Thread(target=run_main)
            runner.start()
            try:
                self.assertTrue(started.wait(1), "first upload did not start")
                self.assertTrue(second_downloaded.wait(1), "course 2 download was blocked")
                self.assertTrue(combined_exported.wait(1), "combined export was blocked")
                self.assertFalse(second_upload_started.wait(0.2),
                                 "second upload ran concurrently with the first")
            finally:
                release_upload.set()
            runner.join(3)

        self.assertFalse(runner.is_alive(), "export did not finish after upload release")
        self.assertEqual([], runner_errors)
        self.assertIn("1 notebooks used", output.getvalue())
        self.assertIn("1 sources uploaded", output.getvalue())
        self.assertIn("1 incremental study guides generated", output.getvalue())
        self.assertIn("ERROR: simulated NotebookLM upload failure", output.getvalue())

    def test_reused_course_directory_waits_for_its_prior_upload(self):
        exporter = _load_export()
        started = threading.Event()
        release_upload = threading.Event()
        second_downloaded = threading.Event()
        runner_errors = []

        class Course:
            def __init__(self, course_id):
                self.id = course_id
                self.name = "Course %s" % course_id
                self.term = object()

        class Canvas:
            def __init__(self, *args):
                pass

            def get_current_user(self):
                return types.SimpleNamespace(name="Student", id=1)

            def get_courses(self, enrollment_state, include):
                return [Course(1), Course(2)] if enrollment_state == "active" else []

        def course_view(course):
            return types.SimpleNamespace(
                term="Fall", course_code="Shared", name=course.name,
                assignments=[], modules=[], pages=[], announcements=[], discussions=[])

        def upload(title, *args, **kwargs):
            if title.endswith("Course 1"):
                started.set()
                self.assertTrue(release_upload.wait(2), "test did not release upload")
            return {"uploaded": 0, "fallback": 0, "pruned": 0,
                    "report": "", "error": False}

        def download(course, view):
            if course.id == 2:
                second_downloaded.set()

        with tempfile.TemporaryDirectory() as output_dir, \
             mock.patch.object(exporter, "Canvas", Canvas), \
             mock.patch.object(exporter, "_load_credentials", return_value={
                 "API_URL": "https://canvas.example", "API_KEY": "key", "USER_ID": 1}), \
             mock.patch.object(exporter, "_install_default_http_timeout"), \
             mock.patch.object(exporter, "getCourseView", side_effect=course_view), \
             mock.patch.object(exporter, "downloadCourseFiles", side_effect=download), \
             mock.patch.object(exporter, "download_submission_attachments"), \
             mock.patch.object(exporter, "findCourseModules", return_value=[]), \
             mock.patch.object(exporter, "exportAllCourseData"), \
             mock.patch.object(exporter, "export_combined_announcements", return_value=None), \
             mock.patch.object(exporter.notebooklm_upload, "upload_course", side_effect=upload), \
             mock.patch.object(exporter.jsonpickle, "encode", return_value="{}"), \
             mock.patch.object(sys, "argv", ["export.py", "--notebooklm", "--no-markdown",
                                              "--no-ocr", "-o", output_dir]):
            exporter.extraction_stats = exporter.ExtractionStats()

            def run_main():
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        exporter.main()
                except BaseException as error:
                    runner_errors.append(error)

            runner = threading.Thread(target=run_main)
            runner.start()
            try:
                self.assertTrue(started.wait(1), "first upload did not start")
                self.assertFalse(second_downloaded.wait(0.2),
                                 "shared course directory was rewritten during upload")
            finally:
                release_upload.set()
            runner.join(3)

        self.assertFalse(runner.is_alive(), "export did not finish after upload release")
        self.assertEqual([], runner_errors)
        self.assertTrue(second_downloaded.is_set())


class CourseEndSelectionWorkflowTest(unittest.TestCase):
    def test_end_at_filter_skips_before_details_and_can_be_overridden(self):
        exporter = _load_export()
        now = datetime.now(timezone.utc)

        class Course:
            def __init__(self, course_id, end_at):
                self.id = course_id
                self.name = "Course %s" % course_id
                self.term = object()
                self.end_at = end_at

        courses = [
            Course(1, (now - timedelta(days=31)).isoformat()),
            Course(2, (now - timedelta(days=29)).isoformat()),
            Course(3, None),
            Course(4, None),
            Course(5, "not-a-timestamp"),
        ]

        class Canvas:
            def __init__(self, *args):
                pass

            def get_current_user(self):
                return types.SimpleNamespace(name="Student", id=1)

            def get_courses(self, enrollment_state, include):
                return courses if enrollment_state == "active" else []

        def run_export(extra_args=(), include_ended_env=""):
            details_requested = []
            output = io.StringIO()

            def course_view(course):
                details_requested.append(course.id)
                return types.SimpleNamespace(
                    term="Fall", course_code="C%s" % course.id, name=course.name,
                    assignments=[], modules=[], pages=[], announcements=[], discussions=[])

            with tempfile.TemporaryDirectory() as output_dir, \
                 mock.patch.object(exporter, "Canvas", Canvas), \
                 mock.patch.object(exporter, "_load_credentials", return_value={
                     "API_URL": "https://canvas.example", "API_KEY": "key", "USER_ID": 1,
                     "COURSES_TO_SKIP": [4]}), \
                 mock.patch.object(exporter, "_install_default_http_timeout"), \
                 mock.patch.object(exporter, "getCourseView", side_effect=course_view), \
                 mock.patch.object(exporter, "downloadCourseFiles"), \
                 mock.patch.object(exporter, "download_submission_attachments"), \
                 mock.patch.object(exporter, "findCourseModules", return_value=[]), \
                 mock.patch.object(exporter, "exportAllCourseData"), \
                 mock.patch.object(exporter, "export_combined_announcements", return_value=None), \
                 mock.patch.object(exporter.jsonpickle, "encode", return_value="{}"), \
                 mock.patch.dict(os.environ, {"CANVAS_INCLUDE_ENDED": include_ended_env}, clear=False), \
                 mock.patch.object(sys, "argv", ["export.py", "--no-markdown", "--no-ocr", "-o", output_dir, *extra_args]):
                exporter.extraction_stats = exporter.ExtractionStats()
                with contextlib.redirect_stdout(output):
                    exporter.main()
            return details_requested, output.getvalue()

        details, output = run_export()
        self.assertEqual([2, 3, 5], details)
        self.assertIn("Skipping Course 1: ended more than 30 days ago", output)
        self.assertIn("unreadable end_at; including it", output)

        details, _ = run_export(("--include-ended",))
        self.assertEqual([1, 2, 3, 5], details)

        details, _ = run_export(include_ended_env="1")
        self.assertEqual([1, 2, 3, 5], details)


class ErrorVerbosityRegressionTest(unittest.TestCase):
    def _run_export(self, exporter, courses, extra_args=()):
        output = io.StringIO()
        errors = io.StringIO()

        class Canvas:
            def __init__(self, *args):
                pass

            def get_current_user(self):
                return types.SimpleNamespace(name="Student", id=1)

            def get_courses(self, enrollment_state, include):
                return courses if enrollment_state == "active" else []

        def course_view(course):
            return types.SimpleNamespace(
                term="Fall", course_code="C%s" % course.id, name=course.name,
                assignments=[], modules=[], pages=[], announcements=[], discussions=[])

        with tempfile.TemporaryDirectory() as output_dir, \
             mock.patch.object(exporter, "Canvas", Canvas), \
             mock.patch.object(exporter, "_load_credentials", return_value={
                 "API_URL": "https://canvas.example", "API_KEY": "key", "USER_ID": 1}), \
             mock.patch.object(exporter, "_install_default_http_timeout"), \
             mock.patch.object(exporter, "getCourseView", side_effect=course_view), \
             mock.patch.object(exporter, "download_submission_attachments"), \
             mock.patch.object(exporter, "findCourseModules", return_value=[]), \
             mock.patch.object(exporter, "export_combined_announcements", return_value=None), \
             mock.patch.object(exporter.jsonpickle, "encode", return_value="{}"), \
             mock.patch.object(sys, "argv", ["export.py", "--no-notebooklm", "--no-markdown",
                                              "--no-ocr", "-o", output_dir, *extra_args]):
            exporter.extraction_stats = exporter.ExtractionStats()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                exporter.main()
            result = {
                "stdout": output.getvalue(),
                "stderr": errors.getvalue(),
                "course_outputs": [
                    pathlib.Path(output_dir) / "Fall" / ("C%s" % course.id)
                    / ("C%s.json" % course.id)
                    for course in courses
                ],
                "combined": pathlib.Path(output_dir) / "all_output.json",
            }
            result["course_outputs_exist"] = [
                course_output.is_file() for course_output in result["course_outputs"]
            ]
            result["combined_exists"] = result["combined"].is_file()
        return result

    def test_forbidden_file_listing_continues_each_course_without_name_error(self):
        exporter = _load_export()

        class Course:
            def __init__(self, course_id):
                self.id = course_id
                self.name = "Course %s" % course_id
                self.term = object()

            def get_files(self):
                return iter(()) if self.id == 2 else self._forbidden_files()

            def _forbidden_files(self):
                class Files:
                    def __iter__(self):
                        raise exporter.Forbidden("file listing is forbidden")
                return Files()

        result = self._run_export(exporter, [Course(1), Course(2)])

        self.assertIn("Note: Access forbidden for course file download.", result["stdout"])
        self.assertIn("Student Account Limitations: 1 (expected)", result["stdout"])
        self.assertIn("Errors Encountered: 0", result["stdout"])
        self.assertEqual([True, True], result["course_outputs_exist"])
        self.assertTrue(result["combined_exists"])

    def test_verbose_flag_controls_traceback_for_file_listing_errors(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                exporter = _load_export()

                class Course:
                    id = 1
                    name = "Course 1"
                    term = object()

                    def get_files(self):
                        class Files:
                            def __iter__(self):
                                raise ValueError("file listing failed")
                        return Files()

                result = self._run_export(exporter, [Course()], ("--verbose",) if verbose else ())

                self.assertIn("ERROR: Unexpected error during course file download: file listing failed",
                              result["stdout"])
                self.assertIn("Errors Encountered: 1", result["stdout"])
                if verbose:
                    self.assertIn("Traceback", result["stderr"])
                    self.assertIn("ValueError: file listing failed", result["stderr"])
                else:
                    self.assertEqual("", result["stderr"])


if __name__ == "__main__":
    unittest.main()
