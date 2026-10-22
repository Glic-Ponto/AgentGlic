"""Retrieve grounded source snippets from the local Glic RAG index."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .index import (
    DATABASE_NAME,
    DEFAULT_CONFIG_PATH,
    AgentConfig,
    ConfigurationError,
    embed_text,
    load_config,
)


class IndexUnavailableError(RuntimeError):
    """The local index is absent or incompatible with the active configuration."""


@dataclass(frozen=True)
class RetrievalResult:
    repository: str
    path: str
    start_line: int
    end_line: int
    commit_sha: str
    score: float
    content: str


@dataclass(frozen=True)
class RetrievalResponse:
    query: str
    results: tuple[RetrievalResult, ...]
    index_status: str
    warning: str | None = None

    @property
    def has_sufficient_information(self) -> bool:
        return bool(self.results)


def _dot(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise IndexUnavailableError("Dimensão dos vetores do índice é incompatível.")
    return sum(a * b for a, b in zip(left, right))


def _load_metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        return {
            row[0]: row[1]
            for row in connection.execute("SELECT key, value FROM metadata").fetchall()
        }
    except sqlite3.Error as exc:
        raise IndexUnavailableError("Banco local não contém metadados válidos.") from exc


def search(
    query: str,
    config: AgentConfig,
    *,
    top_k: int | None = None,
) -> RetrievalResponse:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("A consulta não pode estar vazia.")
    limit = config.top_k if top_k is None else top_k
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
        raise ValueError("top_k deve estar entre 1 e 100.")

    database = config.store_path / DATABASE_NAME
    if not database.is_file():
        raise IndexUnavailableError(
            "Índice local não encontrado. Execute `python -m a_core.rag.index` primeiro."
        )

    try:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise IndexUnavailableError(f"Não foi possível abrir o índice local: {exc}") from exc

    try:
        metadata = _load_metadata(connection)
        if metadata.get("repository", "").lower() != config.repository_slug.lower():
            raise IndexUnavailableError(
                "O índice pertence a outro repositório; execute uma nova indexação."
            )
        if metadata.get("embedding_model") != config.embedding_model:
            raise IndexUnavailableError(
                "O modelo do índice difere da configuração; reindexe o repositório."
            )
        try:
            dimensions = int(metadata["embedding_dimensions"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IndexUnavailableError("Dimensão do índice ausente ou inválida.") from exc
        if dimensions != config.embedding_dimensions:
            raise IndexUnavailableError(
                "A dimensão de embeddings mudou; reindexe o repositório."
            )

        query_vector = embed_text(clean_query, dimensions=dimensions)
        if not any(query_vector):
            return RetrievalResponse(
                query=clean_query,
                results=(),
                index_status=metadata.get("index_status", "unknown"),
                warning="A consulta não gerou termos pesquisáveis.",
            )

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in connection.execute(
            "SELECT path, start_line, end_line, commit_sha, content, vector FROM chunks"
        ):
            try:
                vector = json.loads(row["vector"])
                if not isinstance(vector, list):
                    raise ValueError
                numeric_vector = [float(value) for value in vector]
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise IndexUnavailableError("Vetor corrompido encontrado no índice.") from exc
            score = _dot(query_vector, numeric_vector)
            if math.isfinite(score) and score >= config.min_score:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["start_line"]))

        repository = metadata["repository"]
        results = tuple(
            RetrievalResult(
                repository=repository,
                path=row["path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                commit_sha=row["commit_sha"],
                score=score,
                content=row["content"],
            )
            for score, row in scored[:limit]
        )
        status = metadata.get("index_status", "unknown")
        warning = None
        if status != "ready":
            detail = metadata.get("last_sync_error", "falha de sincronização não detalhada")
            warning = f"O índice local pode estar desatualizado: {detail}"
        elif not results:
            warning = "Nenhuma informação suficientemente relevante foi encontrada no índice."
        return RetrievalResponse(
            query=clean_query,
            results=results,
            index_status=status,
            warning=warning,
        )
    finally:
        connection.close()


def retrieve(
    query: str,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    top_k: int | None = None,
) -> RetrievalResponse:
    return search(query, load_config(config_path), top_k=top_k)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recupera trechos fundamentados do índice local do Glic Project."
    )
    parser.add_argument("query", help="Pergunta ou termos a pesquisar")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Caminho do config.yaml (padrão: a_core/config/config.yaml)",
    )
    parser.add_argument("--top-k", type=int, default=None, help="Quantidade máxima de resultados")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        response = retrieve(args.query, args.config, top_k=args.top_k)
    except (ConfigurationError, IndexUnavailableError, ValueError, OSError) as exc:
        print(f"Erro de recuperação: {exc}", file=sys.stderr)
        return 1

    if response.warning:
        print(f"Aviso: {response.warning}", file=sys.stderr)
    if not response.results:
        print("Sem contexto fundamentado suficiente para responder à consulta.")
        return 0

    for position, result in enumerate(response.results, start=1):
        print(
            f"[{position}] score={result.score:.4f} "
            f"repo={result.repository} commit={result.commit_sha}\n"
            f"    arquivo={result.path}:{result.start_line}-{result.end_line}\n"
            f"{result.content}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
