"""FileStore — flat hash-deduplicated file storage with Qdrant metadata index.

Physical layout:
    data/files/documents/<sha256><ext>
    data/files/images/<sha256>.jpg

Qdrant collection "files" payload per point:
    hash_name  : str   — physical filename (sha256+ext)
    real_name  : str   — display / original filename
    owner_id   : int   — Telegram user_id
    description: str   — model-generated description (empty until described)
    type       : str   — "image" | "document"
    origin     : str   — "loaded" | "created"
    timestamp  : float — unix time of add / last-modified
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from pathlib import Path
from typing import Callable, List, Optional, Any

import numpy as np

logger = logging.getLogger(__name__)


class FileStore:
    FILES_COLLECTION = "files"

    def __init__(
        self,
        project_root: Path,
        embed_fn: Callable[[str], np.ndarray],
        embed_dim: int,
        qdrant_client: Any,  # QdrantClient instance (shared with MemoryStore)
    ):
        self.project_root = Path(project_root)
        self.embed_fn = embed_fn
        self.embed_dim = embed_dim
        self.qdrant = qdrant_client

        # Ensure storage directories exist
        self._docs_dir = self.project_root / "data" / "files" / "documents"
        self._imgs_dir = self.project_root / "data" / "files" / "images"
        self._docs_dir.mkdir(parents=True, exist_ok=True)
        self._imgs_dir.mkdir(parents=True, exist_ok=True)

        self._qdrant_ready = False
        if self.qdrant:
            self._ensure_collection()

    # ── Qdrant helpers ───────────────────────────────────────────────────

    def _ensure_collection(self):
        try:
            from qdrant_client.http import models as qmodels
            collections = [c.name for c in self.qdrant.get_collections().collections]
            if self.FILES_COLLECTION not in collections:
                self.qdrant.create_collection(
                    collection_name=self.FILES_COLLECTION,
                    vectors_config=qmodels.VectorParams(
                        size=self.embed_dim,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection '%s' dim=%d", self.FILES_COLLECTION, self.embed_dim)
            self._qdrant_ready = True
        except Exception as e:
            logger.warning("FileStore: failed to ensure Qdrant collection: %s", e)
            self._qdrant_ready = False

    def _upsert(self, point):
        try:
            self.qdrant.upsert(collection_name=self.FILES_COLLECTION, points=[point])
        except Exception as e:
            logger.error("FileStore upsert failed: %s", e)
            raise

    def _scroll(self, flt, limit: int = 100) -> list:
        """Scroll with a filter, return list of ScoredPoint/Record payloads."""
        try:
            records, _ = self.qdrant.scroll(
                collection_name=self.FILES_COLLECTION,
                scroll_filter=flt,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            return records
        except Exception as e:
            logger.warning("FileStore scroll failed: %s", e)
            return []

    def _search(self, vec: np.ndarray, flt, top_k: int = 10) -> list:
        try:
            hits = self.qdrant.search(
                collection_name=self.FILES_COLLECTION,
                query_vector=vec.tolist() if hasattr(vec, "tolist") else vec,
                query_filter=flt,
                limit=top_k,
                with_payload=True,
            )
            return hits
        except Exception as e:
            logger.warning("FileStore search failed: %s", e)
            return []

    def _owner_filter(self, user_id: int):
        from qdrant_client.http import models as qmodels
        return qmodels.Filter(
            must=[qmodels.FieldCondition(
                key="owner_id",
                match=qmodels.MatchValue(value=int(user_id)),
            )]
        )

    def _zero_vector(self) -> list:
        return [0.0] * self.embed_dim

    # ── Hash helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ── Registration ─────────────────────────────────────────────────────

    def register_image(self, user_id: int, data: bytes, real_name: str) -> dict:
        """Register an uploaded image.

        Returns dict with keys: hash_name, real_name, is_new, path.
        """
        h = self._hash_bytes(data)
        ext = ".jpg"
        hash_name = h + ext
        phys = self._imgs_dir / hash_name

        is_new_file = not phys.exists()
        if is_new_file:
            phys.write_bytes(data)

        # Check if this user already has a record for this hash
        existing = self._find_record_by_hash(user_id, hash_name)
        if existing:
            return {"hash_name": hash_name, "real_name": existing.get("real_name", real_name),
                    "is_new": False, "path": str(phys)}

        # Upsert Qdrant record
        self._upsert_record(
            user_id=user_id,
            hash_name=hash_name,
            real_name=real_name,
            description="",
            file_type="image",
            origin="loaded",
        )
        return {"hash_name": hash_name, "real_name": real_name, "is_new": True, "path": str(phys)}

    def register_document(self, user_id: int, data: bytes, real_name: str) -> dict:
        """Register an uploaded document.

        Returns dict with keys: hash_name, real_name, is_new, path.
        """
        h = self._hash_bytes(data)
        suffix = Path(real_name).suffix or ".txt"
        hash_name = h + suffix
        phys = self._docs_dir / hash_name

        is_new_file = not phys.exists()
        if is_new_file:
            phys.write_bytes(data)

        # Check if this user already has a record for this hash
        existing = self._find_record_by_hash(user_id, hash_name)
        if existing:
            return {"hash_name": hash_name, "real_name": existing.get("real_name", real_name),
                    "is_new": False, "path": str(phys)}

        self._upsert_record(
            user_id=user_id,
            hash_name=hash_name,
            real_name=real_name,
            description="",
            file_type="document",
            origin="loaded",
        )
        return {"hash_name": hash_name, "real_name": real_name, "is_new": True, "path": str(phys)}

    def check_converted_document_exists(self, user_id: int, raw_hash: str) -> Optional[dict]:
        """Check if a converted document (PDF, docx) already exists by its original raw bytes hash."""
        hash_name = raw_hash + ".md"
        existing = self._find_record_by_hash(user_id, hash_name)
        if existing:
            return {
                "hash_name": hash_name,
                "real_name": existing.get("real_name", ""),
                "is_new": False,
                "path": str(self._docs_dir / hash_name),
            }
        return None

    def register_converted_document(
        self,
        user_id: int,
        md_bytes: bytes,
        real_name: str,
        raw_hash: str,
        media_dir: str = "",
    ) -> dict:
        """Register a document that was converted to Markdown (from .docx, .pdf, etc.).

        The *real_name* is the ORIGINAL filename (e.g. 'report.docx') so that the
        user/model always refers to it by its original name.
        The physical file on disk is stored as '<raw_hash>.md'.

        Returns dict with keys: hash_name, real_name, is_new, path.
        """
        hash_name = raw_hash + ".md"
        phys = self._docs_dir / hash_name

        is_new_file = not phys.exists()
        if is_new_file:
            phys.write_bytes(md_bytes)

        # If the user already has a record for this exact real_name, update it
        existing = self._find_record_by_real_name(user_id, real_name)
        if existing:
            # Update media_dir if changed (e.g. re-uploaded)
            if media_dir and existing.get("media_dir") != media_dir and existing.get("_point_id"):
                try:
                    self.qdrant.set_payload(
                        collection_name=self.FILES_COLLECTION,
                        payload={"media_dir": media_dir, "hash_name": hash_name, "timestamp": time.time()},
                        points=[existing["_point_id"]],
                    )
                except Exception as e:
                    logger.warning("register_converted_document: failed to update media_dir: %s", e)
            return {"hash_name": hash_name, "real_name": real_name, "is_new": False, "path": str(phys)}

        self._upsert_record(
            user_id=user_id,
            hash_name=hash_name,
            real_name=real_name,
            description="",
            file_type="document",
            origin="loaded",
            media_dir=media_dir,
        )
        return {"hash_name": hash_name, "real_name": real_name, "is_new": True, "path": str(phys)}

    def _upsert_record(self, user_id: int, hash_name: str, real_name: str,
                       description: str, file_type: str, origin: str,
                       point_id: Optional[str] = None, timestamp: Optional[float] = None,
                       media_dir: str = ""):
        if not self._qdrant_ready:
            return
        from qdrant_client.http import models as qmodels
        pid = point_id or str(uuid.uuid4())
        vec = self.embed_fn(description).tolist() if description else self._zero_vector()
        payload = {
            "hash_name": hash_name,
            "real_name": real_name,
            "owner_id": int(user_id),
            "description": description,
            "type": file_type,
            "origin": origin,
            "timestamp": timestamp or time.time(),
            "media_dir": media_dir,
        }
        pt = qmodels.PointStruct(id=pid, vector=vec, payload=payload)
        self._upsert(pt)
        return pid

    # ── Description update ───────────────────────────────────────────────

    def update_description(self, user_id: int, hash_name: str, description: str) -> bool:
        """Update description + re-embed for the given hash_name owned by user_id."""
        if not self._qdrant_ready:
            return False
        try:
            record = self._find_record_by_hash(user_id, hash_name)
            if not record:
                logger.warning("FileStore.update_description: no record for %s / user %d", hash_name, user_id)
                return False
            point_id = record.get("_point_id")
            if not point_id:
                return False

            from qdrant_client.http import models as qmodels
            vec = self.embed_fn(description).tolist() if description else self._zero_vector()
            payload_update = {"description": description}
            self.qdrant.set_payload(
                collection_name=self.FILES_COLLECTION,
                payload=payload_update,
                points=[point_id],
            )
            self.qdrant.update_vectors(
                collection_name=self.FILES_COLLECTION,
                points=[qmodels.PointVectors(id=point_id, vector=vec)],
            )
            logger.info("FileStore: updated description for %s", hash_name)
            return True
        except Exception as e:
            logger.warning("FileStore.update_description failed: %s", e)
            return False

    # ── Internal lookup ──────────────────────────────────────────────────

    def _find_record_by_hash(self, user_id: int, hash_name: str) -> Optional[dict]:
        """Return payload dict (with _point_id) for the first matching record, or None."""
        if not self._qdrant_ready:
            return None
        try:
            from qdrant_client.http import models as qmodels
            flt = qmodels.Filter(must=[
                qmodels.FieldCondition(key="owner_id", match=qmodels.MatchValue(value=int(user_id))),
                qmodels.FieldCondition(key="hash_name", match=qmodels.MatchValue(value=hash_name)),
            ])
            records = self._scroll(flt, limit=1)
            if records:
                r = records[0]
                payload = r.payload if hasattr(r, "payload") else {}
                payload["_point_id"] = r.id if hasattr(r, "id") else None
                return payload
        except Exception as e:
            logger.warning("FileStore._find_record_by_hash failed: %s", e)
        return None

    def _find_record_by_real_name(self, user_id: int, real_name: str) -> Optional[dict]:
        """Find a record by exact real_name owned by user_id."""
        if not self._qdrant_ready:
            return None
        try:
            from qdrant_client.http import models as qmodels
            flt = qmodels.Filter(must=[
                qmodels.FieldCondition(key="owner_id", match=qmodels.MatchValue(value=int(user_id))),
                qmodels.FieldCondition(key="real_name", match=qmodels.MatchValue(value=real_name)),
            ])
            records = self._scroll(flt, limit=1)
            if records:
                r = records[0]
                payload = r.payload if hasattr(r, "payload") else {}
                payload["_point_id"] = r.id if hasattr(r, "id") else None
                return payload
        except Exception as e:
            logger.warning("FileStore._find_record_by_real_name failed: %s", e)
        return None

    # ── Physical path resolution (ownership-enforced) ────────────────────

    def get_physical_path(self, user_id: int, real_name: str) -> Optional[Path]:
        """Return physical path only if file exists and is owned by user_id."""
        record = self._find_record_by_real_name(user_id, real_name)
        if not record:
            return None
        hash_name = record.get("hash_name", "")
        file_type = record.get("type", "document")
        base = self._imgs_dir if file_type == "image" else self._docs_dir
        p = base / hash_name
        return p if p.exists() else None

    def get_record(self, user_id: int, real_name: str) -> Optional[dict]:
        """Return full metadata record for real_name owned by user_id."""
        return self._find_record_by_real_name(user_id, real_name)

    def get_media_dir(self, user_id: int, real_name: str) -> Optional[Path]:
        """Return the media directory for a converted document, or None if it has none."""
        record = self._find_record_by_real_name(user_id, real_name)
        if not record:
            return None
        md = record.get("media_dir", "")
        if not md:
            return None
        p = Path(md)
        return p if p.is_dir() else None

    # ── Tool-facing queries ──────────────────────────────────────────────

    def list_files(self, user_id: int, start: int = 0, count: int = 20) -> List[dict]:
        """Return files sorted by timestamp descending."""
        if not self._qdrant_ready:
            return []
        try:
            records = self._scroll(self._owner_filter(user_id), limit=1000)
            payloads = []
            for r in records:
                p = r.payload if hasattr(r, "payload") else {}
                if p:
                    payloads.append(p)
            payloads.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            return payloads[start: start + count]
        except Exception as e:
            logger.warning("FileStore.list_files failed: %s", e)
            return []

    def find_by_name(self, user_id: int, query: str, limit: int = 10) -> List[dict]:
        """Return files whose real_name contains query (case-insensitive substring)."""
        if not self._qdrant_ready:
            return []
        try:
            records = self._scroll(self._owner_filter(user_id), limit=1000)
            q = query.lower()
            results = []
            for r in records:
                p = r.payload if hasattr(r, "payload") else {}
                if q in (p.get("real_name") or "").lower():
                    results.append(p)
                if len(results) >= limit:
                    break
            return results
        except Exception as e:
            logger.warning("FileStore.find_by_name failed: %s", e)
            return []

    def find_by_similarity(self, user_id: int, query: str, top_k: int = 5) -> List[dict]:
        """Return files ranked by description embedding similarity."""
        if not self._qdrant_ready:
            return []
        try:
            vec = self.embed_fn(query)
            hits = self._search(vec, self._owner_filter(user_id), top_k=top_k)
            results = []
            for h in hits:
                p = h.payload if hasattr(h, "payload") else {}
                if p and p.get("description"):  # Only return described files
                    p["_score"] = h.score if hasattr(h, "score") else 0.0
                    results.append(p)
            return results
        except Exception as e:
            logger.warning("FileStore.find_by_similarity failed: %s", e)
            return []

    # ── File creation tools ──────────────────────────────────────────────

    def create_file(self, user_id: int, real_name: str) -> dict:
        """Create a new empty document file. Returns record dict or error."""
        # Validate name safety
        if not real_name or "/" in real_name or "\\" in real_name or ".." in real_name:
            return {"error": "Invalid file name."}
        # Check for duplicate real_name for this user
        existing = self._find_record_by_real_name(user_id, real_name)
        if existing:
            return {"error": f"File '{real_name}' already exists."}
        # Compute hash of empty content + real_name to ensure uniqueness per user
        unique_seed = f"{user_id}:{real_name}:{time.time()}".encode()
        h = hashlib.sha256(unique_seed).hexdigest()
        suffix = Path(real_name).suffix or ".txt"
        hash_name = h + suffix
        phys = self._docs_dir / hash_name
        phys.write_text("", encoding="utf-8")

        self._upsert_record(
            user_id=user_id,
            hash_name=hash_name,
            real_name=real_name,
            description="",
            file_type="document",
            origin="created",
        )
        return {"hash_name": hash_name, "real_name": real_name, "type": "document", "origin": "created", "path": str(phys)}

    def duplicate_to_created(self, user_id: int, real_name: str) -> dict:
        """Duplicate a 'loaded' file to a new 'created' copy.

        The copy gets a name like '<base>_copy.<ext>' (or increments until unique).
        Returns the new record dict or error dict.
        """
        record = self._find_record_by_real_name(user_id, real_name)
        if not record:
            return {"error": f"File '{real_name}' not found."}
        if record.get("origin") == "created":
            return {"error": "already_created", "record": record}

        # Read physical content
        old_path = self.get_physical_path(user_id, real_name)
        if not old_path:
            return {"error": f"Physical file for '{real_name}' not found."}
        try:
            data = old_path.read_bytes()
        except Exception as e:
            return {"error": f"Could not read '{real_name}': {e}"}

        # Build new unique name
        stem = Path(real_name).stem
        suffix = Path(real_name).suffix or ".txt"
        new_real_name = f"{stem}_copy{suffix}"
        counter = 1
        while self._find_record_by_real_name(user_id, new_real_name):
            counter += 1
            new_real_name = f"{stem}_copy{counter}{suffix}"

        # Hash new content for physical storage (same bytes → same hash but different real_name is fine)
        unique_seed = f"{user_id}:{new_real_name}:{time.time()}".encode()
        h = hashlib.sha256(unique_seed).hexdigest()
        hash_name = h + suffix
        phys = self._docs_dir / hash_name
        phys.write_bytes(data)

        self._upsert_record(
            user_id=user_id,
            hash_name=hash_name,
            real_name=new_real_name,
            description="",
            file_type="document",
            origin="created",
        )
        return {"hash_name": hash_name, "real_name": new_real_name, "type": "document", "origin": "created", "path": str(phys)}

    def update_timestamp(self, user_id: int, real_name: str) -> None:
        """Update the timestamp field for a file (after modification)."""
        if not self._qdrant_ready:
            return
        try:
            record = self._find_record_by_real_name(user_id, real_name)
            if record and record.get("_point_id"):
                self.qdrant.set_payload(
                    collection_name=self.FILES_COLLECTION,
                    payload={"timestamp": time.time()},
                    points=[record["_point_id"]],
                )
        except Exception as e:
            logger.warning("FileStore.update_timestamp failed: %s", e)
