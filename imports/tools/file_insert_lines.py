# TinyChat (c) 2026 WarWar <somethingstrenge@gmail.com>
# This file is part of TinyChat.
# TinyChat is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License v3.

"""file_insert_lines — insert lines at a given position without overwriting.

Lines are inserted *before* the line at ``line_id``, shifting the existing
content down.  Use ``line_id`` equal to ``total_lines + 1`` (or any value
beyond EOF) to append at the end of the file.

If the target file has origin='loaded', it is automatically duplicated to
a new 'created' copy before the edit is applied.
"""
from typing import Dict, Any, List

from imports.files.store import FileStore


def insert_lines(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
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
        return {"status": "error", "message": "lines list is empty — nothing to insert."}

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

    # Strip trailing newlines so we can safely re-add them on write
    existing = [l.rstrip('\n').rstrip('\r') for l in existing]

    insert_idx = line_id - 1  # convert to 0-based
    new_lines = [str(l) for l in lines]

    # Clamp to EOF when line_id is beyond the current length (append behaviour)
    insert_idx = min(insert_idx, len(existing))

    existing[insert_idx:insert_idx] = new_lines

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
            f"{notice}Inserted {len(lines)} line(s) before line {line_id} in '{actual_name}'.\n"
            f"File now has {len(existing)} line(s)."
        ),
        "file_name": actual_name,
        "lines_inserted": len(lines),
        "insert_before_line": line_id,
        "total_lines": len(existing),
        "duplicated": record.get('origin') == 'loaded',
    }