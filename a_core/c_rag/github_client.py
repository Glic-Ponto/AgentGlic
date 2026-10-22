"""Small, repository-scoped client for GitHub's REST API."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping
from urllib.parse import quote

import requests


API_ROOT = "https://api.github.com"
_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_GIT_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")


class GitHubClientError(RuntimeError):
    """Base error for safe, user-facing GitHub failures."""


class GitHubAuthenticationError(GitHubClientError):
    """The supplied token is absent, invalid, or lacks read access."""


class GitHubRateLimitError(GitHubClientError):
    """GitHub refused a request because a rate limit was reached."""


class UnauthorizedRepositoryError(GitHubClientError):
    """A caller attempted to use a repository other than the configured one."""


@dataclass(frozen=True)
class RepositoryTarget:
    owner: str
    repo: str

    def __post_init__(self) -> None:
        if not _REPOSITORY_PART.fullmatch(self.owner or ""):
            raise ValueError("GitHub owner inválido.")
        if not _REPOSITORY_PART.fullmatch(self.repo or ""):
            raise ValueError("GitHub repo inválido.")

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class GitHubClient:
    """Read-only client locked to one configured GitHub repository.

    The API origin is a constant and callers never supply URLs. This prevents
    repository data from redirecting the client to an arbitrary host.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str,
        *,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        if not token or not token.strip():
            raise GitHubAuthenticationError("GITHUB_TOKEN não configurado.")
        if timeout <= 0:
            raise ValueError("O timeout do GitHub deve ser positivo.")

        self.target = RepositoryTarget(owner=owner, repo=repo)
        self.timeout = float(timeout)
        self._session = session or requests.Session()
        # Never log this mapping: it contains the credential.
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token.strip()}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "glic-engineering-agent/0.1",
        }

    def assert_authorized_repository(self, owner: str, repo: str) -> None:
        candidate = RepositoryTarget(owner=owner, repo=repo)
        if candidate != self.target:
            raise UnauthorizedRepositoryError(
                f"Acesso bloqueado a {candidate.slug}; repositório autorizado: "
                f"{self.target.slug}."
            )

    def _url(self, path: str) -> str:
        prefix = f"/repos/{self.target.owner}/{self.target.repo}/"
        if not path.startswith(prefix) or "://" in path or ".." in path:
            raise UnauthorizedRepositoryError("Caminho de API fora do repositório autorizado.")
        return f"{API_ROOT}{path}"

    def _request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> requests.Response:
        url = self._url(path)
        try:
            response = self._session.get(
                url,
                headers=self._headers,
                params=dict(params or {}),
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise GitHubClientError(f"Falha de rede ao acessar o GitHub: {exc}") from exc

        if response.status_code in {301, 302, 303, 307, 308}:
            raise GitHubClientError("Redirecionamento inesperado recusado pela integração GitHub.")
        if response.status_code == 401:
            raise GitHubAuthenticationError("GitHub recusou o token (HTTP 401).")
        if response.status_code in {403, 429}:
            remaining = response.headers.get("X-RateLimit-Remaining")
            if response.status_code == 429 or remaining == "0":
                reset = response.headers.get("X-RateLimit-Reset", "desconhecido")
                raise GitHubRateLimitError(
                    f"Limite da API do GitHub atingido; reset: {reset}."
                )
            raise GitHubAuthenticationError(
                "GitHub recusou o acesso; verifique as permissões Contents/Metadata read-only."
            )
        if response.status_code == 404:
            raise GitHubClientError(
                "Repositório ou objeto não encontrado; confirme configuração e permissões."
            )
        if response.status_code >= 400:
            raise GitHubClientError(f"GitHub retornou HTTP {response.status_code}.")
        return response

    def _json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self._request(path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubClientError("Resposta JSON inválida recebida do GitHub.") from exc

    def paginated_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[Any]:
        """Yield list entries while honoring GitHub's Link pagination header."""

        page = 1
        while True:
            page_params = {**dict(params or {}), "per_page": 100, "page": page}
            response = self._request(path, params=page_params)
            try:
                payload = response.json()
            except ValueError as exc:
                raise GitHubClientError("Resposta JSON inválida recebida do GitHub.") from exc
            if not isinstance(payload, list):
                raise GitHubClientError("Resposta paginada inesperada do GitHub.")
            yield from payload
            if "next" not in response.links:
                return
            page += 1

    def repository_metadata(self) -> dict[str, Any]:
        payload = self._json(f"/repos/{self.target.owner}/{self.target.repo}/")
        if not isinstance(payload, dict):
            raise GitHubClientError("Metadados de repositório inválidos.")
        return payload

    def default_branch(self) -> str:
        branch = self.repository_metadata().get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise GitHubClientError("Branch padrão ausente nos metadados do GitHub.")
        return branch

    def resolve_ref(self, ref: str) -> str:
        if not ref or len(ref) > 256 or any(ord(char) < 32 for char in ref):
            raise ValueError("Referência Git inválida.")
        encoded_ref = quote(ref, safe="")
        payload = self._json(
            f"/repos/{self.target.owner}/{self.target.repo}/commits/{encoded_ref}"
        )
        sha = payload.get("sha") if isinstance(payload, dict) else None
        if not isinstance(sha, str) or not _GIT_SHA.fullmatch(sha):
            raise GitHubClientError("SHA de commit inválido retornado pelo GitHub.")
        return sha.lower()

    def list_tree(self, commit_sha: str) -> list[dict[str, Any]]:
        self._validate_sha(commit_sha)
        commit = self._json(
            f"/repos/{self.target.owner}/{self.target.repo}/git/commits/{commit_sha}"
        )
        tree_sha = (
            commit.get("tree", {}).get("sha") if isinstance(commit, dict) else None
        )
        if not isinstance(tree_sha, str) or not _GIT_SHA.fullmatch(tree_sha):
            raise GitHubClientError("Árvore Git inválida retornada pelo GitHub.")
        payload = self._json(
            f"/repos/{self.target.owner}/{self.target.repo}/git/trees/{tree_sha}",
            params={"recursive": "1"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            raise GitHubClientError("Conteúdo de árvore inválido retornado pelo GitHub.")
        if payload.get("truncated"):
            raise GitHubClientError(
                "A árvore do repositório foi truncada pelo GitHub; a indexação foi interrompida."
            )
        return [entry for entry in payload["tree"] if entry.get("type") == "blob"]

    def get_text_blob(self, blob_sha: str, *, max_bytes: int) -> str | None:
        self._validate_sha(blob_sha)
        if max_bytes <= 0:
            raise ValueError("O limite de tamanho deve ser positivo.")
        payload = self._json(
            f"/repos/{self.target.owner}/{self.target.repo}/git/blobs/{blob_sha}"
        )
        if not isinstance(payload, dict):
            raise GitHubClientError("Blob inválido retornado pelo GitHub.")
        size = payload.get("size")
        if not isinstance(size, int) or size > max_bytes:
            return None
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise GitHubClientError("Codificação de blob não suportada.")
        try:
            encoded = "".join(payload["content"].split())
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise GitHubClientError("Conteúdo base64 inválido retornado pelo GitHub.") from exc
        if len(raw) > max_bytes or b"\x00" in raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @staticmethod
    def _validate_sha(value: str) -> None:
        if not isinstance(value, str) or not _GIT_SHA.fullmatch(value):
            raise ValueError("SHA Git inválido.")
