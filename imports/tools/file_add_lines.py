"""file_add_lines — append lines to end of a user-owned file.

If the target file has origin='loaded', it is automatically duplicated to
a new 'created' copy before the edit is applied.
"""
from typing import Dict, Any, List

from imports.files.store import FileStore


def add_lines(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    file_name = args.get('file_name', '').strip()
    lines: List[str] = args.get('lines', [])

    if not file_name:
        return {"status": "error", "message": "file_name is required."}
    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return {"status": "error", "message": "file_name must not contain path separators or traversal marks."}
    if not isinstance(lines, list):
        return {"status": "error", "message": "lines must be a list of strings."}
    if not lines:
        return {"status": "error", "message": "lines list is empty — nothing to add."}

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
        with open(file_path, 'a', encoding='utf-8') as f:
            for line in lines:
                f.write(str(line) + '\n')
    except Exception as e:
        return {"status": "error", "message": f"Error writing to file: {e}"}

    file_store.update_timestamp(user_id, actual_name)

    return {
        "status": "success",
        "message": f"{notice}Added {len(lines)} line(s) to '{actual_name}'.",
        "file_name": actual_name,
        "lines_added": len(lines),
        "duplicated": record.get('origin') == 'loaded'
    }
