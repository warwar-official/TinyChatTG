"""file_find_by_name — search file metadata by real_name substring."""
from typing import Dict, Any

from imports.files.store import FileStore


def find_by_name(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    query = args.get('query', '').strip()
    limit = args.get('limit', 10)

    if not query:
        return {"status": "error", "message": "query is required."}

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    limit = min(limit, 20)

    results = file_store.find_by_name(user_id, query, limit=limit)

    out_results = []
    for rec in results:
        import datetime
        ts = rec.get('timestamp', 0)
        mtime = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else "unknown"
        out_results.append({
            "real_name": rec.get("real_name"),
            "type": rec.get("type"),
            "origin": rec.get("origin"),
            "modified": mtime,
            "has_description": bool(rec.get("description"))
        })

    return {
        "status": "success",
        "query": query,
        "results": out_results
    }
