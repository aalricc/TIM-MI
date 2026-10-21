from __future__ import annotations

import pytest

from mi_agent.config import MISettings


@pytest.fixture(autouse=True)
def _clear_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "OPENAI_API_KEY",
        "MI_OPENAI_API_KEY",
        "JEV_API_KEY",
        "MI_JEV_API_KEY",
        "PINECONE_API_KEY",
        "MI_PINECONE_API_KEY",
        "PINECONE_INDEX_HOST",
        "MI_PINECONE_INDEX_HOST",
        "MI_RAG_QUERY_PREFIX",
        "MI_RAG_ENABLED",
        "MI_JEV_ENABLED",
        "MI_CLASSMEM_ENABLED",
        "MI_CLASSMEM_FASTPATH_ENABLED",
    ]:
        monkeypatch.delenv(key, raising=False)


def _settings(**kwargs: object) -> MISettings:
    return MISettings(_env_file=None, **kwargs)


def test_rag_gate_requires_pinecone_config() -> None:
    with pytest.raises(ValueError, match="PINECONE_API_KEY"):
        _settings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            pinecone_index_host="example-host",
        )


def test_openai_key_is_required() -> None:
    with pytest.raises(ValueError):
        _settings()


def test_jev_gate_requires_key_when_enabled() -> None:
    with pytest.raises(ValueError, match="JEV_API_KEY"):
        _settings(
            openai_api_key="test-key",
            jev_api_key="",
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )


def test_rag_and_jev_flags_are_mandatory() -> None:
    with pytest.raises(ValueError, match="MI_RAG_ENABLED"):
        _settings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            rag_enabled=False,
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )

    with pytest.raises(ValueError, match="MI_JEV_ENABLED"):
        _settings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            jev_enabled=False,
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )

    with pytest.raises(ValueError, match="MI_CLASSMEM_ENABLED"):
        _settings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            classmem_enabled=False,
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )

    with pytest.raises(ValueError, match="MI_CLASSMEM_FASTPATH_ENABLED"):
        _settings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            classmem_fastpath_enabled=False,
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )


def test_valid_rag_config_passes() -> None:
    settings = _settings(
        openai_api_key="test-key",
        jev_api_key="test-jev-key",
        pinecone_api_key="pc-key",
        pinecone_index_host="pc-host",
    )
    assert settings.rag_enabled is True
    assert settings.jev_enabled is True
    assert settings.classmem_enabled is True
    assert settings.classmem_fastpath_enabled is True
    assert settings.pinecone_namespace == "sqlite-docs"


def test_rag_query_prefix_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        openai_api_key="test-key",
        jev_api_key="test-jev-key",
        pinecone_api_key="pc-key",
        pinecone_index_host="pc-host",
    )
    assert settings.rag_query_prefix == "sqlite"

    monkeypatch.setenv("MI_RAG_QUERY_PREFIX", "sqlite recovery")
    override_settings = _settings(
        openai_api_key="test-key",
        jev_api_key="test-jev-key",
        pinecone_api_key="pc-key",
        pinecone_index_host="pc-host",
    )
    assert override_settings.rag_query_prefix == "sqlite recovery"

    monkeypatch.setenv("MI_RAG_QUERY_PREFIX", "")
    empty_settings = _settings(
        openai_api_key="test-key",
        jev_api_key="test-jev-key",
        pinecone_api_key="pc-key",
        pinecone_index_host="pc-host",
    )
    assert empty_settings.rag_query_prefix == ""
