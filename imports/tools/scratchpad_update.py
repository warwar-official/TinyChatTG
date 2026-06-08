"""In-app tool: replace a scratchpad line by 1-based index."""
from typing import Dict, Any


def update_record(conv_store, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the text of an existing scratchpad line."""
    record_id = args.get('record_id')
    text = args.get('text')

    if record_id is None:
        return {"error": "Argument 'record_id' is required."}
    if not text or not isinstance(text, str):
        return {"error": "Argument 'text' is required and must be a non-empty string."}

    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return {"error": "Argument 'record_id' must be an integer."}

    text = text.strip()
    if not text:
        return {"error": "Argument 'text' must not be blank."}

    try:
        lines = conv_store.get_scratchpad(user_id)
        if not lines:
            return {"error": "Scratchpad is empty."}
        if record_id < 1 or record_id > len(lines):
            return {"error": f"record_id {record_id} is out of range (1\u2013{len(lines)})."}

        old_text = lines[record_id - 1]
        lines[record_id - 1] = text
        conv_store.set_scratchpad(user_id, lines)
        return {"status": "updated", "record_id": record_id, "old_text": old_text, "new_text": text}
    except Exception as e:
        return {"error": str(e)}