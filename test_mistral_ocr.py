import base64
import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from pypdf import PdfWriter
import requests

import mistral_ocr


class PdfCompletenessTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pdf = pathlib.Path(self.directory.name) / "source.pdf"
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=100, height=100)
        with self.pdf.open("wb") as destination:
            writer.write(destination)
        self.md = self.pdf.with_suffix(".md")
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def response(self, indexes, blank=False):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"pages": [
            {"index": index, "markdown": "" if blank else "Text %d" % index}
            for index in indexes]}
        return response

    def run_ocr(self, responses, uploaded=True, tree=False, chunk=2):
        with mock.patch.object(mistral_ocr, "_upload_ocr_file",
                               return_value="file" if uploaded else None), \
                mock.patch.object(mistral_ocr, "_delete_ocr_file"), \
                mock.patch.object(mistral_ocr.requests, "post", side_effect=responses) as post:
            if tree:
                result = mistral_ocr.ocr_tree(self.directory.name, "key",
                                              min_interval=0, pages_per_request=chunk)
            else:
                result = mistral_ocr.ocr_file(str(self.pdf), "key",
                                              min_interval=0, pages_per_request=chunk)
            return result, post

    def test_complete_chunks_preserve_blank_page_and_bound_last_request(self):
        first = self.response([0, 1])
        first.json.return_value["pages"][1]["markdown"] = ""
        result, post = self.run_ocr([first, self.response([2])])
        self.assertEqual(result, str(self.md))
        self.assertIn("<!-- page 2 -->", self.md.read_text())
        self.assertIn("<!-- page 3 -->\n\nText 2", self.md.read_text())
        self.assertEqual([call.kwargs["json"]["pages"] for call in post.call_args_list],
                         ["0-1", "2-2"])

    def test_incomplete_or_wrong_chunks_preserve_existing_markdown(self):
        old = "<!-- page 1 -->\n\n![Previously extracted image](attachments/source/image.png)"
        image = self.pdf.parent / "attachments" / "source" / "image.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"previous image")
        first = self.response([0, 1])
        first.json.return_value["pages"][0].update({
            "markdown": "![Updated image](image.png)",
            "images": [{"id": "image.png", "image_base64":
                        base64.b64encode(b"updated image").decode("ascii")}]})
        error_response = mock.Mock(status_code=400)
        error_response.json.return_value = {"detail": "bad page request"}
        for responses in ([first, requests.HTTPError(response=error_response)],
                          [first, self.response([])],
                          [self.response([])], [self.response([0])],
                          [self.response([0, 0])], [self.response([0, 2])],
                          [self.response([1, 2])]):
            with self.subTest(responses=responses):
                self.md.write_text(old)
                result, _ = self.run_ocr(responses)
                self.assertIsNone(result)
                self.assertEqual(self.md.read_text(), old)
                self.assertEqual(image.read_bytes(), b"previous image")

    def test_inline_fallback_requires_every_page(self):
        result, _ = self.run_ocr([self.response([0])], uploaded=False)
        self.assertIsNone(result)
        self.assertFalse(self.md.exists())
        rejected = mock.Mock(status_code=422)
        rejected.json.return_value = {"detail": "file route rejected"}
        first = self.response([0, 1])
        first.json.return_value["pages"][0]["images"] = [{
            "id": "discarded.png",
            "image_base64": base64.b64encode(b"discarded image").decode("ascii")}]
        result, _ = self.run_ocr([
            first, requests.HTTPError(response=rejected),
            self.response([0, 1, 2], blank=True)])
        self.assertEqual(result, str(self.md))
        self.assertIn("<!-- page 3 -->", self.md.read_text())
        self.assertFalse((self.pdf.parent / "attachments" / "source" / "discarded.png").exists())

    def test_tree_repairs_truncated_ocr_and_skips_complete_and_authored_output(self):
        self.md.write_text("<!-- page 1 -->\n\nOld truncated text")
        result, _ = self.run_ocr([self.response([0, 1]), self.response([2])], tree=True)
        self.assertEqual(result, [str(self.md)])
        result, post = self.run_ocr([], tree=True)
        self.assertEqual(result, [])
        post.assert_not_called()
        self.md.write_text("Hand-authored notes")
        result, post = self.run_ocr([], tree=True)
        self.assertEqual(result, [])
        post.assert_not_called()
        self.assertEqual(self.md.read_text(), "Hand-authored notes")

    def test_unknown_source_page_count_refuses_unverifiable_output(self):
        with mock.patch.object(mistral_ocr, "_pdf_page_count", return_value=None):
            result, post = self.run_ocr([])
        self.assertIsNone(result)
        post.assert_not_called()
        self.assertFalse(self.md.exists())


if __name__ == "__main__":
    unittest.main()
