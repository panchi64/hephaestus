from collections.abc import Mapping
from typing import Self

class AsyncOAuth2Client:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        scope: str | None = None,
        redirect_uri: str | None = None,
    ) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *args: object) -> None: ...
    async def fetch_token(
        self, url: str | None = None, *, grant_type: str | None = None, **kwargs: str
    ) -> Mapping[str, object]: ...
    def create_authorization_url(
        self, url: str, state: str | None = None, **kwargs: str
    ) -> tuple[str, str]: ...
