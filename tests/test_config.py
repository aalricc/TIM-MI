from __future__ import annotations

import pytest

from mi_agent.config import MISettings


def test_rag_gate_requires_pinecone_config() -> None:
    with pytest.raises(ValueError, match="PINECONE_API_KEY"):
        MISettings(
            openai_api_key="test-key",
            rag_enabled=True,
            pinecone_index_host="example-host",
        )


def test_openai_key_is_required() -> None:
    with pytest.raises(ValueError):
        MISettings()


def test_jev_gate_requires_key_when_enabled() -> None:
    with pytest.raises(ValueError, match="JEV_API_KEY"):
        MISettings(
            openai_api_key="test-key",
            jev_enabled=True,
            jev_api_key="",
        )


def test_valid_rag_config_passes() -> None:
    settings = MISettings(
        openai_api_key="test-key",
        rag_enabled=True,
        pinecone_api_key="pc-key",
        pinecone_index_host="pc-host",
    )
    assert settings.rag_enabled is True
    assert settings.pinecone_namespace == "sqlite-docs"
