"""In-app tool: remove a line from the user's scratchpad by 1-based index."""
from typing import Dict, Any


def remove_record(conv_store, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    """Remove a line from the scratchpad by its 1-based line number."""
    record_id = args.get('record_id')
    if record_id is None:
        return {"error": "Argument 'record_id' is required."}
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return {"error": "Argument 'record_id' must be an integer."}

    try:
        lines = conv_store.get_scratchpad(user_id)
        if not lines:
            return {"error": "Scratchpad is empty."}
        if record_id < 1 or record_id > len(lines):
            return {"error": f"record_id {record_id} is out of range (1\u2013{len(lines)})."}

        removed = lines.pop(record_id - 1)
        conv_store.set_scratchpad(user_id, lines)
        return {"status": "removed", "removed_text": removed, "total_records": len(lines)}
    except Exception as e:
        return {"error": str(e)}
