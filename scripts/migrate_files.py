import os
import shutil
import hashlib
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from imports.config import CONFIG
from imports.memory.store import MemoryStore

PROJECT_ROOT = Path(__file__).resolve().parent

def get_hash(path: Path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def migrate():
    print("Starting file migration...")
    
    # Initialize MemoryStore which sets up Qdrant and Embedder automatically
    mem_store = MemoryStore(CONFIG)
    qdrant = mem_store.qdrant
    if not qdrant:
        print("Error: Qdrant client could not be initialized.")
        return
        
    embed_dim = mem_store.embed_dimension
    
    # Ensure collection
    FILES_COLLECTION = "files"
    try:
        collections = [c.name for c in qdrant.get_collections().collections]
        if FILES_COLLECTION not in collections:
            qdrant.create_collection(
                collection_name=FILES_COLLECTION,
                vectors_config=qmodels.VectorParams(
                    size=embed_dim,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            print(f"Created Qdrant collection '{FILES_COLLECTION}'.")
    except Exception as e:
        print(f"Failed to ensure Qdrant collection: {e}")
        return

    # Directories
    old_docs_dir = PROJECT_ROOT / "data" / "documents"
    old_imgs_dir = PROJECT_ROOT / "data" / "images"
    
    new_docs_dir = PROJECT_ROOT / "data" / "files" / "documents"
    new_imgs_dir = PROJECT_ROOT / "data" / "files" / "images"
    
    new_docs_dir.mkdir(parents=True, exist_ok=True)
    new_imgs_dir.mkdir(parents=True, exist_ok=True)

    points = []
    
    def add_point(user_id: int, hash_name: str, real_name: str, file_type: str):
        import uuid
        import time
        pid = str(uuid.uuid4())
        vec = [0.0] * embed_dim
        payload = {
            "hash_name": hash_name,
            "real_name": real_name,
            "owner_id": user_id,
            "description": "",
            "type": file_type,
            "origin": "loaded",
            "timestamp": time.time(),
            # New fields for improved FileStore compatibility
            "media_dir": "",
            "corrupted": False,
            "corrupted_pages": [],
        }
        points.append(qmodels.PointStruct(id=pid, vector=vec, payload=payload))

    # Migrate Documents
    if old_docs_dir.exists():
        for user_folder in old_docs_dir.iterdir():
            if not user_folder.is_dir():
                continue
            try:
                user_id = int(user_folder.name)
            except ValueError:
                continue
                
            for file_path in user_folder.iterdir():
                if not file_path.is_file():
                    continue
                
                real_name = file_path.name
                h = get_hash(file_path)
                suffix = file_path.suffix or ".txt"
                hash_name = h + suffix
                
                new_path = new_docs_dir / hash_name
                if not new_path.exists():
                    shutil.copy2(file_path, new_path)
                    
                add_point(user_id, hash_name, real_name, "document")
                print(f"Migrated doc: {user_id}/{real_name} -> {hash_name}")

    # Migrate Images
    if old_imgs_dir.exists():
        for user_folder in old_imgs_dir.iterdir():
            if not user_folder.is_dir():
                continue
            try:
                user_id = int(user_folder.name)
            except ValueError:
                continue
                
            # Images might be nested in media_group_id folders or directly
            for root, _, files in os.walk(user_folder):
                for f in files:
                    file_path = Path(root) / f
                    real_name = file_path.name
                    h = get_hash(file_path)
                    hash_name = h + ".jpg"
                    
                    new_path = new_imgs_dir / hash_name
                    if not new_path.exists():
                        shutil.copy2(file_path, new_path)
                        
                    add_point(user_id, hash_name, real_name, "image")
                    print(f"Migrated image: {user_id}/{real_name} -> {hash_name}")

    # Upsert points
    if points:
        print(f"Upserting {len(points)} records to Qdrant...")
        # Upsert in batches of 100
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i:i+batch_size]
            qdrant.upsert(collection_name=FILES_COLLECTION, points=batch)
        print("Migration complete!")
    else:
        print("No files found to migrate.")

    # --- Access migration: mark users as expired 'user' access
    auth_path = PROJECT_ROOT / "data" / "state" / "auth.json"
    try:
        import json, time
        if auth_path.exists():
            with open(auth_path, 'r', encoding='utf-8') as f:
                auth_data = json.load(f) or {}
        else:
            auth_data = {}

        users = auth_data.setdefault('users', {})
        now = time.time()
        for uid, u in users.items():
            # Set access type and mark as expired (leave console/admin handling to manual edits)
            u.setdefault('access_type', 'user')
            # expired timestamp (0) means expired
            u['access_expires'] = 0
            # ensure they are not authorized by default after migration
            u['authorized'] = False
        with open(auth_path, 'w', encoding='utf-8') as f:
            json.dump(auth_data, f, ensure_ascii=False, indent=2)
        print("Auth migration: marked existing users as expired 'user' access.")
    except Exception as e:
        print(f"Auth migration failed: {e}")

    # --- MCP tool configs migration: ensure allow_summarizing present
    try:
        import yaml
        mcp_dir = PROJECT_ROOT / "data" / "mcp"
        if mcp_dir.exists():
            for p in mcp_dir.iterdir():
                if p.suffix.lower() in ('.yaml', '.yml'):
                    try:
                        with open(p, 'r', encoding='utf-8') as f:
                            cfg = yaml.safe_load(f) or {}
                        tools = cfg.get('tools', {})
                        changed = False
                        for tname, tcfg in tools.items():
                            if not isinstance(tcfg, dict):
                                tools[tname] = { 'allow_summarizing': True }
                                changed = True
                            else:
                                if 'allow_summarizing' not in tcfg:
                                    tcfg['allow_summarizing'] = True
                                    changed = True
                        if changed:
                            cfg['tools'] = tools
                            with open(p, 'w', encoding='utf-8') as f:
                                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
                            print(f"Updated MCP config: {p}")
                    except Exception as e:
                        print(f"Failed to update MCP config {p}: {e}")
    except Exception as e:
        print(f"MCP tools migration failed: {e}")

    # --- app_config migration: add auth.expiry_message and summarize_tool_results default
    try:
        import yaml
        cfg_path = PROJECT_ROOT / "data" / "configs" / "app_config.yaml"
        if cfg_path.exists():
            with open(cfg_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
        else:
            cfg = {}

        bot_cfg = cfg.setdefault('bot', {})
        bot_cfg.setdefault('summarize_tool_results', True)
        cfg['bot'] = bot_cfg

        auth_cfg = cfg.setdefault('auth', {})
        auth_cfg.setdefault('expiry_message', "Your access expired. Update your plan or use another key.")
        cfg['auth'] = auth_cfg

        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
        print("app_config migration: ensured auth and bot defaults.")
    except Exception as e:
        print(f"app_config migration failed: {e}")

if __name__ == "__main__":
    migrate()
