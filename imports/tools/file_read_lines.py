from pathlib import Path
from typing import Dict, Any


def read_file_lines(user_id: int, args: Dict[str, Any]) -> str:
    file_name = args.get('file_name')
    if not file_name:
        return "Error: file_name is required."

    # Validate file_name for dangerous marks: '/', '\', '..'
    if '/' in file_name or '\\' in file_name or '..' in file_name:
        return "Error: User directory do not contain subfolders, file_name should contain exectly file name."

    start_id = args.get('start_id', 1)
    count = args.get('count', 50)

    # Convert/validate inputs
    try:
        start_id = int(start_id) if start_id is not None else 1
    except (TypeError, ValueError):
        start_id = 1

    try:
        count = int(count) if count is not None else 50
    except (TypeError, ValueError):
        count = 50

    count = min(count, 50)
    if count < 0:
        count = 0
    if start_id < 1:
        start_id = 1

    project_root = Path(__file__).resolve().parents[2]
    user_dir = project_root / 'data' / 'documents' / str(user_id)
    file_path = user_dir / file_name

    if not file_path.exists() or not file_path.is_file():
        return f"Error: File '{file_name}' not found."

    # Count total files in the user directory
    total_files = sum(1 for f in user_dir.iterdir() if f.is_file())

    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception as e:
        return f"Error reading file: {str(e)}"

    total_lines = len(lines)
    start_idx = start_id - 1
    end_idx = min(start_idx + count, total_lines)

    slice_lines = lines[start_idx:end_idx]
    eof = end_idx >= total_lines

    result_lines = []
    result_lines.append(f"Total files in directory: {total_files}")
    result_lines.append(f"Total lines in file: {total_lines}")
    result_lines.append(f"Range shown: lines {start_id} to {end_idx}")
    result_lines.append("")

    for idx, line in enumerate(slice_lines, start=start_id):
        # strip trailing newline to format nicely
        result_lines.append(f"{idx}: {line.rstrip(chr(10).replace(chr(13), ''))}")

    if eof:
        result_lines.append("")
        result_lines.append("[END OF FILE]")

    return "\n".join(result_lines)
