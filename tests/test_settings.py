from crashtest.settings import Settings


def test_settings_defaults_and_secret_repr():
    settings = Settings(openai_api_key="application", openai_executor_api_key="secret-value-two", _env_file=None)
    assert settings.crashtest_decisions_model == "gpt-6-luna"
    assert "application" not in repr(settings)
    assert "secret-value-two" not in repr(settings)
