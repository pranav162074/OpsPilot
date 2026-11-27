"""Re-ranking: a second, slower, more precise pass over the top candidates.

Hybrid search scores the query and each passage separately (an embedding each, or keyword
statistics), so it can't tell "pool exhaustion caused by long transactions" from a passage
that merely mentions pools and transactions. A cross-encoder reads the query and the passage
together and scores how well one answers the other: far better at ordering, far too slow to
run over a whole corpus. So search finds ~20 candidates cheaply and the cross-encoder orders
those.

The model (ms-marco-MiniLM-L-6-v2, 80 MB, Apache-2.0) runs locally on the CPU through ONNX
(fastembed): free, no API, a few milliseconds per query. It's optional: install it with
`uv sync --extra rerank` and set RERANKER=cross-encoder.
"""

import asyncio
import functools
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from opspilot.config import Settings

logger = logging.getLogger(__name__)


class RerankerUnavailable(Exception):
    """The model can't be loaded (not installed, or the download failed)."""


class Reranker(Protocol):
    name: str

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """A relevance score per passage, higher is better; only the order matters."""
        ...


class CrossEncoderReranker:
    def __init__(self, model: str, cache_dir: str | None = None) -> None:
        self.name = model
        self._cache_dir = cache_dir
        self._model: Any = None
        self._lock = asyncio.Lock()

    def _load(self) -> Any:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            raise RerankerUnavailable(
                "The re-ranker isn't installed: uv sync --extra rerank"
            ) from exc
        try:
            return TextCrossEncoder(self.name, cache_dir=self._cache_dir)
        except Exception as exc:  # a failed download, a corrupt cache
            raise RerankerUnavailable(f"Couldn't load {self.name}: {exc}") from exc

    async def _ready(self) -> Any:
        # Loaded once, on first use; the lock stops parallel requests loading it twice
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load)
        return self._model

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        model = await self._ready()
        # CPU-bound: off the event loop
        scores = await asyncio.to_thread(lambda: list(model.rerank(query, list(passages))))
        return [float(s) for s in scores]

    async def warm_up(self) -> None:
        await self.score("warm up", ["warm up"])


@functools.cache
def _shared(model: str, cache_dir: str | None) -> CrossEncoderReranker:
    # One per process: loading takes seconds and the model is read-only, so the API and every
    # worker task share it
    return CrossEncoderReranker(model, cache_dir)


def build_reranker(settings: Settings) -> Reranker | None:
    if settings.reranker == "cross-encoder":
        return _shared(settings.reranker_model, settings.reranker_cache_dir)
    return None
