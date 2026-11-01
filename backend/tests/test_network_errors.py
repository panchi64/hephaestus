from __future__ import annotations

import pytest

from services.sandbox.network_errors import mentions_no_network


@pytest.mark.parametrize(
    "text",
    [
        "FileNotFoundError: [Errno 2] No such file or directory: 'x'",
        "ModuleNotFoundError: No module named 'requests'",
        "importlib.metadata.PackageNotFoundError: foo",
        "PageNotFound: /missing",
    ],
)
def test_a_missing_file_or_module_is_not_a_missing_network(text: str) -> None:
    assert not mentions_no_network(text)


@pytest.mark.parametrize(
    "text",
    [
        "Error: getaddrinfo ENOTFOUND registry.npmjs.org",
        "curl: (6) Could not resolve host: example.com",
    ],
)
def test_a_dns_failure_is_a_missing_network(text: str) -> None:
    assert mentions_no_network(text)
