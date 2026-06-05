"""file_replace_lines — replace lines starting at a given position.

If the target file has origin='loaded', it is automatically duplicated to
a new 'created' copy before the edit is applied.
"""
from typing import Dict, Any, List

from imports.files.store import FileStore


def replace_lines(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    file_name = args.get('file_name', '').strip()
    line_id = args.get('line_id')
    lines: List[str] = args.get('lines', [])

    if not file_name:
        return {"status": "error", "message": "file_name is required."}
    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return {"status": "error", "message": "file_name must not contain path separators or traversal marks."}
    if line_id is None:
        return {"status": "error", "message": "line_id is required."}
    if not isinstance(lines, list):
        return {"status": "error", "message": "lines must be a list of strings."}
    if not lines:
        return {"status": "error", "message": "lines list is empty — nothing to replace."}

    try:
        line_id = int(line_id)
    except (TypeError, ValueError):
        return {"status": "error", "message": "line_id must be an integer."}
    if line_id < 1:
        return {"status": "error", "message": "line_id must be >= 1 (1-based)."}

    record = file_store.get_record(user_id, file_name)
    if not record:
        return {"status": "error", "message": f"File '{file_name}' not found or access denied."}

    notice = ""
    actual_name = file_name

    # Duplicate loaded files before editing
    if record.get('origin') == 'loaded':
        dup = file_store.duplicate_to_created(user_id, file_name)
        if 'error' in dup:
            return {"status": "error", "message": f"Error duplicating file: {dup['error']}"}
        actual_name = dup['real_name']
        notice = (
            f"Note: '{file_name}' is a loaded (read-only) file and cannot be edited directly. "
            f"An editable copy '{actual_name}' has been created and the operation was applied to it.\n\n"
        )

    file_path = file_store.get_physical_path(user_id, actual_name)
    if not file_path:
        return {"status": "error", "message": f"Error: Physical file for '{actual_name}' not found."}

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            existing = f.readlines()
    except Exception as e:
        return {"status": "error", "message": f"Error reading file: {e}"}

    # Ensure existing lines don't have trailing newlines so we can safely re-add them
    existing = [l.rstrip('\n').rstrip('\r') for l in existing]

    start_idx = line_id - 1  # convert to 0-based

    if start_idx >= len(existing):
        # line_id beyond EOF → just append
        existing.extend(str(l) for l in lines)
    else:
        # Replace slice [start_idx : start_idx + len(lines)]
        replacement = [str(l) for l in lines]
        existing[start_idx: start_idx + len(replacement)] = replacement

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            for l in existing:
                f.write(l + '\n')
    except Exception as e:
        return {"status": "error", "message": f"Error writing file: {e}"}

    file_store.update_timestamp(user_id, actual_name)

    return {
        "status": "success",
        "message": (
            f"{notice}Replaced {len(lines)} line(s) starting at line {line_id} in '{actual_name}'.\n"
            f"File now has {len(existing)} line(s)."
        ),
        "file_name": actual_name,
        "lines_replaced": len(lines),
        "start_line": line_id,
        "total_lines": len(existing),
        "duplicated": record.get('origin') == 'loaded'
    }
