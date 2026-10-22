from __future__ import annotations

from pathlib import Path

import pytest

from a_core.c_rag.index import build_index, load_config, mark_index_stale
from a_core.c_rag.retrieve import IndexUnavailableError, search

from a_core.c_rag.tests.test_index import FakeGitHub, write_config


def indexed_config(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    build_index(
        config,
        client=FakeGitHub(
            "def authenticate(user, password):\n"
            "    return verify_password(user, password)\n"
            "\n"
            "def create_session(user):\n"
            "    return session_store.save(user)\n"
        ),
    )
    return config


def test_retrieval_returns_ranked_grounded_result(tmp_path: Path):
    config = indexed_config(tmp_path)
    response = search("authenticate password", config, top_k=1)
    assert response.has_sufficient_information
    assert len(response.results) == 1
    result = response.results[0]
    assert result.repository == "glic-org/glic-project"
    assert result.path == "src/auth.py"
    assert result.commit_sha == "c" * 40
    assert result.start_line >= 1
    assert "verify_password" in result.content
    assert result.score > 0


def test_retrieval_reports_when_no_result_is_relevant(tmp_path: Path):
    config = indexed_config(tmp_path)
    response = search("xylophone quasar nebula", config)
    assert not response.has_sufficient_information
    assert response.results == ()
    assert response.warning is not None
    assert "Nenhuma informação" in response.warning


def test_retrieval_warns_when_last_sync_failed(tmp_path: Path):
    config = indexed_config(tmp_path)
    mark_index_stale(config, "falha simulada da API")
    response = search("authenticate password", config)
    assert response.results
    assert response.index_status == "stale"
    assert response.warning is not None
    assert "desatualizado" in response.warning
    assert "falha simulada" in response.warning


def test_retrieval_requires_an_index(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    with pytest.raises(IndexUnavailableError, match="não encontrado"):
        search("autenticação", config)


def test_retrieval_rejects_empty_query(tmp_path: Path):
    config = indexed_config(tmp_path)
    with pytest.raises(ValueError, match="vazia"):
        search("   ", config)
