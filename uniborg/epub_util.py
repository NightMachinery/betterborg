from bs4 import XMLParsedAsHTMLWarning
import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from bs4 import BeautifulSoup

from pynight.common_icecream import ic
import io
import re
from epub_sum_lib.epubsplit import SplitEpub
from epub_sum_lib.chunking import semantic_chunking

MAX_EBOOK_CHUNK_CHARS = 92000
MIN_EBOOK_CHUNK_CHARS = 5000


def _extract_text_from_html(html_content: str) -> str:
    """Extracts clean text from an HTML string."""
    soup = BeautifulSoup(html_content, "html.parser")
    # Clean up spacing by replacing multiple newlines with a single one
    return re.sub(r"\n\s*\n", "\n\n", soup.get_text(separator="\n", strip=True))


def chunk_epub(epub_path: str) -> list[str]:
    """
    Chunks an EPUB file using a sophisticated two-stage process:

    1.  **Structural Splitting**: The EPUB is first divided into sections
        based on its Table of Contents (chapters).
    2.  **Semantic Chunking**: If a chapter's text is too long, it is
        further divided into smaller, semantically coherent chunks.

    Args:
        epub_path: Path to the EPUB file.

    Returns:
        A list of text chunks.
    """
    final_chunks = []
    try:
        # Open the EPUB file in memory
        with open(epub_path, "rb") as f:
            epub_io = io.BytesIO(f.read())

        splitter = SplitEpub(epub_io)
        lines = splitter.get_split_lines()

        # Group split lines into sections based on TOC entries
        sections = []
        current_section_lines = []
        for i, line in enumerate(lines):
            # A TOC entry marks the beginning of a new section
            if line.get("toc"):
                if current_section_lines:
                    sections.append(current_section_lines)
                current_section_lines = [i]
            # If no TOC, append to the current section
            elif current_section_lines:
                current_section_lines.append(i)

        if current_section_lines:
            sections.append(current_section_lines)

        # If no TOC was found, treat the entire book as one section
        if not sections:
            sections.append(list(range(len(lines))))

        # Process sections and group small ones together
        accumulated_text = ""
        for section_linenums in sections:
            # Use get_split_files to extract the raw HTML for the section
            # This is a bit of a hack, but it reuses the existing file splitting logic
            # to get the precise content of each chapter.
            files_in_section = splitter.get_split_files(section_linenums)

            section_text_parts = []
            for _, _, _, filedata in files_in_section:
                section_text_parts.append(_extract_text_from_html(filedata))

            section_text = "\n\n".join(section_text_parts).strip()

            if not section_text:
                continue

            # Add section to accumulated text
            if accumulated_text:
                accumulated_text += "\n\n" + section_text
            else:
                accumulated_text = section_text

            # If accumulated text is too long, apply semantic chunking
            if len(accumulated_text) > MAX_EBOOK_CHUNK_CHARS:
                # ic(len(accumulated_text))
                
                semantic_chunks = semantic_chunking(
                    accumulated_text, max_chunk_size=MAX_EBOOK_CHUNK_CHARS
                )
                
                # Handle merging small last chunk with accumulated_text
                for i, chunk in enumerate(semantic_chunks):
                    if i == len(semantic_chunks) - 1 and len(chunk) < MIN_EBOOK_CHUNK_CHARS:
                        # Keep the last small chunk in accumulated_text for next iteration
                        accumulated_text = chunk
                    else:
                        final_chunks.append(chunk)
                        if i == len(semantic_chunks) - 1:
                            accumulated_text = ""
                            
            # If accumulated text is long enough, finalize it
            elif len(accumulated_text) >= MIN_EBOOK_CHUNK_CHARS:
                # ic(len(accumulated_text))
                final_chunks.append(accumulated_text)
                accumulated_text = ""

        # Handle any remaining accumulated text
        if accumulated_text:
            if len(accumulated_text) > MAX_EBOOK_CHUNK_CHARS:
                semantic_chunks = semantic_chunking(
                    accumulated_text, max_chunk_size=MAX_EBOOK_CHUNK_CHARS
                )
                final_chunks.extend(semantic_chunks)
            else:
                final_chunks.append(accumulated_text)

    except Exception as e:
        print(f"Error chunking EPUB file {epub_path}: {e}")
        # Optionally, re-raise or return a specific error message
        # For now, we'll return any chunks processed so far.

    return final_chunks
