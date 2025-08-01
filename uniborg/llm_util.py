import llm
from pathlib import Path
import os

# --- LLM-Specific Shared Constants and Utilities ---

MIME_TYPE_MAP = {
    ##
    #: Audio formats
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".m4a": "audio/aac",
    # ".mp3": "audio/mpeg",
    # ".wav": "audio/wav",
    # ".flac": "audio/flac",
    ##
    #: Video formats
    ".mp4": "video/mp4",
    # ".mov": "video/quicktime",
    # ".webm": "video/webm",
    # ".mkv": "video/x-matroska",
    ##
}

def create_attachments_from_dir(directory: Path) -> list[llm.Attachment]:
    """
    Scans a directory for files and creates a list of llm.Attachment objects.

    It uses a predefined MIME_TYPE_MAP to handle common audio/video types
    by reading their binary content directly. For all other file types (e.g.,
    images), it creates an attachment by path, allowing the `llm` library
    to infer the type.

    Args:
        directory: The Path object of the directory to scan.

    Returns:
        A list of llm.Attachment objects, ready to be sent to a model.
    """
    attachments = []
    if not directory.is_dir():
        return attachments

    for filepath in directory.iterdir():
        if not filepath.is_file():
            continue

        lower_filename = filepath.name.lower()
        handled_explicitly = False

        for extension, mime_type in MIME_TYPE_MAP.items():
            if lower_filename.endswith(extension):
                try:
                    with open(filepath, "rb") as f:
                        content = f.read()
                    attachments.append(
                        llm.Attachment(content=content, type=mime_type)
                    )
                    handled_explicitly = True
                    break  # Move to the next file
                except IOError as e:
                    print(f"Error reading file {filepath} for explicit MIME type: {e}")
                    # Mark as handled to prevent fallback, but skip adding it
                    handled_explicitly = True
                    break

        if not handled_explicitly:
            # Fallback for images and other file types.
            # llm.Attachment(path=...) is smart enough to handle these.
            attachments.append(llm.Attachment(path=str(filepath)))

    return attachments
