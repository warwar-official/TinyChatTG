import datetime
from typing import Dict, Any

from imports.files.store import FileStore


def list_files(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    start_id = args.get('start_id', 0)
    count = args.get('count', 20)

    try:
        start_id = int(start_id) if start_id is not None else 0
    except (TypeError, ValueError):
        start_id = 0

    try:
        count = int(count) if count is not None else 20
    except (TypeError, ValueError):
        count = 20

    count = min(count, 20)
    if count < 0:
        count = 0
    if start_id < 0:
        start_id = 0

    # Get total count (full list) then slice
    all_files = file_store.list_files(user_id, start=0, count=10000)
    total_files = len(all_files)

    if total_files == 0:
        return {
            "status": "success",
            "total_files": 0,
            "start_id": start_id,
            "end_id": None,
            "files": []
        }

    end_id = min(start_id + count, total_files)
    slice_files = all_files[start_id:end_id]

    files_list = []
    for i, rec in enumerate(slice_files, start=start_id):
        origin = rec.get("origin", "?")
        ftype = rec.get("type", "?")
        ts = rec.get("timestamp", 0)
        mtime = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else "unknown"
        real_name = rec.get("real_name", "?")
        files_list.append({
            "id": i,
            "real_name": real_name,
            "type": ftype,
            "origin": origin,
            "modified": mtime
        })

    return {
        "status": "success",
        "total_files": total_files,
        "start_id": start_id,
        "end_id": end_id - 1 if start_id < total_files else None,
        "files": files_list
    }
