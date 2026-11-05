from pathlib import Path

import pytest

from factory import moma


def test_env_file_is_read_with_bom_comments_and_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        '﻿# MoMA\nMOMA_API_BASE="https://example.invalid/v1/"\n\nMOMA_API_KEY=sk-test\n',
        encoding="utf-8",
    )
    assert moma.read_env_file(env) == {
        "MOMA_API_BASE": "https://example.invalid/v1/",
        "MOMA_API_KEY": "sk-test",
    }


def test_options_prefer_the_environment_and_trim_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "MOMA_API_BASE=https://file.invalid/v1/\nMOMA_API_KEY=from-file\n", encoding="utf-8"
    )
    monkeypatch.setenv("MOMA_API_KEY", "from-env")
    monkeypatch.delenv("MOMA_API_BASE", raising=False)
    assert moma.options(env) == {"api_base": "https://file.invalid/v1", "api_key": "from-env"}


def test_a_missing_key_is_named_without_echoing_anything_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("MOMA_API_BASE=https://file.invalid/v1\n", encoding="utf-8")
    monkeypatch.delenv("MOMA_API_KEY", raising=False)
    monkeypatch.delenv("MOMA_API_BASE", raising=False)
    with pytest.raises(RuntimeError, match="MOMA_API_KEY"):
        moma.options(env)


def test_model_names_get_the_openai_prefix_once() -> None:
    assert moma.model_name("deepseek-ai/DeepSeek-V3") == "openai/deepseek-ai/DeepSeek-V3"
    assert moma.model_name("openai/qwen-plus") == "openai/qwen-plus"
