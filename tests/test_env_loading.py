from pathlib import Path

import pytest

from agentcrash.providers.meta_responses import (
    API_KEY_ENV_VAR,
    PROJECT_API_KEY_ENV_VAR,
    MissingCredentialsError,
    default_dotenv_path,
    load_config_from_env,
)


def _clear_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (API_KEY_ENV_VAR, PROJECT_API_KEY_ENV_VAR):
        monkeypatch.delenv(var, raising=False)


def test_dotenv_file_supplies_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_keys(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY=fake-test-key-1\n", encoding="utf-8")

    config = load_config_from_env(dotenv_path=env_file)

    assert config.api_key == "fake-test-key-1"
    assert config.base_url == "https://api.meta.ai/v1/responses"
    assert config.model == "muse-spark-1.3"


def test_project_override_wins_in_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_keys(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MODEL_API_KEY=fake-official-key\n"
        "AGENTCRASH_META_API_KEY=fake-project-key\n",
        encoding="utf-8",
    )

    config = load_config_from_env(dotenv_path=env_file)

    assert config.api_key == "fake-project-key"


def test_explicit_environ_wins_over_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY=fake-file-key\n", encoding="utf-8")

    config = load_config_from_env(
        {"MODEL_API_KEY": "fake-explicit-key"}, dotenv_path=env_file
    )

    assert config.api_key == "fake-explicit-key"


def test_real_environ_wins_over_dotenv_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_keys(monkeypatch)
    monkeypatch.setenv(API_KEY_ENV_VAR, "fake-real-env-key")
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY=fake-file-key\n", encoding="utf-8")

    config = load_config_from_env(dotenv_path=env_file)

    assert config.api_key == "fake-real-env-key"


def test_missing_key_still_raises_with_empty_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_keys(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("AGENTCRASH_META_MODEL=muse-spark-1.3\n", encoding="utf-8")

    with pytest.raises(MissingCredentialsError):
        load_config_from_env(dotenv_path=env_file)


def test_dotenv_example_has_placeholder_only() -> None:
    example = Path(__file__).resolve().parents[1] / ".env.example"
    assert example.exists()
    content = example.read_text(encoding="utf-8")
    assert content == "MODEL_API_KEY=\n"


def test_default_dotenv_path_is_repo_root() -> None:
    assert default_dotenv_path().name == ".env"
    assert default_dotenv_path().parent.name == "agentcrash"
