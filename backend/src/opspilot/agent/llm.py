from langchain_core.language_models import BaseChatModel

from opspilot.config import Settings


def build_chat_model(settings: Settings) -> BaseChatModel | None:
    """The agent's model, or None when no provider is configured (investigations then 503)."""
    if settings.llm_provider == "none" or settings.gemini_api_key is None:
        return None
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.llm_model,
        api_key=settings.gemini_api_key,
        # Deterministic as possible: an on-call assistant should give the same answer twice
        temperature=0,
        retries=2,
        request_timeout=30,
    )
