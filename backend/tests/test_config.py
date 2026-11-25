from opspilot.agent.llm import build_chat_model
from opspilot.config import Settings


def test_blank_secrets_mean_unset() -> None:
    """Blank values, as .env.example ships them, switch features off instead of crashing."""
    settings = Settings(_env_file=None, admin_token="", gemini_api_key="  ")  # type: ignore[call-arg]
    assert settings.admin_token is None
    assert settings.gemini_api_key is None
    assert build_chat_model(settings) is None


def test_private_integration_urls_default_by_environment() -> None:
    dev = Settings(_env_file=None, integrations_allow_private_urls="")  # type: ignore[call-arg]
    prod = Settings(_env_file=None, environment="production")  # type: ignore[call-arg]
    forced = Settings(  # type: ignore[call-arg]
        _env_file=None, environment="production", integrations_allow_private_urls=True
    )
    assert dev.allow_private_integration_urls is True
    assert prod.allow_private_integration_urls is False
    assert forced.allow_private_integration_urls is True
