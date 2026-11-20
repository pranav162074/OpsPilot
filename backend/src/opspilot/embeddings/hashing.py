import hashlib
import re

from opspilot.embeddings.base import DIMENSIONS, normalize

_TOKEN = re.compile(r"[a-z0-9_]+")


class HashingEmbedder:
    """A local embedder with no model and no API: feature hashing of words and word pairs.

    It captures word overlap, not meaning ("restart" and "reboot" share nothing), so it's for
    tests, CI and offline development. Deterministic and instant.
    """

    model = "hashing-v1"
    dimensions = DIMENSIONS

    def _embed(self, text: str) -> list[float]:
        words = _TOKEN.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
        vector = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            # A hash-chosen sign keeps unrelated features from piling up in one direction
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        return normalize(vector)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    async def aclose(self) -> None:
        return None
