"""The mail transport seam: a fake provider must satisfy the whole protocol, and every
adapter must too — so nothing above the seam can come to depend on an IMAP detail."""

from __future__ import annotations

import pytest

from services.mail.errors import MailError, MailUnsupportedError
from services.mail.gmail import GmailTransport
from services.mail.graph import GraphTransport
from services.mail.imap import ImapTransport
from services.mail.jmap import JmapTransport
from services.mail.models import (
    ROLE_INBOX,
    AccountSpec,
    MailAddress,
    OutgoingMail,
    TransportCapabilities,
)
from services.mail.transport import MailTransport
from tests.mail_fakes import FakeTransport

type _Adapter = type[ImapTransport | JmapTransport | GmailTransport | GraphTransport]


def test_fake_transport_satisfies_the_protocol():
    assert isinstance(FakeTransport(), MailTransport)


@pytest.mark.parametrize("adapter", [ImapTransport, JmapTransport, GmailTransport, GraphTransport])
def test_every_adapter_satisfies_the_protocol(adapter: _Adapter):
    spec = AccountSpec(
        account_id="a1",
        address="operator@example.com",
        provider="imap",
        auth_kind="password",
        config={"imap_host": "mail.example.com", "session_url": "https://jmap.example.com/"},
        password="hunter2",
    )
    assert isinstance(adapter(spec), MailTransport)


@pytest.mark.parametrize("adapter", [ImapTransport, JmapTransport, GmailTransport, GraphTransport])
def test_capabilities_are_declared_not_discovered(adapter: _Adapter):
    spec = AccountSpec(
        account_id="a1", address="op@example.com", provider="imap", auth_kind="password"
    )
    capabilities = adapter(spec).capabilities()
    assert isinstance(capabilities, TransportCapabilities)


async def test_listing_is_newest_first_and_incremental():
    transport = FakeTransport()
    headers = await transport.list_messages("INBOX")
    assert [h.uid for h in headers] == ["2", "1"]
    assert [h.uid for h in await transport.list_messages("INBOX", since_uid="1")] == ["2"]


async def test_folders_carry_normalized_roles():
    folders = {f.id: f.role for f in await FakeTransport().list_folders()}
    assert folders["INBOX"] == ROLE_INBOX


async def test_unknown_folder_raises_a_domain_error():
    with pytest.raises(MailError):
        _ = await FakeTransport().list_messages("Nope")


async def test_send_returns_a_message_id():
    transport = FakeTransport()
    message_id = await transport.send(
        OutgoingMail(to=(MailAddress(address="ada@example.org"),), subject="hi", body="hello")
    )
    assert message_id.startswith("<sent-")
    assert transport.sent[0].subject == "hi"


async def test_an_unsupported_operation_is_declared_and_refused():
    transport = FakeTransport()
    transport.supports_move = False
    assert transport.capabilities().move is False
    with pytest.raises(MailUnsupportedError):
        await transport.move("INBOX", "1", "Archive")
