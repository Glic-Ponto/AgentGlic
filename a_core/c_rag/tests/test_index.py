from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from a_core.c_rag.github_client import (
    GitHubAuthenticationError,
    GitHubClient,
    GitHubRateLimitError,
    UnauthorizedRepositoryError,
)
from a_core.c_rag.index import (
    ConfigurationError,
    build_index,
    chunk_text,
    contains_secret,
    is_path_eligible,
    load_config,
)


COMMIT_SHA = "c" * 40
BLOB_SHA = "a" * 40


def write_config(tmp_path: Path, **rag_overrides: object) -> Path:
    rag = {
        "enabled": True,
        "provider": "local",
        "model": "feature-hashing-v1",
        "embedding_dimensions": 64,
        "store_path": "a_core/rag/store",
        "chunk_size": 120,
        "chunk_overlap": 20,
        "top_k": 5,
        "min_score": 0.05,
        "max_file_size_kb": 64,
    }
    rag.update(rag_overrides)
    lines = [
        "project:",
        "  name: Glic Project",
        "github:",
        "  owner: glic-org",
        "  repo: glic-project",
        "  ref: null",
        "  timeout_seconds: 10",
        "rag:",
    ]
    for key, value in rag.items():
        yaml_value = str(value).lower() if isinstance(value, bool) else value
        lines.append(f"  {key}: {yaml_value}")
    lines.extend(
        [
            "paths:",
            "  knowledge: a_core/rag/knowledge",
            "  workspace_input: c_workspace/ws_input",
            "  workspace_output: c_workspace/ws_output",
        ]
    )
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class FakeGitHub:
    def __init__(self, content: str = "def authenticate(user):\n    return verify_password(user)\n"):
        self.content = content
        self.blob_calls = 0

    def assert_authorized_repository(self, owner: str, repo: str) -> None:
        assert (owner, repo) == ("glic-org", "glic-project")

    def repository_metadata(self):
        return {"full_name": "glic-org/glic-project", "default_branch": "main"}

    def resolve_ref(self, ref: str) -> str:
        assert ref == "main"
        return COMMIT_SHA

    def list_tree(self, commit_sha: str):
        assert commit_sha == COMMIT_SHA
        return [{"path": "src/auth.py", "sha": BLOB_SHA, "size": len(self.content)}]

    def get_text_blob(self, blob_sha: str, *, max_bytes: int):
        assert blob_sha == BLOB_SHA
        assert len(self.content) <= max_bytes
        self.blob_calls += 1
        return self.content


class FakeResponse:
    def __init__(self, status_code: int, payload=None, *, headers=None, links=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.links = links or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_invalid_configuration_rejects_placeholders(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """
project: {name: Glic Project}
github: {owner: CONFIGURAR_ORGANIZACAO, repo: CONFIGURAR_REPOSITORIO}
rag:
  enabled: true
  provider: local
  store_path: a_core/rag/store
  chunk_size: 800
  chunk_overlap: 120
  top_k: 5
  max_file_size_kb: 256
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="github.owner"):
        load_config(config)


def test_configuration_rejects_store_outside_repository(tmp_path: Path):
    config_path = write_config(tmp_path, store_path="../outside")
    with pytest.raises(ConfigurationError, match="não pode sair"):
        load_config(config_path)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        "config/private.pem",
        "node_modules/pkg/index.js",
        "dist/app.js",
        "credentials.json",
        "image.png",
    ],
)
def test_sensitive_or_irrelevant_paths_are_excluded(path: str):
    assert not is_path_eligible(path, size=20, max_bytes=1024)


def test_secret_patterns_are_detected_before_persistence():
    assert contains_secret("github_pat_abcdefghijklmnopqrstuvwxyz123456")
    assert contains_secret("-----BEGIN PRIVATE KEY-----\nabc")
    assert contains_secret("password = 'this-is-a-real-secret-value'")
    assert not contains_secret("token = os.environ['GITHUB_TOKEN']")


def test_chunking_preserves_overlap_and_line_references():
    text = "\n".join(f"linha {number} autenticação" for number in range(1, 20))
    chunks = chunk_text(text, chunk_size=100, overlap=20)
    assert len(chunks) > 1
    assert chunks[0].start_line == 1
    assert all(chunk.start_line <= chunk.end_line for chunk in chunks)
    assert all(len(chunk.content) <= 100 for chunk in chunks)


def test_github_authentication_and_rate_limit_errors_do_not_expose_token():
    token = "super-secret-token"
    auth_session = FakeSession([FakeResponse(401)])
    client = GitHubClient("glic-org", "glic-project", token, session=auth_session)
    with pytest.raises(GitHubAuthenticationError) as auth_error:
        client.repository_metadata()
    assert token not in str(auth_error.value)

    rate_session = FakeSession(
        [FakeResponse(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "42"})]
    )
    rate_client = GitHubClient("glic-org", "glic-project", token, session=rate_session)
    with pytest.raises(GitHubRateLimitError, match="42"):
        rate_client.repository_metadata()


def test_client_blocks_other_repositories_and_disables_redirects():
    session = FakeSession([FakeResponse(200, {"default_branch": "main"})])
    client = GitHubClient("glic-org", "glic-project", "token-value", session=session)
    with pytest.raises(UnauthorizedRepositoryError):
        client.assert_authorized_repository("other-org", "other-repo")
    client.repository_metadata()
    assert session.calls[0][0] == "https://api.github.com/repos/glic-org/glic-project/"
    assert session.calls[0][1]["allow_redirects"] is False


def test_blob_decoding_rejects_binary_and_decodes_utf8():
    raw_encoded = base64.b64encode("olá".encode()).decode()
    encoded = f"{raw_encoded[:4]}\n{raw_encoded[4:]}"
    session = FakeSession([FakeResponse(200, {"size": 4, "encoding": "base64", "content": encoded})])
    client = GitHubClient("glic-org", "glic-project", "token-value", session=session)
    assert client.get_text_blob(BLOB_SHA, max_bytes=20) == "olá"


def test_index_persists_chunks_and_reuses_unchanged_blobs(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    first_client = FakeGitHub()
    first = build_index(config, client=first_client)
    assert first.indexed_files == 1
    assert first_client.blob_calls == 1

    database = config.store_path / "index.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 1
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    assert metadata["indexed_commit"] == COMMIT_SHA
    assert metadata["index_status"] == "ready"

    second_client = FakeGitHub()
    second = build_index(config, client=second_client)
    assert second.reused_files == 1
    assert second.indexed_files == 0
    assert second_client.blob_calls == 0


def test_secret_file_content_is_not_persisted(tmp_path: Path):
    config = load_config(write_config(tmp_path))
    secret = "token = 'abcdefghijklmnopqrstuvwxyz123456'\n"
    stats = build_index(config, client=FakeGitHub(secret))
    assert stats.indexed_files == 0
    with sqlite3.connect(config.store_path / "index.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
