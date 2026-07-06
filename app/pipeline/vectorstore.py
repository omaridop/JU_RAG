"""Chroma vector store wrapper.

Two persistent collections live side by side:
  * knowledge   (``ju_knowledge``) — document chunks + manual Q&A pairs
  * memory      (``ju_memory``)     — admin-promoted answers (semantic cache)

Adds are **upserts**, so re-ingesting a file or appending a new one never
rebuilds the corpus — vectors are added on top of what already exists.

Retrieval is **recency-aware**: we over-fetch by cosine similarity, then blend
in a recency score derived from each record's ``doc_date`` so the newest
information wins on near-ties.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Optional, Sequence

import chromadb
from chromadb.config import Settings as ChromaSettings

from ..config import settings
from ..core.utils import parse_dt, recency_score

log = logging.getLogger(__name__)

_PRIMITIVE = (str, int, float, bool)


def _dedupe_key(meta: dict, doc: str) -> tuple:
    """Collapse records that carry the same fact.

    Q&A ingestion fans one answer out into many question-paraphrase vectors;
    without this, the top-k context can be k copies of the same answer.
    """
    meta = meta or {}
    unit = meta.get("unit")
    if unit is not None:
        return ("unit", meta.get("doc_id"), unit)
    answer = meta.get("answer")
    if answer:
        return ("answer", meta.get("doc_id"), answer)
    return ("doc", meta.get("doc_id"), doc)


def _clean_meta(meta: dict) -> dict:
    """Chroma only accepts str/int/float/bool scalars. Coerce everything else."""
    out: dict[str, Any] = {}
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, _PRIMITIVE):
            out[k] = v
        elif isinstance(v, (list, tuple, set)):
            out[k] = "; ".join(str(x) for x in v)
        else:
            out[k] = str(v)
    return out


class VectorStore:
    def __init__(self) -> None:
        self._client: Optional[chromadb.api.ClientAPI] = None
        self._kb = None
        self._mem = None

    # --- lazy handles ----------------------------------------------------- #
    @property
    def client(self):
        if self._client is None:
            self._client = chromadb.PersistentClient(
                path=str(settings.CHROMA_DIR),
                settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
            )
        return self._client

    @property
    def kb(self):
        if self._kb is None:
            self._kb = self.client.get_or_create_collection(
                settings.COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
            )
        return self._kb

    @property
    def memory(self):
        if self._mem is None:
            self._mem = self.client.get_or_create_collection(
                settings.MEMORY_COLLECTION, metadata={"hnsw:space": "cosine"}
            )
        return self._mem

    # --- knowledge base --------------------------------------------------- #
    def add_records(self, records: Sequence[dict]) -> int:
        """Upsert chunk/QA records. Each: {id, embedding, document, metadata}."""
        if not records:
            return 0
        self.kb.upsert(
            ids=[r["id"] for r in records],
            embeddings=[r["embedding"] for r in records],
            documents=[r["document"] for r in records],
            metadatas=[_clean_meta(r["metadata"]) for r in records],
        )
        return len(records)

    def delete_doc(self, doc_id: int) -> None:
        self.kb.delete(where={"doc_id": int(doc_id)})

    def count(self) -> int:
        try:
            return int(self.kb.count())
        except Exception:  # noqa: BLE001
            return 0

    def search(self, query_embedding: list[float], *, top_k: Optional[int] = None,
               candidate_k: Optional[int] = None, recency_weight: Optional[float] = None,
               halflife_days: Optional[float] = None,
               where: Optional[dict] = None,
               exclude_doc_ids: Optional[set] = None) -> list[dict]:
        """Recency-aware similarity search with per-fact deduplication.

        Over-fetches ``candidate_k`` by cosine similarity, blends in recency,
        collapses paraphrase duplicates, and returns up to ``top_k`` records.
        ``exclude_doc_ids`` drops vectors belonging to deactivated documents
        (treated as if deleted) before scoring.
        """
        top_k = top_k or settings.TOP_K
        candidate_k = candidate_k or settings.CANDIDATE_K
        rw = settings.RECENCY_WEIGHT if recency_weight is None else recency_weight
        hl = settings.RECENCY_HALFLIFE_DAYS if halflife_days is None else halflife_days

        count = self.count()
        if count == 0:
            return []

        n = max(1, min(max(candidate_k, top_k), count))
        res = self.kb.query(
            query_embeddings=[query_embedding],
            n_results=n,
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]

        candidates: list[dict] = []
        for _id, doc, meta, dist in zip(ids, docs, metas, dists):
            if exclude_doc_ids:
                try:
                    if int((meta or {}).get("doc_id")) in exclude_doc_ids:
                        continue  # vector belongs to a deactivated document
                except (TypeError, ValueError):
                    pass
            similarity = max(0.0, 1.0 - float(dist))  # cosine distance -> similarity
            if similarity < settings.SCORE_THRESHOLD:
                continue
            rec = recency_score(parse_dt((meta or {}).get("doc_date")), hl)
            score = (1.0 - rw) * similarity + rw * rec
            candidates.append({
                "id": _id,
                "document": doc,
                "metadata": meta or {},
                "similarity": round(similarity, 4),
                "recency": round(rec, 4),
                "score": round(score, 4),
            })

        candidates.sort(key=lambda c: c["score"], reverse=True)

        # Keep only the best-scoring record per fact (paraphrases collapse).
        out: list[dict] = []
        seen: set = set()
        for c in candidates:
            key = _dedupe_key(c["metadata"], c["document"])
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= top_k:
                break
        return out

    # --- memory (semantic cache) ----------------------------------------- #
    def memory_add(self, vector_id: str, embedding: list[float], document: str,
                   metadata: dict) -> None:
        self.memory.upsert(
            ids=[vector_id],
            embeddings=[embedding],
            documents=[document],
            metadatas=[_clean_meta(metadata)],
        )

    def memory_search(self, query_embedding: list[float], k: int = 3) -> list[dict]:
        try:
            if int(self.memory.count()) == 0:
                return []
        except Exception:  # noqa: BLE001
            return []
        res = self.memory.query(
            query_embeddings=[query_embedding],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )
        ids = res.get("ids", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        out = []
        for _id, meta, dist in zip(ids, metas, dists):
            out.append({
                "id": _id,
                "metadata": meta or {},
                "similarity": round(max(0.0, 1.0 - float(dist)), 4),
            })
        out.sort(key=lambda c: c["similarity"], reverse=True)
        return out

    def memory_delete(self, vector_ids: Sequence[str]) -> None:
        ids = [v for v in vector_ids if v]
        if ids:
            self.memory.delete(ids=ids)


@lru_cache(maxsize=1)
def get_store() -> VectorStore:
    return VectorStore()
