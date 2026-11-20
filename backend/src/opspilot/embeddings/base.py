import math
from typing import Protocol

# Fixed by the database column (vector(768)). Changing it needs a migration and a re-index.
DIMENSIONS = 768


class EmbeddingError(Exception):
    """A permanent failure: bad API key, rejected input. Retrying won't help."""


class EmbeddingUnavailable(Exception):
    """A temporary failure: rate limited, provider down, network error. Worth retrying."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Embedder(Protocol):
    """Turns text into vectors whose cosine similarity reflects similarity of meaning."""

    # Stored with every chunk: vectors from different models can't be compared, so search
    # only uses chunks embedded by the current model
    model: str
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


def normalize(vector: list[float]) -> list[float]:
    """Scales to length 1, so cosine similarity is a plain dot product and scores compare."""
    length = math.sqrt(sum(v * v for v in vector))
    if length == 0:
        return vector
    return [v / length for v in vector]
