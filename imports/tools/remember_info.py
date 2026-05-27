import re
from typing import Dict, Any


def add_memory(mem_store, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    title = args.get('title') or args.get('name') or 'manual'
    text = args.get('text') or args.get('value') or ''
    
    if not text:
        return {"error": "no text provided"}
    
    if len(text) < 50:
        return {"error": f"Memory text must be at least 50 characters long (current length: {len(text)}). Please pad with context."}
        
    if len(text) > 350:
        return {"error": f"Memory text must not exceed 350 characters (current length: {len(text)})."}
        
    if not re.search(r'[a-zA-Z]', text):
        return {"error": "Memory text must contain alphabetical letters."}
        
    try:
        res = mem_store.add_memory_manual(user_id, title, text)
        return res
    except Exception as e:
        return {"error": str(e)}
