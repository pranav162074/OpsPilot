from opspilot.config import Settings
from opspilot.embeddings.base import Embedder
from opspilot.embeddings.gemini import GeminiEmbedder
from opspilot.embeddings.hashing import HashingEmbedder


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "gemini":
        if settings.gemini_api_key is None:
            raise ValueError("EMBEDDING_PROVIDER=gemini needs GEMINI_API_KEY")
        return GeminiEmbedder(
            settings.gemini_api_key.get_secret_value(), model=settings.embedding_model
        )
    return HashingEmbedder()
