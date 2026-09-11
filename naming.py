# Shared filename/path helpers for the JSON and HTML exporters.

import os
import re
import string
import unicodedata

# Max PATH length is 260 characters on Windows. 70 is just an estimate for a
# reasonable max folder name to prevent the chance of reaching the limit.
# Applies to modules, assignments, announcements, and discussions.
# If a folder exceeds this limit, a "-" will be added to the end to indicate it
# was shortened ("..." not valid)
MAX_FOLDER_NAME_SIZE = 70


def makeValidFilename(input_str):
    if(not input_str):
        return input_str

    # Normalize Unicode and whitespace
    input_str = unicodedata.normalize('NFKC', input_str)
    input_str = input_str.replace("\u00A0", " ") # NBSP to space
    input_str = re.sub(r"\s+", " ", input_str)

    # Remove invalid characters
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    input_str = input_str.replace("+"," ") # Canvas default for spaces
    input_str = input_str.replace(":","-")
    input_str = input_str.replace("/","-")
    input_str = "".join(c for c in input_str if c in valid_chars)

    # Remove leading and trailing whitespace
    input_str = input_str.lstrip().rstrip()

    # Remove trailing periods
    input_str = input_str.rstrip(".")

    return input_str


def makeValidFolderPath(input_str):
    # Normalize Unicode and whitespace
    input_str = unicodedata.normalize('NFKC', input_str)
    input_str = input_str.replace("\u00A0", " ") # NBSP to space
    input_str = re.sub(r"\s+", " ", input_str)

    # Remove invalid characters
    valid_chars = "-_.()/ %s%s" % (string.ascii_letters, string.digits)
    input_str = input_str.replace("+"," ") # Canvas default for spaces
    input_str = input_str.replace(":","-")
    input_str = "".join(c for c in input_str if c in valid_chars)

    # Remove leading and trailing whitespace, separators
    input_str = input_str.lstrip().rstrip().strip("/").strip("\\")

    # Remove trailing periods
    input_str = input_str.rstrip(".")

    # Replace path separators with OS default
    input_str=input_str.replace("/",os.sep)

    return input_str


def shortenFileName(string, shorten_by) -> str:
    if (not string or shorten_by <= 0):
        return string

    # Shorten string by specified value + 1 for "-" to indicate incomplete file name (trailing periods not allowed)
    string = string[:len(string)-(shorten_by + 1)]

    string = string.rstrip().rstrip(".").rstrip("-")
    string += "-"

    return string
