"""In-app tool: append a new line to the user's scratchpad."""
from typing import Dict, Any

SCRATCHPAD_CHAR_LIMIT = 5000


def add_record(conv_store, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    """Append a new line to the user's scratchpad."""
    text = args.get('text')
    if not text or not isinstance(text, str):
        return {"error": "Argument 'text' is required and must be a non-empty string."}

    text = text.strip()
    if not text:
        return {"error": "Argument 'text' must not be blank."}

    try:
        lines = conv_store.get_scratchpad(user_id)
        current_content = '\n'.join(lines)
        added_chars = len(text) + (1 if current_content else 0)
        if len(current_content) + added_chars > SCRATCHPAD_CHAR_LIMIT:
            return {
                "error": (
                    "Scratchpad content limit reached. "
                    "Update existing or remove unnecessary records before adding new ones."
                )
            }

        lines.append(text)
        conv_store.set_scratchpad(user_id, lines)
        return {"status": "added", "record_id": len(lines), "total_records": len(lines)}
    except Exception as e:
        return {"error": str(e)}
