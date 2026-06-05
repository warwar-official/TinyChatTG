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

if __name__ == "__main__":
    migrate()
