"""In-app tool: wipe all lines from the user's scratchpad."""
from typing import Dict, Any


def wipe_records(conv_store, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    """Clear all lines from the scratchpad."""
    try:
        conv_store.set_scratchpad(user_id, [])
        return {"status": "wiped"}
    except Exception as e:
        return {"error": str(e)}