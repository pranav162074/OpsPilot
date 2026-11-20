import logging
from typing import Any

import httpx

from opspilot.embeddings.base import (
    DIMENSIONS,
    EmbeddingError,
    EmbeddingUnavailable,
    normalize,
)

logger = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
# The API accepts up to 100 texts per batch request
MAX_BATCH = 100


class GeminiEmbedder:
    """Embeddings from Google's Gemini API (gemini-embedding-001 by default).

    Documents and queries are embedded with different task types: the model places a question
    near the passages that answer it, rather than near other questions.
    """

    dimensions = DIMENSIONS

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-001",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_BATCH):
            vectors += await self._batch(texts[start : start + MAX_BATCH], "RETRIEVAL_DOCUMENT")
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._batch([text], "RETRIEVAL_QUERY"))[0]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _batch(self, texts: list[str], task_type: str) -> list[list[float]]:
        model = f"models/{self.model}"
        body = {
            "requests": [
                {
                    "model": model,
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    # The model's full output is 3072 numbers; it's trained so the first 768
                    # work on their own, at a quarter of the storage
                    "outputDimensionality": self.dimensions,
                }
                for text in texts
            ]
        }
        try:
            response = await self._client.post(
                f"{API}/{model}:batchEmbedContents",
                json=body,
                headers={"x-goog-api-key": self._api_key},
            )
        except httpx.TransportError as exc:  # timeouts, connection errors
            raise EmbeddingUnavailable(f"Embedding request failed: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("retry-after")
            raise EmbeddingUnavailable(
                f"Embedding API returned {response.status_code}",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code != 200:
            # The body says what was wrong (bad key, bad model name); never log the key itself
            raise EmbeddingError(
                f"Embedding API returned {response.status_code}: {response.text[:300]}"
            )

        payload: dict[str, Any] = response.json()
        embeddings = payload.get("embeddings") or []
        if len(embeddings) != len(texts):
            raise EmbeddingError(f"Asked for {len(texts)} embeddings, got {len(embeddings)}")
        vectors = []
        for item in embeddings:
            values = [float(v) for v in item.get("values", [])]
            if len(values) != self.dimensions:
                raise EmbeddingError(f"Expected {self.dimensions} dimensions, got {len(values)}")
            # Shortened embeddings aren't unit length; normalize so cosine scores are comparable
            vectors.append(normalize(values))
        return vectors
