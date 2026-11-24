from opspilot.agent.llm import build_chat_model
from opspilot.config import Settings


def test_blank_secrets_mean_unset() -> None:
    """Blank values, as .env.example ships them, switch features off instead of crashing."""
    settings = Settings(_env_file=None, admin_token="", gemini_api_key="  ")  # type: ignore[call-arg]
    assert settings.admin_token is None
    assert settings.gemini_api_key is None
    assert build_chat_model(settings) is None
