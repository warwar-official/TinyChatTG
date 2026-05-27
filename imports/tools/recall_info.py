"""Memory search tool provided by the app."""
from typing import Dict, Any


def search_memory(mem_store, user_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    query = args.get('query') or ''
    if not query:
        return {"error": "no query provided"}
    
    top_k = args.get('limit', 5)
    try:
        results = mem_store.search(user_id, query, top_k=top_k)
        if not results:
            return {"status": "success", "results": "No memories found matching the query."}
        
        # Format the results into a string representation
        formatted_results = []
        for idx, res in enumerate(results):
            text = res.get('text', '')
            meta = res.get('meta', {})
            title = meta.get('title', 'Memory')
            formatted_results.append(f"{idx + 1}. {title}: {text}")
            
        return {
            "status": "success", 
            "results": "\n\n".join(formatted_results),
            "count": len(results)
        }
    except Exception as e:
        return {"error": str(e)}
