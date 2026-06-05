import base64
import mimetypes
import re
from pathlib import Path
from typing import Dict, Any, Optional

from imports.files.store import FileStore

# Maximum number of images to embed per file_read_lines call
MAX_IMAGES_PER_RESPONSE = 5

# Regex to find Markdown image syntax: ![alt](path)
_IMAGE_PATTERN = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')


def _resolve_image_path(raw_path: str, media_dir: Optional[Path]) -> Optional[Path]:
    """Resolve a Markdown image reference to an absolute Path.

    Pandoc writes paths like 'media/image1.png' or absolute paths.
    We resolve relative paths against the *media_dir* if provided.
    """
    p = Path(raw_path)
    if p.is_absolute() and p.exists():
        return p
    if media_dir:
        candidate = media_dir / p
        if candidate.exists():
            return candidate
        # Pandoc sometimes writes paths like '<hash>/media/image1.png'
        # Try resolving relative to media_dir's parent
        candidate2 = media_dir.parent / p
        if candidate2.exists():
            return candidate2
    return None


def read_file_lines(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    file_name = args.get('file_name')
    if not file_name:
        return {"status": "error", "message": "file_name is required."}

    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return {"status": "error", "message": "file_name must not contain path separators or traversal marks."}

    start_id = args.get('start_id', 1)
    count = args.get('count', 50)

    try:
        start_id = int(start_id) if start_id is not None else 1
    except (TypeError, ValueError):
        start_id = 1

    try:
        count = int(count) if count is not None else 50
    except (TypeError, ValueError):
        count = 50

    count = min(count, 50)
    if count < 0:
        count = 0
    if start_id < 1:
        start_id = 1

    file_path = file_store.get_physical_path(user_id, file_name)
    if not file_path:
        return {"status": "error", "message": f"File '{file_name}' not found or access denied."}

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        return {"status": "error", "message": f"Error reading file: {str(e)}"}

    total_lines = len(lines)
    start_idx = start_id - 1
    end_idx = min(start_idx + count, total_lines)

    slice_lines = lines[start_idx:end_idx]

    # Resolve media directory for image injection
    media_dir: Optional[Path] = None
    try:
        media_dir = file_store.get_media_dir(user_id, file_name)
    except Exception:
        pass

    output_lines = []
    embedded_images = []
    image_limit_reached = False
    actual_end_idx = end_idx  # may be adjusted if image limit hit

    for rel_idx, line in enumerate(slice_lines):
        line_number = start_id + rel_idx
        clean_line = line.rstrip('\n').replace('\r', '')
        output_lines.append({"line_number": line_number, "content": clean_line})

        # Scan for image references in this line
        for m in _IMAGE_PATTERN.finditer(clean_line):
            if len(embedded_images) >= MAX_IMAGES_PER_RESPONSE:
                image_limit_reached = True
                actual_end_idx = line_number - 1  # stop just before this line
                # Remove the lines from this one onwards from output
                output_lines = output_lines[:-1]
                break

            alt = m.group(1)
            raw_path = m.group(2).strip()
            resolved = _resolve_image_path(raw_path, media_dir)
            if not resolved:
                continue  # image file not found — skip silently

            ext = resolved.suffix.lower()
            mime, _ = mimetypes.guess_type(str(resolved))
            if not mime:
                # Fallback by extension
                _ext_map = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                            '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
                            '.tiff': 'image/tiff', '.tif': 'image/tiff', '.svg': 'image/svg+xml'}
                mime = _ext_map.get(ext, 'image/png')

            try:
                img_bytes = resolved.read_bytes()
            except Exception:
                continue

            b64 = base64.b64encode(img_bytes).decode('utf-8')
            data_uri = f"data:{mime};base64,{b64}"

            embedded_images.append({
                "line_number": line_number,
                "alt": alt,
                "path": raw_path,
                "type": "image_url",
                "image_url": {"url": data_uri},
            })

        if image_limit_reached:
            break

    eof = actual_end_idx >= total_lines

    result: Dict[str, Any] = {
        "status": "success",
        "file_name": file_name,
        "total_lines": total_lines,
        "start_id": start_id,
        "end_id": actual_end_idx,
        "eof": eof,
        "lines": output_lines,
    }

    if embedded_images:
        result["images"] = embedded_images

    if image_limit_reached:
        result["image_limit_reached"] = True
        result["note"] = (
            f"Image per-message limit ({MAX_IMAGES_PER_RESPONSE}) reached. "
            f"Read stopped at line {actual_end_idx}. "
            f"Call file_read_lines again starting from line {actual_end_idx + 1} to continue."
        )

    return result
