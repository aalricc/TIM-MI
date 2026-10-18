from __future__ import annotations

import pytest

from mi_agent.config import MISettings


def test_rag_gate_requires_pinecone_config() -> None:
    with pytest.raises(ValueError, match="PINECONE_API_KEY"):
        MISettings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            pinecone_index_host="example-host",
        )


def test_openai_key_is_required() -> None:
    with pytest.raises(ValueError):
        MISettings()


def test_jev_gate_requires_key_when_enabled() -> None:
    with pytest.raises(ValueError, match="JEV_API_KEY"):
        MISettings(
            openai_api_key="test-key",
            jev_api_key="",
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )


def test_rag_and_jev_flags_are_mandatory() -> None:
    with pytest.raises(ValueError, match="MI_RAG_ENABLED"):
        MISettings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            rag_enabled=False,
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )

    with pytest.raises(ValueError, match="MI_JEV_ENABLED"):
        MISettings(
            openai_api_key="test-key",
            jev_api_key="test-jev-key",
            jev_enabled=False,
            pinecone_api_key="pc-key",
            pinecone_index_host="pc-host",
        )


def test_valid_rag_config_passes() -> None:
    settings = MISettings(
        openai_api_key="test-key",
        jev_api_key="test-jev-key",
        pinecone_api_key="pc-key",
        pinecone_index_host="pc-host",
    )
    assert settings.rag_enabled is True
    assert settings.jev_enabled is True
    assert settings.pinecone_namespace == "sqlite-docs"
