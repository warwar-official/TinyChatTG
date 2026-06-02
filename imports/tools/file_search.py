from pathlib import Path
from typing import Dict, Any


def search_file(user_id: int, args: Dict[str, Any]) -> str:
    file_name = args.get('file_name')
    query = args.get('query')

    if not file_name:
        return "Error: file_name is required."
    if query is None:
        return "Error: query is required."

    # Validate file_name for dangerous marks: '/', '\', '..'
    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return "Error: User directory do not contain subfolders, file_name should contain exectly file name."

    project_root = Path(__file__).resolve().parents[2]
    user_dir = project_root / 'data' / 'documents' / str(user_id)
    file_path = user_dir / file_name

    if not file_path.exists() or not file_path.is_file():
        return f"Error: File '{file_name}' not found."

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        return f"Error reading file: {str(e)}"

    results = []
    for idx, line in enumerate(lines, start=1):
        if query in line:
            pos = line.find(query)
            # Window of 40 characters before and after query
            start_pos = max(0, pos - 40)
            end_pos = min(len(line), pos + len(query) + 40)
            snippet = line[start_pos:end_pos].strip()

            prefix = "... " if start_pos > 0 else ""
            suffix = " ..." if end_pos < len(line) else ""
            results.append((idx, f"{prefix}{snippet}{suffix}"))

    total_results = len(results)
    slice_results = results[:50]

    output_lines = []
    output_lines.append(f"Total results found: {total_results}")
    output_lines.append(f"Results shown: {len(slice_results)} (max 50)")
    output_lines.append("")

    for line_id, snippet in slice_results:
        output_lines.append(f"Line {line_id}: {snippet}")

    return "\n".join(output_lines)
