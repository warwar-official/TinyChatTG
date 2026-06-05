"""file_send — send a user-created file to the Telegram chat.

Only files with origin='created' can be sent.
Returns a sentinel dict {'_send_file': path, 'real_name': name} that the
orchestrator intercepts and forwards to the bot's send_file_callback.
"""
from typing import Dict, Any

from imports.files.store import FileStore


def send_file(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Any:
    file_name = args.get('file_name', '').strip()

    if not file_name:
        return {"status": "error", "message": "file_name is required."}
    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return {"status": "error", "message": "file_name must not contain path separators or traversal marks."}

    record = file_store.get_record(user_id, file_name)
    if not record:
        return {"status": "error", "message": f"File '{file_name}' not found or access denied."}

    if record.get('origin') != 'created':
        return {"status": "error", "message": (
            f"Error: Cannot send '{file_name}' — only user-created files can be sent. "
            f"This file has origin='{record.get('origin')}'."
        )}

    file_path = file_store.get_physical_path(user_id, file_name)
    if not file_path:
        return {"status": "error", "message": f"Error: Physical file for '{file_name}' not found."}

    # Return sentinel; orchestrator will invoke send_file_callback
    return {'_send_file': str(file_path), 'real_name': file_name}
