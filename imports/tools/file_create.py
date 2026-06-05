"""file_create — create a new empty text file owned by the current user."""
from typing import Dict, Any

from imports.files.store import FileStore


def create_file(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    file_name = args.get('file_name', '').strip()

    if not file_name:
        return {"status": "error", "message": "file_name is required."}
    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return {"status": "error", "message": "file_name must not contain path separators or traversal marks."}

    result = file_store.create_file(user_id, file_name)
    if 'error' in result:
        return {"status": "error", "message": result['error']}

    return {
        "status": "success",
        "message": f"File '{result['real_name']}' created successfully.",
        "file_name": result['real_name'],
        "type": result['type'],
        "origin": result['origin']
    }
