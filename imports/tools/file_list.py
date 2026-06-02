import datetime
from pathlib import Path
from typing import Dict, Any


def list_files(user_id: int, args: Dict[str, Any]) -> str:
    start_id = args.get('start_id', 0)
    count = args.get('count', 20)

    # Convert/validate inputs
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

    project_root = Path(__file__).resolve().parents[2]
    user_dir = project_root / 'data' / 'documents' / str(user_id)

    if not user_dir.exists() or not user_dir.is_dir():
        return "Total files: 0\nRange shown: N/A\n\nNo files found."

    files = [f for f in user_dir.iterdir() if f.is_file()]
    # Sort from new to old (mtime descending)
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    total_files = len(files)

    if total_files == 0:
        return "Total files: 0\nRange shown: N/A\n\nNo files found."

    end_id = min(start_id + count, total_files)
    slice_files = files[start_id:end_id]

    lines = []
    lines.append(f"Total files: {total_files}")
    if start_id < total_files:
        lines.append(f"Range shown: {start_id} to {end_id - 1} (0-indexed)")
    else:
        lines.append("Range shown: N/A (start_id out of bounds)")
    lines.append("")

    for i, f in enumerate(slice_files, start=start_id):
        sz = f.stat().st_size
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        lines.append(f"[{i}] {f.name} - {sz} bytes - Modified: {mtime}")

    return "\n".join(lines)
