"""The shared plumbing behind the two REST mail adapters (Gmail, Microsoft Graph).

Both are the same shape — a fixed vendor API root, a bearer access token from
:mod:`services.mail.oauth`, JSON in and JSON out — and differ only in paths and payload
schemas. That common half lives here so a status-code-to-domain-error rule or a timeout
change lands once for both, and each adapter is left holding only its vendor's shapes.

Unlike JMAP, the base URL is **ours**, not the operator's, so there is no SSRF surface to
guard: nothing here ever fetches an operator-supplied address.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from services.shapes import as_dict, parse_json

from .errors import MailAuthError, MailError, MailUnavailableError
from .models import AccountSpec

_TIMEOUT_S = 30.0


class RestApi:
    """A thin authenticated JSON client rooted at one vendor API."""

    def __init__(
        self, spec: AccountSpec, base_url: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._spec: AccountSpec = spec
        self._base_url: str = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = client
        self._owns_client: bool = client is None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object = None,
    ) -> dict[str, object]:
        token = self._spec.access_token
        if not token:
            raise MailAuthError("this account has no access token — reconnect it")
        client = self._http()
        try:
            response = await client.request(
                method,
                f"{self._base_url}/{path.lstrip('/')}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                json=json,
            )
        except httpx.HTTPError as exc:
            raise MailUnavailableError(f"could not reach the mail provider: {exc}") from exc
        return self._decode(response)

    def _decode(self, response: httpx.Response) -> dict[str, object]:
        if response.status_code in (401, 403):
            raise MailAuthError("the mail provider rejected this account's authorization")
        if response.status_code == 404:
            raise MailError("that message is no longer on the server")
        if response.status_code == 429:
            raise MailUnavailableError("the mail provider is rate-limiting this account")
        if response.status_code >= 400:
            raise MailError(f"the mail provider returned HTTP {response.status_code}")
        if not response.content:
            return {}
        try:
            document = as_dict(parse_json(response.content))
        except ValueError as exc:
            raise MailError("the mail provider returned a malformed response") from exc
        if document is None:
            raise MailError("the mail provider returned a malformed response")
        return document

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT_S)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None
