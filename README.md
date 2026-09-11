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

Data is saved in JSON (and optionally HTML) format and organized into folders by academic term and course.

Example output structure:
- Fall 2023
  - CS 101
    - announcements/
      - First Announcement/
        - announcement_1.html
      - announcement_list.html
    - assignments/
      - Sample Assignment/
        - assignment.html
        - submission.html
        - attempts/
          - attempt_1.html
      - assignment_list.html
    - course files/
      - file_1.docx
      - file_2.png
    - discussions/
      - Sample Discussion/
        - discussion_1.html
      - discussion_list.html
    - modules/
      - Sample Module/
        - Sample Assignment.html
        - Sample Discussion.html
        - Sample Page.html
        - Sample Quiz.html
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

# Getting Started

## Dependencies
- Python 3.8 or newer

Install the Python dependencies:
```bash
pip install -r requirements.txt
```

## Docker

Pre-built images are published to the GitHub Container Registry, so you do not need Python or anything else installed locally.

```bash
docker run --rm \
  -v "$PWD/credentials.yaml:/config/credentials.yaml:ro" \
  -v "$PWD/output:/data" \
  ghcr.io/ovlerfork/canvas-student-data-export:latest
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
| `CANVAS_HTML` | Generates HTML pages when set to `1`/`true` (default `1` in the container) |

Environment variables override values from the YAML file. The image runs as uid 1000; if the mounted output directory is owned by another user, add `--user "$(id -u):$(id -g)"`.

To build the image locally instead:

```bash
docker build -t canvas-student-data-export .
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
# (Optional) A list of course IDs to skip when exporting data.
# COURSES_TO_SKIP:
#   - 12345
#   - 67890
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
| `-v`, `--verbose`       | Enable verbose output for debugging.               | Disabled                       |
| `--version`             | Show the version of the tool and exit.             | N/A                            |

**Example:**

```bash
# Run with default settings (uses ./credentials.yaml, outputs to ./output)
python export.py

# Run with a custom output directory and generate HTML pages
python export.py -o /path/to/my-canvas-backup --html
```

After the export is complete, the tool will display a detailed summary of all the data that was successfully extracted, including counts of assignments, files, and pages, as well as any warnings or errors encountered.

# Contribute

I would love to see this script's functionality expanded and improved! I welcome all pull requests 🙂  
Thank you!
