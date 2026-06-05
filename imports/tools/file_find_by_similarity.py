"""file_find_by_similarity — find files by description embedding similarity."""
from typing import Dict, Any

from imports.files.store import FileStore


def find_by_similarity(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    query = args.get('query', '').strip()
    top_k = args.get('top_k', 5)

    if not query:
        return {"status": "error", "message": "query is required."}

    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5
    top_k = min(top_k, 10)

    results = file_store.find_by_similarity(user_id, query, top_k=top_k)

    out_results = []
    for rec in results:
        import datetime
        score = rec.pop('_score', 0.0)
        ts = rec.get('timestamp', 0)
        mtime = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else "unknown"
        desc = rec.get('description', '')
        desc_snippet = (desc[:120] + '...') if len(desc) > 120 else desc
        out_results.append({
            "real_name": rec.get("real_name"),
            "type": rec.get("type"),
            "origin": rec.get("origin"),
            "score": round(score, 3),
            "modified": mtime,
            "description_snippet": desc_snippet
        })

    return {
        "status": "success",
        "query": query,
        "results": out_results
    }
