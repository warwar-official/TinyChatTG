#!/usr/bin/env python
"""Utility script to export file description and metadata from Qdrant files collection to JSON."""

import json
from pathlib import Path
from imports.config import CONFIG
from imports.memory.store import MemoryStore

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "data" / "tmp"
OUTPUT_FILE = OUTPUT_DIR / "descriptions_debug.json"
FILES_COLLECTION = "files"

def export_descriptions():
    print("Initializing MemoryStore...")
    try:
        mem_store = MemoryStore(CONFIG)
        qdrant = mem_store.qdrant
    except Exception as e:
        print(f"Error initializing MemoryStore: {e}")
        return

    if not qdrant:
        print("Error: Qdrant client could not be initialized.")
        return

    print("Fetching points from collection 'files'...")
    exported_data = []
    offset = None
    batch_count = 0

    try:
        # Check if the files collection exists first
        collections = [c.name for c in qdrant.get_collections().collections]
        if FILES_COLLECTION not in collections:
            print(f"Error: Qdrant collection '{FILES_COLLECTION}' does not exist.")
            return

        while True:
            records, next_offset = qdrant.scroll(
                collection_name=FILES_COLLECTION,
                limit=100,
                with_payload=True,
                with_vectors=False,
                offset=offset
            )
            batch_count += 1
            print(f"Retrieved batch {batch_count} ({len(records)} points)...")

            for record in records:
                payload = record.payload if hasattr(record, "payload") else {}
                point_id = record.id if hasattr(record, "id") else None

                # Extract metadata fields
                extracted = {
                    "point_id": point_id,
                    "owner_id": payload.get("owner_id"),
                    "real_name": payload.get("real_name"),
                    "hash_name": payload.get("hash_name"),
                    "description": payload.get("description"),
                    "type": payload.get("type"),
                    "origin": payload.get("origin"),
                    "timestamp": payload.get("timestamp")
                }
                exported_data.append(extracted)

            if next_offset is None:
                break
            offset = next_offset

    except Exception as e:
        print(f"Error fetching points from Qdrant: {e}")
        return

    print(f"Successfully retrieved {len(exported_data)} total files.")

    # Ensure data/tmp exists
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(exported_data, f, ensure_ascii=False, indent=2)
        print(f"Saved JSON export to: {OUTPUT_FILE}")
    except Exception as e:
        print(f"Failed to write JSON output to {OUTPUT_FILE}: {e}")

if __name__ == "__main__":
    export_descriptions()
