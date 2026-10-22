"""Build a small, local SQLite retrieval index from one GitHub repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .github_client import GitHubClient, GitHubClientError


DATABASE_NAME = "index.sqlite3"
EMBEDDING_MODEL = "feature-hashing-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "config.yaml"

ALLOWED_EXTENSIONS = {
    ".bat",
    ".c",
    ".cfg",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".gql",
    ".go",
    ".gradle",
    ".graphql",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".less",
    ".md",
    ".mdx",
    ".php",
    ".properties",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".sass",
    ".scss",
    ".sh",
    ".sql",
    ".svelte",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
ALLOWED_FILENAMES = {
    ".dockerignore",
    ".gitignore",
    "dockerfile",
    "gemfile",
    "makefile",
    "procfile",
}
EXCLUDED_DIRECTORIES = {
    ".git",
    ".gradle",
    ".idea",
    ".next",
    ".nuxt",
    ".pytest_cache",
    ".terraform",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "htmlcov",
    "node_modules",
    "out",
    "target",
    "vendor",
    "venv",
}
SENSITIVE_SUFFIXES = {
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pfx",
    ".pem",
    ".pkcs12",
}
SENSITIVE_NAMES = {
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(
        r"(?im)^\s*(?:api[_-]?key|client[_-]?secret|password|secret|token)\s*[:=]\s*"
        r"['\"]?(?!\$\{|process\.env|os\.environ|env\(|<|CONFIGURAR|CHANGE_ME|EXAMPLE)"
        r"[A-Za-z0-9+/=_-]{16,}['\"]?\s*$"
    ),
)
TOKEN_PATTERN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9_]{2,}", re.UNICODE)


class ConfigurationError(ValueError):
    """The local agent configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class AgentConfig:
    config_path: Path
    project_name: str
    project_local_path: Path | None
    github_owner: str
    github_repo: str
    github_ref: str | None
    github_timeout: float
    store_path: Path
    chunk_size: int
    chunk_overlap: int
    top_k: int
    min_score: float
    max_file_size_bytes: int
    embedding_dimensions: int
    embedding_model: str

    @property
    def repository_slug(self) -> str:
        return f"{self.github_owner}/{self.github_repo}"


@dataclass(frozen=True)
class TextChunk:
    content: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class IndexStats:
    repository: str
    commit_sha: str
    indexed_files: int
    reused_files: int
    skipped_files: int
    chunks: int


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"A seção '{name}' deve ser um objeto YAML.")
    return value


def _require_int(value: Any, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError(f"'{name}' deve ser um inteiro >= {minimum}.")
    return value


def _safe_local_path(base_dir: Path, raw: Any, name: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigurationError(f"'{name}' deve ser um caminho relativo não vazio.")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ConfigurationError(f"'{name}' deve permanecer dentro do repositório.")
    resolved = (base_dir / candidate).resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise ConfigurationError(f"'{name}' não pode sair do repositório.") from exc
    return resolved


def _repository_root_for_config(config_path: Path) -> Path:
    for candidate in config_path.parents:
        if (candidate / "AGENTS.md").is_file():
            return candidate.resolve()
    return config_path.parent.resolve()


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> AgentConfig:
    path = Path(config_path).resolve()
    if not path.is_file():
        raise ConfigurationError(f"Arquivo de configuração não encontrado: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Não foi possível ler config.yaml: {exc}") from exc

    root = _require_mapping(raw, "raiz")
    project = _require_mapping(root.get("project"), "project")
    github = _require_mapping(root.get("github"), "github")
    rag = _require_mapping(root.get("rag"), "rag")

    project_name = project.get("name")
    if not isinstance(project_name, str) or not project_name.strip():
        raise ConfigurationError("'project.name' é obrigatório.")
    repository_root = _repository_root_for_config(path)
    local_path = project.get("local_path")
    if local_path is not None and (
        not isinstance(local_path, str) or not local_path.strip()
    ):
        raise ConfigurationError("'project.local_path' deve ser null ou um caminho não vazio.")
    project_local_path = None
    if isinstance(local_path, str):
        local_candidate = Path(local_path).expanduser()
        if not local_candidate.is_absolute():
            local_candidate = repository_root / local_candidate
        project_local_path = local_candidate.resolve()

    owner = github.get("owner")
    repo = github.get("repo")
    placeholders = {"CONFIGURAR_ORGANIZACAO", "CONFIGURAR_REPOSITORIO"}
    if not isinstance(owner, str) or not owner or owner in placeholders:
        raise ConfigurationError("Configure 'github.owner' antes de acessar a API.")
    if not isinstance(repo, str) or not repo or repo in placeholders:
        raise ConfigurationError("Configure 'github.repo' antes de acessar a API.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner):
        raise ConfigurationError("'github.owner' contém caracteres inválidos.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ConfigurationError("'github.repo' contém caracteres inválidos.")

    ref = github.get("ref")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise ConfigurationError("'github.ref' deve ser null ou uma referência não vazia.")
    timeout = github.get("timeout_seconds", 20)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ConfigurationError("'github.timeout_seconds' deve ser positivo.")

    if rag.get("enabled") is not True:
        raise ConfigurationError("O RAG precisa estar habilitado para indexação.")
    if rag.get("provider") != "local":
        raise ConfigurationError("Somente o provedor local é aceito nesta implementação.")
    model = rag.get("model", EMBEDDING_MODEL)
    if model != EMBEDDING_MODEL:
        raise ConfigurationError(f"Modelo local suportado: {EMBEDDING_MODEL}.")

    dimensions = _require_int(
        rag.get("embedding_dimensions", 384), "rag.embedding_dimensions", minimum=32
    )
    chunk_size = _require_int(rag.get("chunk_size"), "rag.chunk_size", minimum=100)
    chunk_overlap = _require_int(
        rag.get("chunk_overlap"), "rag.chunk_overlap", minimum=0
    )
    if chunk_overlap >= chunk_size:
        raise ConfigurationError("'rag.chunk_overlap' deve ser menor que chunk_size.")
    top_k = _require_int(rag.get("top_k"), "rag.top_k", minimum=1)
    max_file_kb = _require_int(
        rag.get("max_file_size_kb"), "rag.max_file_size_kb", minimum=1
    )
    min_score = rag.get("min_score", 0.05)
    if (
        not isinstance(min_score, (int, float))
        or isinstance(min_score, bool)
        or not 0 <= float(min_score) <= 1
    ):
        raise ConfigurationError("'rag.min_score' deve estar entre 0 e 1.")

    store_path = _safe_local_path(
        repository_root, rag.get("store_path"), "rag.store_path"
    )
    return AgentConfig(
        config_path=path,
        project_name=project_name.strip(),
        project_local_path=project_local_path,
        github_owner=owner,
        github_repo=repo,
        github_ref=ref.strip() if isinstance(ref, str) else None,
        github_timeout=float(timeout),
        store_path=store_path,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        top_k=top_k,
        min_score=float(min_score),
        max_file_size_bytes=max_file_kb * 1024,
        embedding_dimensions=dimensions,
        embedding_model=model,
    )


def is_path_eligible(path: str, *, size: int, max_bytes: int) -> bool:
    if not path or path.startswith("/") or "\\" in path or size < 0 or size > max_bytes:
        return False
    pure_path = PurePosixPath(path)
    if ".." in pure_path.parts:
        return False
    lowered_parts = [part.lower() for part in pure_path.parts]
    if any(part in EXCLUDED_DIRECTORIES for part in lowered_parts[:-1]):
        return False
    name = lowered_parts[-1]
    if name == ".env" or name.startswith(".env."):
        return False
    if name in SENSITIVE_NAMES or pure_path.suffix.lower() in SENSITIVE_SUFFIXES:
        return False
    return name in ALLOWED_FILENAMES or pure_path.suffix.lower() in ALLOWED_EXTENSIONS


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[TextChunk]:
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Parâmetros de chunking inválidos.")
    if not text:
        return []

    chunks: list[TextChunk] = []
    start = 0
    text_length = len(text)
    while start < text_length:
        hard_end = min(start + chunk_size, text_length)
        end = hard_end
        if hard_end < text_length:
            newline = text.rfind("\n", start + max(1, chunk_size // 2), hard_end)
            if newline >= 0:
                end = newline + 1
        content = text[start:end].rstrip()
        if content:
            start_line = text.count("\n", 0, start) + 1
            final_position = max(start, end - 1)
            end_line = text.count("\n", 0, final_position) + 1
            chunks.append(
                TextChunk(content=content, start_line=start_line, end_line=end_line)
            )
        if end >= text_length:
            break
        next_start = end - overlap
        start = next_start if next_start > start else start + 1
    return chunks


def _embedding_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in TOKEN_PATTERN.findall(text.lower()):
        tokens.append(raw)
        if "_" in raw:
            tokens.extend(part for part in raw.split("_") if len(part) >= 2)
    return tokens


def embed_text(text: str, *, dimensions: int) -> list[float]:
    """Create a deterministic, local feature-hashing embedding."""

    if dimensions < 1:
        raise ValueError("A dimensão do embedding deve ser positiva.")
    counts = Counter(_embedding_tokens(text))
    vector = [0.0] * dimensions
    for token, count in counts.items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        sign = 1.0 if digest[8] & 1 else -1.0
        vector[index] += sign * (1.0 + math.log(count))
    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def open_database(store_path: Path) -> sqlite3.Connection:
    store_path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(store_path / DATABASE_NAME)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            blob_sha TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            size INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
            blob_sha TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            vector TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
        """
    )
    return connection


def _set_metadata(connection: sqlite3.Connection, values: Mapping[str, str]) -> None:
    connection.executemany(
        "INSERT INTO metadata(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        values.items(),
    )


def mark_index_stale(config: AgentConfig, reason: str) -> None:
    connection = open_database(config.store_path)
    try:
        with connection:
            _set_metadata(
                connection,
                {
                    "index_status": "stale",
                    "last_sync_error": reason[:500],
                    "last_sync_attempt": datetime.now(timezone.utc).isoformat(),
                },
            )
    finally:
        connection.close()


def _valid_tree_entries(
    entries: Iterable[Mapping[str, Any]], max_bytes: int
) -> tuple[dict[str, dict[str, Any]], int]:
    accepted: dict[str, dict[str, Any]] = {}
    skipped = 0
    for entry in entries:
        path, sha, size = entry.get("path"), entry.get("sha"), entry.get("size")
        if (
            not isinstance(path, str)
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha)
            or not isinstance(size, int)
            or not is_path_eligible(path, size=size, max_bytes=max_bytes)
        ):
            skipped += 1
            continue
        accepted[path] = {"path": path, "sha": sha.lower(), "size": size}
    return accepted, skipped


def build_index(
    config: AgentConfig,
    *,
    token: str | None = None,
    client: GitHubClient | None = None,
) -> IndexStats:
    try:
        github = client or GitHubClient(
            config.github_owner,
            config.github_repo,
            token or "",
            timeout=config.github_timeout,
        )
        github.assert_authorized_repository(config.github_owner, config.github_repo)
    except Exception as exc:
        mark_index_stale(config, str(exc))
        raise

    connection = open_database(config.store_path)
    try:
        old_files = {
            row["path"]: row["blob_sha"]
            for row in connection.execute("SELECT path, blob_sha FROM files")
        }
    finally:
        connection.close()

    try:
        metadata = github.repository_metadata()
        configured_slug = config.repository_slug.lower()
        remote_slug = metadata.get("full_name")
        if isinstance(remote_slug, str) and remote_slug.lower() != configured_slug:
            raise GitHubClientError(
                "Os metadados recebidos não correspondem ao repositório autorizado."
            )
        ref = config.github_ref or metadata.get("default_branch")
        if not isinstance(ref, str) or not ref:
            raise GitHubClientError("Não foi possível determinar a referência Git.")
        commit_sha = github.resolve_ref(ref)
        entries, initially_skipped = _valid_tree_entries(
            github.list_tree(commit_sha), config.max_file_size_bytes
        )

        reusable = {
            path for path, entry in entries.items() if old_files.get(path) == entry["sha"]
        }
        changed = [entry for path, entry in entries.items() if path not in reusable]
        indexed: dict[str, tuple[dict[str, Any], list[TextChunk]]] = {}
        skipped = initially_skipped
        for entry in changed:
            content = github.get_text_blob(
                entry["sha"], max_bytes=config.max_file_size_bytes
            )
            if content is None or contains_secret(content):
                skipped += 1
                continue
            chunks = chunk_text(
                content,
                chunk_size=config.chunk_size,
                overlap=config.chunk_overlap,
            )
            if not chunks:
                skipped += 1
                continue
            indexed[entry["path"]] = (entry, chunks)
    except Exception as exc:
        mark_index_stale(config, str(exc))
        raise

    included_paths = reusable | set(indexed)
    connection = open_database(config.store_path)
    try:
        with connection:
            for path in set(old_files) - included_paths:
                connection.execute("DELETE FROM files WHERE path = ?", (path,))

            for path in reusable:
                entry = entries[path]
                connection.execute(
                    "UPDATE files SET commit_sha = ?, size = ? WHERE path = ?",
                    (commit_sha, entry["size"], path),
                )
                connection.execute(
                    "UPDATE chunks SET commit_sha = ? WHERE path = ?",
                    (commit_sha, path),
                )

            for path, (entry, chunks) in indexed.items():
                connection.execute("DELETE FROM files WHERE path = ?", (path,))
                connection.execute(
                    "INSERT INTO files(path, blob_sha, commit_sha, size) VALUES (?, ?, ?, ?)",
                    (path, entry["sha"], commit_sha, entry["size"]),
                )
                rows = []
                for chunk in chunks:
                    vector = embed_text(
                        f"{path}\n{chunk.content}",
                        dimensions=config.embedding_dimensions,
                    )
                    rows.append(
                        (
                            path,
                            entry["sha"],
                            commit_sha,
                            chunk.start_line,
                            chunk.end_line,
                            chunk.content,
                            hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                            json.dumps(vector, separators=(",", ":")),
                        )
                    )
                connection.executemany(
                    "INSERT INTO chunks(path, blob_sha, commit_sha, start_line, end_line, "
                    "content, content_hash, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )

            now = datetime.now(timezone.utc).isoformat()
            _set_metadata(
                connection,
                {
                    "repository": config.repository_slug,
                    "source_ref": ref,
                    "indexed_commit": commit_sha,
                    "indexed_at": now,
                    "last_sync_attempt": now,
                    "last_sync_error": "",
                    "index_status": "ready",
                    "embedding_model": config.embedding_model,
                    "embedding_dimensions": str(config.embedding_dimensions),
                },
            )
            total_chunks = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        connection.close()

    return IndexStats(
        repository=config.repository_slug,
        commit_sha=commit_sha,
        indexed_files=len(indexed),
        reused_files=len(reusable),
        skipped_files=skipped,
        chunks=total_chunks,
    )


def index_repository(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    environ: Mapping[str, str] | None = None,
    client: GitHubClient | None = None,
) -> IndexStats:
    config = load_config(config_path)
    environment = os.environ if environ is None else environ
    token = None if client is not None else environment.get("GITHUB_TOKEN")
    return build_index(config, token=token, client=client)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Indexa o repositório Glic autorizado.")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Caminho do config.yaml (padrão: a_core/config/config.yaml)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        stats = index_repository(args.config)
    except (ConfigurationError, GitHubClientError, OSError, sqlite3.Error) as exc:
        print(f"Erro de indexação: {exc}", file=sys.stderr)
        return 1
    print(
        f"Índice atualizado: {stats.repository} @ {stats.commit_sha}\n"
        f"Arquivos novos/alterados: {stats.indexed_files}; "
        f"reutilizados: {stats.reused_files}; ignorados: {stats.skipped_files}; "
        f"chunks: {stats.chunks}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
