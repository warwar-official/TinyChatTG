"""Lightweight MemoryStore with JSON fallback; uses fastembed/qdrant when available."""
import json
import asyncio
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any
import atexit

import numpy as np
import uuid

logger = logging.getLogger(__name__)

try:
    from qdrant_client import QdrantClient
    QDRANT_AVAILABLE = True
except Exception:
    QDRANT_AVAILABLE = False

try:
    from fastembed import TextEmbedding
    FASTEMBED_AVAILABLE = True
except Exception:
    FASTEMBED_AVAILABLE = False


class MemoryStore:
    IDENTICAL_THRESHOLD = 0.97
    MERGE_THRESHOLD = 0.85
    COLLECTION_NAME = "memories"

    def __init__(self, config: Dict[str, Any]):
        memory_conf = config.get("memory") if isinstance(config, dict) else {}
        self.model_path = memory_conf.get("model_path", "data/memory/model")
        self.db_path = memory_conf.get("db_path", "data/memory/db")
        self.embedding_model = memory_conf.get("embedding_model", "intfloat/multilingual-e5-large")
        self.max_tool_response_chars = memory_conf.get("max_tool_response_chars", 15000)

        Path(self.db_path).mkdir(parents=True, exist_ok=True)
        self.json_path = Path(self.db_path) / "memories.json"
        if not self.json_path.exists():
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump([], f)

        # Attempt to initialize real embedder, fallback to pseudo-embeddings
        self._embed_dim = None
        if FASTEMBED_AVAILABLE:
            try:
                Path(self.model_path).mkdir(parents=True, exist_ok=True)
                self._fastembed = TextEmbedding(
                    model_name=self.embedding_model,
                    cache_dir=self.model_path,
                )
                # Get embedding dimension by embedding a test string
                test_vec = list(self._fastembed.embed(["test"]))[0]
                self._embed_dim = len(test_vec)
                self.embed_fn = self._real_embed
                logger.info("fastembed initialized: model=%s dim=%d", self.embedding_model, self._embed_dim)
                print(f"[MemoryStore] fastembed initialized: model={self.embedding_model}, dim={self._embed_dim}")
            except Exception as e:
                logger.warning("fastembed init failed, using pseudo-embeddings: %s", e)
                print(f"[MemoryStore] WARNING: fastembed init failed ({e}), using pseudo-embeddings")
                self._fastembed = None
                self._embed_dim = 384
                self.embed_fn = self._pseudo_embed
        else:
            logger.warning("fastembed not available, using pseudo-embeddings")
            print("[MemoryStore] WARNING: fastembed not installed, using pseudo-embeddings")
            self._fastembed = None
            self._embed_dim = 384
            self.embed_fn = self._pseudo_embed

        # qdrant is optional; fallback to JSON store
        self.qdrant = None
        self._qdrant_ready = False
        if QDRANT_AVAILABLE:
            try:
                self.qdrant = QdrantClient(path=self.db_path)
                self._ensure_qdrant_collection()
                logger.info("Qdrant initialized (path mode): %s", self.db_path)
                print(f"[MemoryStore] Qdrant initialized: {self.db_path}")
            except Exception as e:
                logger.warning("Qdrant init failed, using JSON fallback: %s", e)
                print(f"[MemoryStore] WARNING: Qdrant init failed ({e}), using JSON fallback")
                self.qdrant = None

        # register close handler to avoid Qdrant shutdown issues
        try:
            atexit.register(self.close)
        except Exception:
            pass

    def _ensure_qdrant_collection(self):
        """Create the memories collection if it doesn't exist (without destroying existing data)."""
        if not self.qdrant or self._qdrant_ready:
            return
        try:
            from qdrant_client.http import models as qmodels
            collections = [c.name for c in self.qdrant.get_collections().collections]
            if self.COLLECTION_NAME not in collections:
                self.qdrant.create_collection(
                    collection_name=self.COLLECTION_NAME,
                    vectors_config=qmodels.VectorParams(
                        size=self._embed_dim,
                        distance=qmodels.Distance.COSINE,
                    ),
                )
                logger.info("Created Qdrant collection '%s' with dim=%d", self.COLLECTION_NAME, self._embed_dim)
            self._qdrant_ready = True
        except Exception as e:
            logger.warning("Failed to ensure Qdrant collection: %s", e)
            self._qdrant_ready = False

    # Compatibility wrappers for various qdrant-client versions
    def _qdrant_search(self, qv, limit=10, with_payload=True):
        if not self.qdrant:
            return []
        vec = qv.tolist() if hasattr(qv, 'tolist') else qv
        # Try common search signatures
        try:
            return self.qdrant.search(collection_name=self.COLLECTION_NAME, query_vector=vec, limit=limit, with_payload=with_payload)
        except Exception:
            pass
        try:
            return self.qdrant.search(self.COLLECTION_NAME, vec, limit=limit, with_payload=with_payload)
        except Exception:
            pass
        # Try underlying client if available
        client = getattr(self.qdrant, 'client', None) or getattr(self.qdrant, '_client', None)
        if client:
            try:
                return client.search(collection_name=self.COLLECTION_NAME, query_vector=vec, limit=limit, with_payload=with_payload)
            except Exception:
                pass
        raise AttributeError('No compatible qdrant search method')

    def _qdrant_upsert(self, points):
        if not self.qdrant:
            raise RuntimeError('Qdrant not configured')
        try:
            return self.qdrant.upsert(collection_name=self.COLLECTION_NAME, points=points)
        except Exception:
            pass
        try:
            return self.qdrant.upsert(points)
        except Exception:
            pass
        client = getattr(self.qdrant, 'client', None) or getattr(self.qdrant, '_client', None)
        if client:
            # try client.points or client.upsert
            points_api = getattr(client, 'points', None) or getattr(client, 'points_api', None)
            if points_api and hasattr(points_api, 'upsert'):
                try:
                    return points_api.upsert(collection_name=self.COLLECTION_NAME, points=points)
                except Exception:
                    pass
        raise RuntimeError('No compatible qdrant upsert method')

    def close(self):
        try:
            if getattr(self, 'qdrant', None):
                try:
                    self.qdrant.close()
                except Exception:
                    pass
                self.qdrant = None
        except Exception:
            pass

    def _real_embed(self, text: str) -> np.ndarray:
        """Embed using the real fastembed model."""
        vecs = list(self._fastembed.embed([text]))
        return np.array(vecs[0])

    def _pseudo_embed(self, text: str) -> np.ndarray:
        """Deterministic pseudo-embedding fallback. Not semantic — only for testing."""
        h = hashlib.sha256(text.encode("utf-8")).digest()
        base = np.frombuffer(h, dtype=np.uint8).astype(np.float32)
        # Tile to target dimension (384 = 32 * 12)
        arr = np.tile(base, self._embed_dim // len(base) + 1)[:self._embed_dim]
        return arr / (np.linalg.norm(arr) + 1e-8)

    def _load_all(self) -> List[Dict[str, Any]]:
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_all(self, data: List[Dict[str, Any]]):
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _similarity(self, v1: np.ndarray, v2: np.ndarray) -> float:
        return float(np.dot(v1, v2) / ((np.linalg.norm(v1) + 1e-8) * (np.linalg.norm(v2) + 1e-8)))

    def add_memory(self, user_id: int, text: str, meta: Dict[str, Any] = None) -> Dict[str, Any]:
        """Add memory with dedup/merge logic.

        - If new memory is nearly identical to existing (>IDENTICAL_THRESHOLD): skip.
        - If similar (>MERGE_THRESHOLD and <= IDENTICAL_THRESHOLD): merge by concatenation (placeholder).
        - Else: add as new entry.
        """
        qv = self.embed_fn(text)

        # Try Qdrant flow first if available
        if self.qdrant and self._qdrant_ready:
            try:
                return self._add_memory_qdrant(user_id, text, qv, meta)
            except Exception as e:
                logger.warning("Qdrant add_memory failed, falling back to JSON: %s", e)

        # JSON fallback flow
        return self._add_memory_json(user_id, text, qv, meta)

    def _add_memory_qdrant(self, user_id: int, text: str, qv: np.ndarray, meta: Dict[str, Any] = None) -> Dict[str, Any]:
        """Add memory via Qdrant."""
        from qdrant_client.http import models as qmodels

        # Search nearest to decide skip/merge
        try:
            hits = self._qdrant_search(qv, limit=10, with_payload=True)
        except Exception:
            hits = []

        best_hit = None
        best_sim = -1.0
        for h in hits:
            payload = h.payload if hasattr(h, 'payload') else {}
            if not payload:
                continue
            if int(payload.get('user_id', -1)) != int(user_id):
                continue
            score = h.score if hasattr(h, 'score') else -1.0
            if score > best_sim:
                best_sim = float(score)
                best_hit = h

        if best_hit is not None and best_sim >= self.IDENTICAL_THRESHOLD:
            return {"status": "skipped", "reason": "identical", "similarity": best_sim, "existing": best_hit.payload}

        if best_hit is not None and best_sim >= self.MERGE_THRESHOLD:
            existing_text = best_hit.payload.get('text', '')
            merged_text = existing_text + "\n" + text
            merged_vec = self.embed_fn(merged_text).tolist()
            point_id = best_hit.id if hasattr(best_hit, 'id') else str(uuid.uuid4())
            pt = qmodels.PointStruct(
                id=point_id,
                vector=merged_vec,
                payload={"user_id": int(user_id), "text": merged_text, "meta": meta or {}},
            )
            try:
                self._qdrant_upsert([pt])
            except Exception:
                logger.warning("Qdrant upsert (merge) failed")

            # Schedule async model-based merge if callback present
            if hasattr(self, 'merge_callback') and callable(self.merge_callback):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._async_merge_update_qdrant(point_id, existing_text, text, user_id, meta))
                except RuntimeError:
                    pass

            return {"status": "merged", "similarity": best_sim, "entry": {"text": merged_text, "meta": meta or {}}}

        # Add new point
        point_id = str(uuid.uuid4())
        payload = {"user_id": int(user_id), "text": text, "meta": meta or {}}
        pt = qmodels.PointStruct(id=point_id, vector=qv.tolist(), payload=payload)
        try:
            self._qdrant_upsert([pt])
        except Exception:
            logger.warning("Qdrant upsert failed, falling back to JSON save")
            # fallback to JSON add
            return self._add_memory_json(user_id, text, qv, meta)

        # Also save a human-readable JSON copy for debugging/inspection
        try:
            data = self._load_all()
            entry = {"user_id": int(user_id), "text": text, "vec": qv.tolist(), "meta": meta or {}}
            data.append(entry)
            self._save_all(data)
        except Exception:
            logger.debug("Failed to write JSON copy of memory, continuing")

        return {"status": "added", "entry": payload}

    def _add_memory_json(self, user_id: int, text: str, qv: np.ndarray, meta: Dict[str, Any] = None) -> Dict[str, Any]:
        """Add memory via JSON fallback."""
        data = self._load_all()

        best_sim = -1.0
        best_idx = None
        for idx, d in enumerate(data):
            if int(d.get("user_id", -1)) != int(user_id):
                continue
            v = np.array(d.get("vec", []))
            if v.size == 0:
                continue
            sim = self._similarity(qv, v)
            if sim > best_sim:
                best_sim = sim
                best_idx = idx

        if best_idx is not None and best_sim >= self.IDENTICAL_THRESHOLD:
            return {"status": "skipped", "reason": "identical", "similarity": best_sim, "existing": data[best_idx]}

        if best_idx is not None and best_sim >= self.MERGE_THRESHOLD:
            existing = data[best_idx]
            merged_text = existing.get("text", "") + "\n" + text
            merged_vec = self.embed_fn(merged_text).tolist()
            existing["text"] = merged_text
            existing["vec"] = merged_vec
            existing_meta = existing.get("meta", {})
            if meta:
                existing_meta.update(meta)
            existing["meta"] = existing_meta
            self._save_all(data)

            # schedule async merge if callback present
            if hasattr(self, 'merge_callback') and callable(self.merge_callback):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._async_merge_update_json(best_idx, existing.get('text', ''), text, user_id, meta))
                except RuntimeError:
                    pass

            return {"status": "merged", "similarity": best_sim, "entry": existing}

        # Add new
        vec = qv.tolist()
        entry = {"user_id": int(user_id), "text": text, "vec": vec, "meta": meta or {}}
        data.append(entry)
        self._save_all(data)
        return {"status": "added", "entry": entry}

    def search(self, user_id: int, query: str, top_k: int = 5, threshold: float = 0.8) -> List[Dict[str, Any]]:
        from imports.utils.logger import get_user_logger
        ulog = get_user_logger(user_id)
        ulog.info("Memory search query: '%s' (threshold=%.2f)", query, threshold)
        
        qv = self.embed_fn(query)
        final_results = []
        
        # Try Qdrant search first
        if self.qdrant and self._qdrant_ready:
            try:
                hits = self._qdrant_search(qv, limit=max(top_k * 3, 10), with_payload=True)
                for h in hits:
                    score = h.score if hasattr(h, 'score') else 0.0
                    if score < threshold:
                        continue
                    payload = h.payload if hasattr(h, 'payload') else {}
                    if not payload:
                        continue
                    if int(payload.get('user_id', -1)) != int(user_id):
                        continue
                    # Keep track of similarity score for logging
                    payload['_sim'] = score
                    final_results.append(payload)
                    if len(final_results) >= top_k:
                        break
            except Exception as e:
                ulog.warning("Qdrant search failed, falling back to JSON: %s", e)

        # JSON fallback if Qdrant didn't return results
        if not final_results:
            data = [d for d in self._load_all() if int(d.get("user_id", -1)) == int(user_id)]
            results = []
            for d in data:
                v = np.array(d.get("vec"))
                if v.size == 0:
                    continue
                sim = self._similarity(qv, v)
                if sim < threshold:
                    continue
                results.append((sim, d))
            results.sort(key=lambda x: x[0], reverse=True)
            for s, d in results[:top_k]:
                d['_sim'] = float(s)
                final_results.append(d)
                
        # Log the results
        filtered_results = []
        for idx, res in enumerate(final_results):
            sim = res.pop('_sim', 0.0) # remove it so it doesn't pollute the payload
            title = res.get('meta', {}).get('title', 'Memory')
            text = res.get('text', '')
            ulog.info("Memory %d: sim=%.4f title='%s' text='%s'", idx+1, sim, title, text)
            filtered_results.append(res)
            
        return filtered_results

    async def _async_merge_update_qdrant(self, point_id: str, existing_text: str, new_text: str, user_id: int, meta: Dict[str, Any] = None):
        try:
            if not hasattr(self, 'merge_callback'):
                return
            if asyncio.iscoroutinefunction(self.merge_callback):
                merged = await self.merge_callback(existing_text, new_text)
            else:
                merged = self.merge_callback(existing_text, new_text)

            if not merged:
                return
            merged_vec = self.embed_fn(merged).tolist()
            from qdrant_client.http import models as qmodels
            pt = qmodels.PointStruct(
                id=point_id,
                vector=merged_vec,
                payload={"user_id": int(user_id), "text": merged, "meta": meta or {}},
            )
            if self.qdrant:
                self.qdrant.upsert(collection_name=self.COLLECTION_NAME, points=[pt])
        except Exception as e:
            logger.warning("Async merge update (qdrant) failed: %s", e)

    async def _async_merge_update_json(self, idx: int, existing_text: str, new_text: str, user_id: int, meta: Dict[str, Any] = None):
        try:
            if not hasattr(self, 'merge_callback'):
                return
            if asyncio.iscoroutinefunction(self.merge_callback):
                merged = await self.merge_callback(existing_text, new_text)
            else:
                merged = self.merge_callback(existing_text, new_text)
            if not merged:
                return
            data = self._load_all()
            if idx < 0 or idx >= len(data):
                return
            entry = data[idx]
            entry['text'] = merged
            entry['vec'] = self.embed_fn(merged).tolist()
            if meta:
                m = entry.get('meta', {})
                m.update(meta)
                entry['meta'] = m
            self._save_all(data)
        except Exception as e:
            logger.warning("Async merge update (json) failed: %s", e)

    # Simple helper for manual memory addition tool
    def add_memory_manual(self, user_id: int, title: str, text: str) -> Dict[str, Any]:
        meta = {"title": title}
        return self.add_memory(user_id, text, meta=meta)
