"""file_grep — search for a text string inside a file (content grep)."""
from typing import Dict, Any

from imports.files.store import FileStore


def grep_file(file_store: FileStore, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    file_name = args.get('file_name')
    query = args.get('query')

    if not file_name:
        return {"status": "error", "message": "file_name is required."}
    if query is None:
        return {"status": "error", "message": "query is required."}

    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return {"status": "error", "message": "file_name must not contain path separators or traversal marks."}

    file_path = file_store.get_physical_path(user_id, file_name)
    if not file_path:
        return {"status": "error", "message": f"File '{file_name}' not found or access denied."}

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        return {"status": "error", "message": f"Error reading file: {str(e)}"}

    results = []
    for idx, line in enumerate(lines, start=1):
        if query in line:
            pos = line.find(query)
            start_pos = max(0, pos - 40)
            end_pos = min(len(line), pos + len(query) + 40)
            snippet = line[start_pos:end_pos].strip()
            prefix = "... " if start_pos > 0 else ""
            suffix = " ..." if end_pos < len(line) else ""
            results.append((idx, f"{prefix}{snippet}{suffix}"))

    total_results = len(results)
    slice_results = results[:50]

    out_results = []
    for line_id, snippet in slice_results:
        out_results.append({
            "line_number": line_id,
            "snippet": snippet
        })

    return {
        "status": "success",
        "file_name": file_name,
        "query": query,
        "total_results": total_results,
        "results": out_results
    }
