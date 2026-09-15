# Introduction

The Canvas Student Data Export Tool exports nearly all of a student's data from the Instructure Canvas Learning Management System (Canvas LMS).  
This is useful when you are graduating or leaving your college or university, and would like to have a backup of all the data you had in canvas.

The tool exports the following data:
- Course Assignments (including submissions and attachments)
- Course Announcements
- Course Discussions
- Course Pages
- Course Files
- Course Modules
- (Optional) HTML pages generated from the Canvas API:
    - Course Home Page
    - Grades Page
    - Assignments
    - Announcements
    - Discussions
    - Modules
- (Optional) Markdown versions of HTML/Word/PowerPoint/EPUB/… files
- (Optional) Markdown OCR of images and PDFs through the Mistral OCR API
- (Optional) Automatic uploads of recent courses to Google NotebookLM

Data is saved in JSON (and optionally HTML/Markdown) format and organized into folders by academic term and course. Pages that would contain no actual information (for example an assignment with only an "Assigned/Due" header and "No description.") are not written at all.

Example output structure:
- Fall 2023
  - CS 101
    - announcements/
      - First Announcement/
        - announcement_1.html
        - announcement_1.md
      - announcement_list.html
    - assignments/
      - Sample Assignment/
        - assignment.html
        - assignment.md
        - submission.html
        - submission.md
        - attempts/
          - attempt_1.html
          - attempt_1.md
      - assignment_list.html
    - course files/
      - lecture_1.pptx
      - lecture_1.md
      - attachments/
        - lecture_1/
          - media/
            - image1.png
    - discussions/
      - Sample Discussion/
        - discussion_1.html
        - discussion_1.md
      - discussion_list.html
    - modules/
      - Sample Module/
        - Sample Page.html
        - Sample Page.md
      - modules_list.html
    - grades.html
    - homepage.html
    - CS 101.json
  - ENGL 101
    - ...
- Spring 2024
  - ...
- course_list.html
- all_output.json
- .notebooklm_state.json   (upload bookkeeping, only when NotebookLM is enabled)

# Getting Started

## Dependencies
- Python 3.8 or newer (NotebookLM uploads need Python 3.10 or newer)
- [pandoc](https://pandoc.org/installing.html) (optional but recommended) for HTML/Word/PowerPoint/Excel/EPUB → Markdown conversion. Use the full official release: several distribution packages omit the PowerPoint/Excel readers. The Docker `full` image bundles the official full build.
- [markdownify](https://pypi.org/project/markdownify/) (included in `requirements-full.txt`) to convert the HTML pages this tool generates
- A Mistral API key (optional) for OCR of images and PDFs
- A NotebookLM `storage_state.json` (optional) for automatic NotebookLM uploads

Install the Python dependencies (slim):
```bash
pip install -r requirements.txt
```

For Markdown conversion and NotebookLM uploads use the full set:
```bash
pip install -r requirements-full.txt
```

## Docker

Pre-built images are published to the GitHub Container Registry, so you do not need Python or anything else installed locally. Two variants are published for every commit:

| Tags | Contents |
| ---- | -------- |
| `:latest`, `:<version>`, `:sha-xxxxxxx` | **slim** – Canvas export, optional Mistral OCR, JSON/HTML output |
| `:latest-full`, `:<version>-full`, `:sha-xxxxxxx-full` | **full** – adds Markdown conversion (official full pandoc) and NotebookLM uploads |

```bash
docker run --rm \
  -v "$PWD/credentials.yaml:/config/credentials.yaml:ro" \
  -v "$PWD/output:/data" \
  ghcr.io/ovlerfork/canvas-student-data-export:latest-full
```

Inside the container the exporter defaults to `-c /config/credentials.yaml -o /data` and generates HTML pages from the Canvas API (no browser involved). Set `CANVAS_HTML=0` to skip the HTML pages.

Instead of mounting a credentials file, every value can be passed as an environment variable:

```bash
docker run --rm \
  -e CANVAS_API_URL=https://example.instructure.com \
  -e CANVAS_API_KEY=<your token> \
  -e CANVAS_USER_ID=123456 \
  -v "$PWD/output:/data" \
  ghcr.io/ovlerfork/canvas-student-data-export:latest
```

| Variable | Equivalent YAML key |
| -------- | ------------------- |
| `CANVAS_CONFIG` | Path passed to `-c` (default `/config/credentials.yaml` in the container) |
| `CANVAS_API_URL` | `API_URL` |
| `CANVAS_API_KEY` | `API_KEY` |
| `CANVAS_USER_ID` | `USER_ID` |
| `CANVAS_HTTP_TIMEOUT` | `HTTP_TIMEOUT` (seconds, default `60`) |
| `CANVAS_HTML` | Generates HTML pages when set to `1`/`true` (default `1` in the container) |
| `MISTRAL_API_KEY` | `MISTRAL_API_KEY` (enables OCR when set) |
| `CANVAS_NOTEBOOKLM_MONTHS` | `NOTEBOOKLM_MONTHS` (default `3`) |
| `CANVAS_NOTEBOOKLM_MAX_SOURCES` | `NOTEBOOKLM_MAX_SOURCES` (default `300`) |
| `NOTEBOOKLM_AUTH_JSON` | NotebookLM cookies, inline JSON (see [NotebookLM uploads](#notebooklm-uploads)) |
| `NOTEBOOKLM_PROFILE` | NotebookLM profile name to load cookies from |

Environment variables override values from the YAML file. The image runs as uid 1000; if the mounted output directory is owned by another user, add `--user "$(id -u):$(id -g)"`.

To build the images locally instead:

```bash
# slim (default)
docker build -t canvas-student-data-export .

# full: official full pandoc + NotebookLM support
docker build --build-arg CANVAS_VARIANT=full -t canvas-student-data-export:full .
```

## Configuration

To use the tool, you must create a `credentials.yaml` file in the project root (or provide the values through `CANVAS_*` environment variables, see [Docker](#docker)). You can also specify a different path using the `-c` or `--config` command-line option.

Create the `credentials.yaml` file with the following content:

```yaml
# The URL of your Canvas instance (e.g., https://your-school.instructure.com)
API_URL: https://example.instructure.com
# Your Canvas API token
API_KEY: <Your Canvas API token>
# Your Canvas User ID
USER_ID: 123456
# (Optional) Network timeout in seconds for Canvas requests. Default: 60
# HTTP_TIMEOUT: 60
# (Optional) A list of course IDs to skip when exporting data.
# COURSES_TO_SKIP:
#   - 12345
#   - 67890
# (Optional) Mistral API key; enables OCR of images and PDFs. Can also be set
# through the MISTRAL_API_KEY environment variable.
# MISTRAL_API_KEY: <your Mistral API key>
# (Optional) Only courses active within this many months are uploaded to
# NotebookLM. Default: 3.
# NOTEBOOKLM_MONTHS: 3
# (Optional) Maximum number of sources per NotebookLM notebook. Default: 300.
# NOTEBOOKLM_MAX_SOURCES: 300
```

### Finding Your Credentials

-   **`API_URL`**: Your institution's Canvas URL.
-   **`API_KEY`**: In Canvas, go to `Account` > `Settings`, scroll down to `Approved Integrations`, and click `+ New Access Token`.
-   **`USER_ID`**: After logging into Canvas, visit `https://<your-canvas-url>/api/v1/users/self`. Your browser will show a JSON response; find the `id` field.
-   **`COURSES_TO_SKIP`** (Optional): A list of course IDs to exclude from the export. To find a course ID, go to the course's homepage and look at the URL for the number that follows `/courses/`.

## Running the Exporter

Once your `credentials.yaml` is set up, run the script:

```bash
python export.py [options]
```

**Options:**

| Flag                    | Description                                        | Default                        |
| ----------------------- | -------------------------------------------------- | ------------------------------ |
| `-c`, `--config <path>` | Path to your YAML credentials file.                | `credentials.yaml`             |
| `-o`, `--output <path>` | Directory to store exported data.                  | `./output`                     |
| `--html`                | Generate HTML pages from the Canvas API.           | Enabled with `CANVAS_HTML=1`   |
| `--no-markdown`         | Do not convert files to Markdown.                  | Markdown conversion is on      |
| `--no-ocr`              | Do not OCR images/PDFs, even with a Mistral key.   | OCR is on when a key is set    |
| `--notebooklm`          | Upload recent courses to NotebookLM.               | Enabled with `NOTEBOOKLM_AUTH_JSON` |
| `--no-notebooklm`       | Never upload to NotebookLM.                        | Disabled                       |
| `-v`, `--verbose`       | Enable verbose output for debugging.               | Disabled                       |
| `--version`             | Show the version of the tool and exit.             | N/A                            |

**Example:**

```bash
# Run with default settings (uses ./credentials.yaml, outputs to ./output)
python export.py

# Run with a custom output directory and generate HTML pages
python export.py -o /path/to/my-canvas-backup --html

# Export and OCR everything, without touching NotebookLM
python export.py --no-notebooklm
```

After the export is complete, the tool will display a detailed summary of all the data that was successfully extracted, including counts of assignments, files, pages, converted Markdown files, OCR results and NotebookLM uploads, as well as any warnings or errors encountered.

# Markdown conversion

Every HTML/Word/PowerPoint/EPUB/text file in the export is converted to Markdown next to the original (`.md`), which makes the data easy to search and ready for tools such as NotebookLM that do not accept HTML.

- The conversion mirrors the classic pandoc batch script: a Lua filter unwraps Canvas wrapper panels, media is extracted into `attachments/<file name>/`, tables and math keep their GFM/TeX extensions, runs of `----`/`====` are collapsed, and the Markdown file inherits the source file's modification time.
- Images extracted from `.docx`/`.pptx` files are kept under `attachments/<file name>/`. If ImageMagick (`magick`/`convert`) is available, `.emf` images are converted to `.png`; otherwise they are left as `.emf`.
- HTML pages generated by this tool are converted directly (no pandoc needed, using [markdownify](https://pypi.org/project/markdownify/)); HTML that Markdown cannot express (for example complex tables) is embedded as-is.
- Files that pandoc cannot read (legacy `.doc`, …) are skipped with a note.

# Mistral OCR

Set `MISTRAL_API_KEY` (in `credentials.yaml` or the environment) to convert images and PDFs into Markdown using the [Mistral OCR API](https://docs.mistral.ai/capabilities/OCR/basic_ocr/).

- Supported inputs: `.pdf`, `.png`, `.jpg`/`.jpeg`, `.webp`, `.gif`, `.bmp`, `.tif`/`.tiff`, `.avif`, `.heic`/`.heif`.
- Each file becomes `<name>.md` next to the source; images the OCR service extracts are saved under `attachments/<name>/` and the Markdown links point at those relative paths.
- Files larger than Mistral's ~50 MB request limit are skipped with a note, and empty OCR results are not saved.
- OCR runs once per file: existing `.md` files are left alone, so re-running the exporter is cheap.

# NotebookLM uploads

The exporter can create one NotebookLM notebook per course and upload the course content automatically. Login uses **cookies only** – no browser or Playwright is installed:

1. On a machine with a browser, install the NotebookLM CLI once and sign in:
   ```bash
   pipx install "notebooklm-py[browser]"   # or: uv tool install "notebooklm-py[browser]"
   notebooklm login
   ```
2. Copy the resulting cookies to the machine/container that runs the exporter:
   ```bash
   export NOTEBOOKLM_AUTH_JSON="$(cat ~/.notebooklm/profiles/default/storage_state.json)"
   ```
   `storage_state.json` is a bearer credential for your Google account: keep it private and never commit it. In the container, pass `-e NOTEBOOKLM_AUTH_JSON`; you can also mount the file and use `NOTEBOOKLM_PROFILE` instead.

When `NOTEBOOKLM_AUTH_JSON` is set, the upload stage runs automatically after each course is exported (disable with `--no-notebooklm`, force with `--notebooklm`).

Upload rules:
- Only courses whose last activity is within `NOTEBOOKLM_MONTHS` (default 3) are uploaded. Course activity is taken from Canvas dates (assignments, pages, announcements, discussions) and the Canvas modification times preserved on downloaded files.
- One notebook per course, named `<term> - <course code> - <name>`. An existing notebook with the same title is reused.
- File types NotebookLM accepts natively are uploaded unchanged: pdf, txt, md, docx, csv, pptx, epub, audio/video and image formats (3g2, 3gp, aac, aif, aifc, aiff, amr, au, avi, m4a, mp3, mp4, mpeg, ogg, opus, ra, snd, wav, wma, avif, bmp, gif, ico, jp2, png, webp, tif, tiff, heic, heif, jpeg, jpg, jpe).
- Files outside that list are uploaded as their converted Markdown sibling (`<name>.md`) instead. A file is never uploaded twice – either the original or the Markdown, never both.
- Files are deduplicated by SHA-256 across runs (see `.notebooklm_state.json` in the output directory); re-running the exporter only uploads new content.
- NotebookLM flattens folders, so the upload title is the path relative to the course (`course files - Chapter 1 - slides.pdf`), with a numeric suffix if two files would otherwise collide.
- Generated HTML/Markdown pages, JSON exports, extracted `attachments/` and empty/no-information files are never uploaded, keeping the notebook focused on real course content.
- Each notebook is capped at `NOTEBOOKLM_MAX_SOURCES` (default 300) sources, counting what is already there; extra files are reported and skipped.

# Contribute

I would love to see this script's functionality expanded and improved! I welcome all pull requests 🙂  
Thank you!
