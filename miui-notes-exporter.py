"""
MIUI Notes Backup Converter
============================
Converts a MIUI Notes `.bak` file into a collection of Markdown files,
preserving folder structure, timestamps, and image attachments.

Usage:
    python miui_notes_converter.py <path/to/Notes(com.miui.notes).bak>
"""

import argparse
import datetime
import os
import re
import shutil
import tarfile
import warnings
from dataclasses import dataclass
from pathlib import Path

from bs4 import MarkupResemblesLocatorWarning
from markdownify import markdownify as md

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BAK_HEADER_SIZE   = 64       # Non-standard header bytes to skip in the .bak file
COPY_CHUNK_SIZE   = 65536    # 64 KB — used for streaming file copies
ATTACHMENT_FNAME_LEN = 40    # Fixed length of an image filename inside note content

OUTPUT_NOTES_DIR       = Path("Notes")
OUTPUT_ATTACHMENTS_DIR = OUTPUT_NOTES_DIR / ".attachments"
FALLBACK_NOTE_DIR      = "Miscellaneous"

# Binary markers used in the proprietary format


MARKER_FILE_START          = 0x32   #Found at the start of the file

#Group Markers
MARKER_GROUP_START         = 0x0A
MARKER_GROUP_ID            = 0x12
MARKER_GROUP_UNKNOWN_1     = 0x18
MARKER_GROUP_UNKNOWN_2     = 0x20
MARKER_GROUP_CREATION_TIME = 0x28
MARKER_GROUP_LAST_MOD_TIME = 0x30
MARKER_GROUP_UNKNOWN_3     = 0x38
MARKER_GROUP_NAME          = 0x4A


#Note Makers
MARKER_NOTE_START   = 0x12
MARKER_NOTE_ID      = 0x12
MARKER_NOTE_ALARM   = 0x18

MARKER_NOTE_UNKNOWN_1      = 0x20
MARKER_NOTE_CREATION_TIME  = 0x28
MARKER_NOTE_LAST_MOD_TIME  = 0x30
MARKER_NOTE_UNKNOWN_2      = 0x38
MARKER_NOTE_CONTENT        = 0x42
MARKER_NOTE_LEN_TO_FOOTER  = 0x4A
MARKER_IMAGE_FOOTER        = 0x4A
MARKER_NOTE_GROUP          = 0x52
MARKER_NOTE_UNKNOWN_3      = 0x60
MARKER_NOTE_BACKGROUND_ID  = 0x68
MARKER_NOTE_TITLE          = 0x72
MARKER_NOTE_TYPE           = 0x7A
MARKER_NOTE_MINDMAP_CODE_1 = b'\x82\x01'
MARKER_NOTE_MINDMAP_CODE_2 = b'\x8a\x01'

#Image markers
MARKER_INLINE_IMAGE        = b'\xe2\x98\xba\x20'  # ☺ + space, signals an embedded image ref
MARKER_IMAGE_EXT_LEN       = 0x0A
MARKER_IMAGE_CREATION_TIME = 0x10
MARKER_IMAGE_LAST_MOD_TIME = 0x18
MARKER_IMAGE_FILENAME      = 0x22


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class ConverterConfig:
    linux_filenames: bool
    keep_html: bool


@dataclass
class NoteGroup:
    group_id: str
    title: str


@dataclass
class Note:
    note_id: str
    title: str
    note_type: str
    content: bytes
    group_name: str
    creation_time: int        # millisecond timestamp
    last_mod_time: int        # millisecond timestamp
    alarm_timestamp: int = 0  # millisecond timestamp 
    background_id: int = 0

@dataclass
class Image:
    filename: Path
    extension: str

# ---------------------------------------------------------------------------
# Binary parsing utilities
# ---------------------------------------------------------------------------

def varint_decode(data: bytes, pos: int) -> tuple[int,int]:
    """
    Decode a Protocol Buffer-style variable-length integer from `data` at `pos`.
    Returns (decoded_value, new_position).
    """
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        shift += 7
        if not (byte & 0x80):
            break
    return result, pos


def expect_byte(data: bytes, pos: int, expected: int) -> int:
    """Assert that the byte at `pos` equals `expected`, then advance past it."""
    if data[pos] != expected:
        raise ValueError(
            f"Expected byte 0x{expected:02X} at position {pos}, "
            f"got 0x{data[pos]:02X}"
        )
    return pos + 1

def expect_bytes(data: bytes, pos: int, expected: bytes) -> int:
    """Assert that the bytes at `pos` match `expected`, then advance past them."""
    length = len(expected)
    if data[pos : pos + length] != expected:
        raise ValueError(
            f"Expected {expected!r} at position {pos}, "
            f"got {data[pos : pos + length]!r}"
        )
    return pos + length

def read_length_prefixed_data(data: bytes, pos: int) -> tuple[bytes, int]:
    """Read a varint-prefixed blob. Returns (blob, new_position)."""
    length, pos = varint_decode(data, pos)
    return data[pos : pos + length], pos + length


def read_utf8_field(data: bytes, pos: int) -> tuple[str, int]:
    """Read a varint-prefixed UTF-8 string. Returns (string, new_position)."""
    raw, pos = read_length_prefixed_data(data, pos)
    return raw.decode("utf-8", errors="replace"), pos

def skip_length_prefixed_data(data: bytes, pos: int) -> int:
    """Skip over a varint-prefixed blob without reading it."""
    length, pos = varint_decode(data, pos)
    return pos + length

def ms_timestamp_to_datetime(ms: int) -> datetime.datetime:
    """Convert a millisecond Unix timestamp to a local datetime."""
    return datetime.datetime.fromtimestamp(ms // 1000)

# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
def add_extension(filename: Path, extension: str, work_dir: Path):
    """Rename `filename` to `filename`+`extension`"""
    old_path = work_dir / filename
    new_path = work_dir / (filename.name + extension)
    old_path.rename(new_path)


def replace_inline_images(content: bytes, extensions: list[str]) -> bytes:
    """
    The MIUI format embeds image references as a 4-byte sentinel (☺ + space)
    followed by a 40-character filename and a 7-byte footer.
    This replaces each such reference with a proper <img> HTML tag so that
    markdownify can later convert it correctly.
    """
    output = bytearray()
    pos = 0
    sentinel = MARKER_INLINE_IMAGE
    sentinel_len = len(sentinel)
    image_counter=0

    while pos < len(content):
        if content[pos : pos + sentinel_len] == sentinel:
            if image_counter >= len(extensions):
                print("Warning! Found more images inside a note than image_footers")
                pos += sentinel_len + ATTACHMENT_FNAME_LEN +7 #skips embedded note
                continue
            
            pos += sentinel_len
            image_filename = content[pos : pos + ATTACHMENT_FNAME_LEN]
            output += b"<img src='../.attachments/"
            output += image_filename
            output += bytes(extensions[image_counter], "utf-8")
            output += b"'>"
            pos += ATTACHMENT_FNAME_LEN + 7  # skip filename + 7-byte footer (the footer is: "<0\><\>"")
            image_counter += 1
        else:
            output.append(content[pos])
            pos += 1

    return bytes(output)

def parse_image(data: bytes, pos: int) -> tuple[Image,int]:
    pos += 1
    end_offset, pos = varint_decode(data, pos)
    image_footer_end_pos = pos + end_offset
    pos = expect_byte(data,pos, MARKER_IMAGE_EXT_LEN)

    ext_len, pos = varint_decode(data,pos) #extension's length including "image/"
    ext_len -= len("image/")

    pos = expect_bytes(data,pos,b"image/")
    extension = bytearray()
    for i in range(ext_len):
        extension.append(data[pos+i])
    
    pos += ext_len
    extension = "."+str(extension, "utf-8", errors="ignore")
    
    pos = expect_byte(data,pos,MARKER_IMAGE_CREATION_TIME)
    _, pos = varint_decode(data,pos)
    pos = expect_byte(data,pos, MARKER_IMAGE_LAST_MOD_TIME)
    _, pos = varint_decode(data,pos)

    pos = expect_byte(data,pos,MARKER_IMAGE_FILENAME)
    image_filename, pos = read_utf8_field(data,pos)
    image_filename = Path(image_filename)

    pos = image_footer_end_pos
    image = Image(image_filename, extension)
    return image, pos

def parse_image_footers(data: bytes, pos: int, output_attachments_dir: Path) -> tuple[bytes, int]:
    """Parse each image footer found.
     Rename each image to add its extension
     """
    extensions = []
    while data[pos] == MARKER_IMAGE_FOOTER:
        image, pos = parse_image(data,pos)
        add_extension(image.filename, image.extension, output_attachments_dir)
        extensions.append(image.extension)
    return extensions,pos


# ---------------------------------------------------------------------------
# .bak extraction
# ---------------------------------------------------------------------------

def strip_bak_header(bak_path: Path, tar_path: Path) -> None:
    """
    The .bak file is a standard tar archive prefixed with 64 non-standard bytes.
    This copies everything after those bytes into a proper .tar file.
    """
    with bak_path.open("rb") as src, tar_path.open("wb") as dst:
        src.seek(BAK_HEADER_SIZE)
        shutil.copyfileobj(src, dst, COPY_CHUNK_SIZE)

def extract_bak(bak_path: Path, extract_to: Path) -> tuple[bytes, Path]:
    """
    Strips the .bak header, extracts the tar archive, and returns:
      - the raw bytes of the notes database
      - the path to the extracted image attachments
    Cleans up the temporary tar file afterwards.
    """
    tar_path = bak_path.with_suffix(".tar")
    strip_bak_header(bak_path, tar_path)

    try:
        with tarfile.open(tar_path, "r") as tar:
            tar.extractall(path=extract_to)
    finally:
        tar_path.unlink(missing_ok=True)

    db_path  = extract_to / "apps/com.miui.notes/miui_bak/_tmp_bak"
    att_path = extract_to / "apps/com.miui.notes/miui_att"

    raw_data = db_path.read_bytes()
    return raw_data, att_path

# ---------------------------------------------------------------------------
# Format parsing
# ---------------------------------------------------------------------------

def parse_groups(data: bytes, pos: int) -> tuple[list[NoteGroup], int]:
    """
    Parse all note group (folder) definitions from the binary data.
    Groups appear as a sequence of 0x0A-prefixed records before the notes.
    """
    groups = []

    while pos < len(data) and data[pos] == MARKER_GROUP_START:
        pos += 1
        group_length, pos = varint_decode(data, pos)
        group_end = pos + group_length

        pos = expect_byte(data, pos, MARKER_GROUP_ID)
        group_id, pos = read_utf8_field(data, pos)

        pos = expect_byte(data,pos,MARKER_GROUP_UNKNOWN_1)
        pos = skip_length_prefixed_data(data,pos)

        pos = expect_byte(data,pos,MARKER_GROUP_UNKNOWN_2)
        pos = skip_length_prefixed_data(data,pos)
        
        pos = expect_byte(data,pos,MARKER_GROUP_CREATION_TIME)
        creation_time, pos = varint_decode(data,pos)
        
        pos = expect_byte(data,pos,MARKER_GROUP_LAST_MOD_TIME)
        mod_time, pos = varint_decode(data,pos)

        pos = expect_byte(data,pos, MARKER_GROUP_UNKNOWN_3)
        pos = skip_length_prefixed_data(data,pos)
        
        pos = expect_byte(data,pos, MARKER_GROUP_NAME)

        group_title, pos = read_utf8_field(data, pos)

        if pos != group_end:
            print(f"Warning: group length mismatch (expected end={group_end}, got pos={pos}). Skipping.")
            pos = group_end
        else:
            groups.append(NoteGroup(group_id=group_id, title=group_title))

    return groups, pos

def parse_note(data: bytes, pos: int, output_attachments_dir: Path) -> tuple[Note, int]:
    """
    Parse a single note record starting just after the 0x12 marker byte.
    Returns the populated Note object and the position after the record.
    """
    note_total_len, pos = varint_decode(data, pos)
    note_end = pos + note_total_len

    pos = expect_byte(data, pos, MARKER_NOTE_ID)
    note_id, pos = read_utf8_field(data, pos)

    pos = expect_byte(data, pos, MARKER_NOTE_ALARM)
    alarm_timestamp, pos = varint_decode(data, pos)

    pos = expect_byte(data, pos, MARKER_NOTE_UNKNOWN_1)
    pos = skip_length_prefixed_data(data,pos)

    pos = expect_byte(data, pos, MARKER_NOTE_CREATION_TIME)
    creation_time, pos = varint_decode(data, pos)

    pos = expect_byte(data, pos, MARKER_NOTE_LAST_MOD_TIME)
    last_mod_time, pos = varint_decode(data, pos)

    pos = expect_byte(data, pos, MARKER_NOTE_UNKNOWN_2)
    pos = skip_length_prefixed_data(data,pos)

    pos = expect_byte(data, pos, MARKER_NOTE_CONTENT)
    raw_content, pos = read_length_prefixed_data(data, pos)

    
    pos = expect_byte(data, pos, MARKER_NOTE_LEN_TO_FOOTER)
    pos = skip_length_prefixed_data(data, pos)   # skip note middle blob

    # Deal with image footers before the actual footer
    if data[pos] == MARKER_IMAGE_FOOTER:
        extensions, pos = parse_image_footers(data, pos, output_attachments_dir)
        raw_content = replace_inline_images(raw_content, extensions)
    
    # Optional group name field
    group_name = ""
    if data[pos] == MARKER_NOTE_GROUP:
        pos += 1
        group_name, pos = read_utf8_field(data, pos)

    pos = expect_byte(data, pos, MARKER_NOTE_UNKNOWN_3)
    pos = skip_length_prefixed_data(data,pos)

    pos = expect_byte(data, pos, MARKER_NOTE_BACKGROUND_ID)
    background_id, pos = varint_decode(data, pos)

    pos = expect_byte(data, pos, MARKER_NOTE_TITLE)
    title_raw, pos = read_length_prefixed_data(data, pos)
    note_title = title_raw.decode("utf-8", errors="replace") if title_raw else note_id

    pos = expect_byte(data, pos, MARKER_NOTE_TYPE)
    note_type, pos = read_utf8_field(data, pos)

    pos = expect_bytes(data, pos, MARKER_NOTE_MINDMAP_CODE_1)
    pos = skip_length_prefixed_data(data, pos)

    pos = expect_bytes(data, pos, MARKER_NOTE_MINDMAP_CODE_2)
    pos = skip_length_prefixed_data(data, pos)

    return Note(
        note_id=note_id,
        title=note_title,
        note_type=note_type,
        content=raw_content,
        group_name=group_name,
        creation_time=creation_time,
        last_mod_time=last_mod_time,
        alarm_timestamp=alarm_timestamp,
        background_id=background_id,
    ), pos

def parse_all_notes(data: bytes, output_attachments_dir: Path) -> tuple[list[NoteGroup], list[Note]]:
    """
    Entry point for the binary parser. Validates the file magic bytes,
    then parses groups and notes in sequence.
    """
    pos = expect_byte(data, 0, MARKER_FILE_START)
    database_len, pos = varint_decode(data,pos) # Length of the database containing regular notes

    groups, pos = parse_groups(data, pos)

    notes = []
    while pos < database_len and data[pos] == MARKER_NOTE_START:
        pos += 1
        note, pos = parse_note(data, pos, output_attachments_dir)
        notes.append(note)

    return groups, notes

# ---------------------------------------------------------------------------
# Output utils
# ---------------------------------------------------------------------------

def sanitize_filename_windows(name: str) -> str:
    """Replace characters that are illegal in windows filenames."""
    name = re.sub(r'[/\\<>:"|?*\x00-\x1f]', "_", name)
    return name.strip()

def sanitize_filename_linux(name: str) -> str:
    """Replace characters that are illegal in linux filenames."""
    name = name.replace("/","_").replace("\\", "_")
    return name.strip()

def note_to_markdown(note: Note, keep_html: bool) -> str:
    """Convert a Note's HTML content to Markdown, with a creation-date header."""
    created = ms_timestamp_to_datetime(note.creation_time)
    header = f"Note created on: {created}\n\n"

    html_content = note.content.decode("utf-8", errors="replace")

    if keep_html:
        return header + html_content + "\n"
    
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
        body = md(html_content)

    return header + body + "\n"

def write_note(note: Note, output_dir: Path, fallback_note_dir: Path, config: ConverterConfig) -> None:
    """Write a single note to a .md file and set its modification timestamp."""
    if note.note_type != "common":
        print(f"Skipping unsupported note type '{note.note_type}' (Title={note.title})")
        return

    folder = output_dir / (note.group_name if note.group_name else fallback_note_dir)
    folder.mkdir(parents=True, exist_ok=True)

    extension = ".md"
    if config.keep_html:
        extension = ".html"

    if config.linux_filenames:
        filename = sanitize_filename_linux(note.title + extension)
    else:
        filename = sanitize_filename_windows(note.title + extension)
    
    filepath = folder / filename

    markdown = note_to_markdown(note, config.keep_html)
    filepath.write_text(markdown, encoding="utf-8")

    mod_time = note.last_mod_time / 1000
    os.utime(filepath, (mod_time, mod_time))


def write_all_notes(notes: list[Note], groups: list[NoteGroup], output_dir: Path, fallback_note_dir: Path, config: ConverterConfig) -> None:
    """Create group subdirectories and write every note."""
    # Pre-create directories for all known groups
    for group in groups:
        (output_dir / group.title).mkdir(parents=True, exist_ok=True)

    (output_dir / fallback_note_dir).mkdir(parents=True, exist_ok=True)

    for note in notes:
        write_note(note, output_dir,fallback_note_dir, config)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="MIUI Notes .bak file converter")
    parser.add_argument("filename", help="Path to your `Notes(com.miui.notes).bak` file")
    parser.add_argument("--keep-html", action="store_true", help="Output notes in HTML instead of converting to Markdown")
    parser.add_argument("--linux-filenames", action="store_true", help="Keep Windows-unsafe characters in note titles")

    args = parser.parse_args()

    config = ConverterConfig(
        linux_filenames = args.linux_filenames,
        keep_html = args.keep_html
    )

    bak_path = Path(args.filename)
    if not bak_path.is_file():
        raise SystemExit(f"Error: file not found: {bak_path}")

    work_dir = Path(".")

    print(f"Extracting {bak_path.name}...")
    raw_data, attachments_src = extract_bak(bak_path, work_dir)

    print(f"Moving Attachments...")
    OUTPUT_NOTES_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Copy image attachments into the output folder
    if attachments_src.is_dir():
        for image in attachments_src.iterdir():
            if image.is_file():
                shutil.copy2(image, OUTPUT_ATTACHMENTS_DIR / image.name)
    else:
        print(f"Error! Unable to retrieve note attachments: {attachments_src} is not a Directory\nThe backup format has probably changed")

    print("Parsing notes...")
    try:
        groups, notes = parse_all_notes(raw_data, OUTPUT_ATTACHMENTS_DIR)
    except ValueError as exc:
        raise SystemExit(f"Parse error: {exc}") from exc
    finally:
        shutil.rmtree(work_dir / "apps", ignore_errors=True)

        

    print(f"Found {len(groups)} group(s) and {len(notes)} note(s).")

    print(f"Writing Notes...")
    write_all_notes(notes, groups, OUTPUT_NOTES_DIR, FALLBACK_NOTE_DIR, config)
    
    

    print(f"Done. Notes written to '{OUTPUT_NOTES_DIR}/'.")


if __name__ == "__main__":
    main()
