"""Out-of-band notification channels (`AE-3.2`, `TASK-6`): what leaves the machine, what
never does, and how a broken channel degrades (`XC-DEG-3`)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core.db import init_db, make_engine
from core.vault import Vault
from services.notification_channels import (
    EmailChannel,
    PushChannel,
    default_channels,
)
from services.notifications import NotificationService
from services.settings_store import SettingsStore

OWNER = "operator"


class _Account:
    def __init__(self, account_id="acct-1", address="operator@example.com", enabled=True):
        self.id = account_id
        self.address = address
        self.enabled = enabled


class _FakeMail:
    """Stands in for `MailService` — the channel only needs `list_accounts` and `send`."""

    def __init__(self, accounts=None, fail: Exception | None = None):
        self.accounts = accounts if accounts is not None else [_Account()]
        self.sent: list[tuple] = []
        self.fail = fail

    async def list_accounts(self, owner_id):
        return self.accounts

    async def send(self, owner_id, account_id, *, to, subject, body, **_kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append((owner_id, account_id, tuple(to), subject, body))
        return "sent-1"


async def _wired(tmp_path: Path, mail=None, client=None):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    settings = SettingsStore(engine)
    mail = _FakeMail() if mail is None else mail
    email = EmailChannel(lambda: mail, settings, vault)
    push = PushChannel(settings, vault, client=client)
    service = NotificationService(engine, vault, channels=[email, push])
    await service.start()
    return service, mail, email, push, settings


# --- what leaves the machine --------------------------------------------------


async def test_an_approval_request_reaches_the_operator_by_email(tmp_path):
    """An unattended run parks until answered, so the ask has to leave the app."""
    service, mail, _email, _push, _settings = await _wired(tmp_path)
    await service.notify(OWNER, "approval_needed", "run needs approval", "wants to send mail")
    await service._deliveries.join()
    await service.stop()

    [(owner, account_id, to, subject, body)] = mail.sent
    assert (owner, account_id, to) == (OWNER, "acct-1", ("operator@example.com",))
    assert subject == "run needs approval"
    assert body == "wants to send mail"


async def test_a_reminder_reaches_the_operator_by_email(tmp_path):
    service, mail, _email, _push, _settings = await _wired(tmp_path)
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()
    assert [s[3] for s in mail.sent] == ["Call the dentist"]


async def test_informational_notifications_stay_in_the_app(tmp_path):
    """Mailing every finished run would train the operator to ignore the channel."""
    service, mail, _email, _push, _settings = await _wired(tmp_path)
    for kind in ("run_completed", "run_failed", "task_outcome", "system"):
        await service.notify(OWNER, kind, f"{kind} happened")
    await service._deliveries.join()
    await service.stop()
    assert mail.sent == []


async def test_a_notification_is_recorded_in_app_even_with_no_channels(tmp_path):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    service = NotificationService(engine, vault)
    await service.start()
    view = await service.notify(OWNER, "reminder", "Call the dentist")
    await service.stop()
    rows, _unread = await service.list_notifications(OWNER)
    assert [v.id for v in rows] == [view.id]


# --- degradation --------------------------------------------------------------


async def test_an_unreachable_mail_server_does_not_break_the_notification(tmp_path):
    """The in-app record is authoritative; a failed reach is degraded, never lost."""
    mail = _FakeMail(fail=RuntimeError("smtp down"))
    service, _mail, _email, _push, _settings = await _wired(tmp_path, mail=mail)
    view = await service.notify(OWNER, "approval_needed", "needs approval")
    await service._deliveries.join()
    await service.stop()
    rows, _unread = await service.list_notifications(OWNER)
    assert [v.id for v in rows] == [view.id]


async def test_no_mail_account_degrades_to_in_app_only(tmp_path):
    mail = _FakeMail(accounts=[])
    service, _mail, email, _push, _settings = await _wired(tmp_path, mail=mail)
    health = await email.health(OWNER)
    await service.stop()
    assert (health.configured, health.status) == (False, "warn")
    assert "in-app only" in health.detail


async def test_a_disabled_account_is_never_sent_from(tmp_path):
    mail = _FakeMail(accounts=[_Account(enabled=False)])
    service, _mail, _email, _push, _settings = await _wired(tmp_path, mail=mail)
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()
    assert mail.sent == []


async def test_turning_the_email_channel_off_is_honored(tmp_path):
    service, mail, email, _push, _settings = await _wired(tmp_path)
    await email.configure(OWNER, enabled=False, address=None, account_id=None)
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()
    assert mail.sent == []


# --- addressing ---------------------------------------------------------------


async def test_a_connected_mailbox_needs_no_configuration(tmp_path):
    """One operator ⇒ their own mailbox is not a guess."""
    service, _mail, email, _push, _settings = await _wired(tmp_path)
    health = await email.health(OWNER)
    await service.stop()
    assert (health.configured, health.status) == (True, "nominal")
    assert "operator@example.com" in health.detail


async def test_an_explicit_address_overrides_the_account_address_and_is_sealed(tmp_path):
    service, mail, email, _push, settings = await _wired(tmp_path)
    await email.configure(OWNER, enabled=True, address="phone@example.net", account_id=None)
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()

    assert mail.sent[0][2] == ("phone@example.net",)
    stored = await settings.get(OWNER, "notifications.email.address_enc")
    assert stored is not None and "phone@example.net" not in stored


async def test_an_override_for_a_deleted_account_falls_back_rather_than_sending_nowhere(
    tmp_path,
):
    service, mail, email, _push, _settings = await _wired(tmp_path)
    await email.configure(OWNER, enabled=True, address=None, account_id="gone")
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()
    assert mail.sent[0][1] == "acct-1"


# --- push ---------------------------------------------------------------------


async def _push_client(sink: list[httpx.Request], status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sink.append(request)
        return httpx.Response(status)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_push_posts_the_notification_to_the_operators_endpoint(tmp_path):
    seen: list[httpx.Request] = []
    service, _mail, _email, push, _settings = await _wired(
        tmp_path, client=await _push_client(seen)
    )
    await push.configure(OWNER, enabled=True, endpoint="https://ntfy.example/hephaestus")
    await service.notify(OWNER, "approval_needed", "needs approval", "wants to send mail")
    await service._deliveries.join()
    await service.stop()

    [request] = seen
    assert str(request.url) == "https://ntfy.example/hephaestus"
    assert request.headers["Title"] == "needs approval"
    import json

    payload = json.loads(request.content)
    # Both key spellings, so a notifier works without a per-vendor adapter.
    assert payload["title"] == "needs approval"
    assert payload["message"] == payload["body"] == "wants to send mail"


async def test_push_is_silent_until_an_endpoint_is_configured(tmp_path):
    seen: list[httpx.Request] = []
    service, _mail, _email, push, _settings = await _wired(
        tmp_path, client=await _push_client(seen)
    )
    health = await push.health(OWNER)
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()
    assert seen == []
    assert (health.configured, health.status) == (False, "warn")


async def test_push_health_names_the_host_but_never_the_path(tmp_path):
    """A push endpoint's path is usually the only credential it has."""
    service, _mail, _email, push, _settings = await _wired(tmp_path)
    await push.configure(OWNER, enabled=True, endpoint="https://ntfy.example/secret-topic")
    health = await push.health(OWNER)
    await service.stop()
    assert health.detail == "to ntfy.example"
    assert "secret-topic" not in health.detail


async def test_the_push_endpoint_is_stored_sealed(tmp_path):
    service, _mail, _email, push, settings = await _wired(tmp_path)
    await push.configure(OWNER, enabled=True, endpoint="https://ntfy.example/secret-topic")
    await service.stop()
    stored = await settings.get(OWNER, "notifications.push.endpoint_enc")
    assert stored is not None and "secret-topic" not in stored


@pytest.mark.parametrize(
    "endpoint",
    ["ftp://host/topic", "https:///topic", "https://user:pw@ntfy.example/topic"],
)
async def test_a_malformed_push_endpoint_is_refused(tmp_path, endpoint):
    service, _mail, _email, push, _settings = await _wired(tmp_path)
    with pytest.raises(ValueError):
        await push.configure(OWNER, enabled=True, endpoint=endpoint)
    await service.stop()


async def test_a_lan_endpoint_is_allowed_because_a_self_hosted_notifier_is_the_point(
    tmp_path,
):
    seen: list[httpx.Request] = []
    service, _mail, _email, push, _settings = await _wired(
        tmp_path, client=await _push_client(seen)
    )
    await push.configure(OWNER, enabled=True, endpoint="http://192.168.1.10:8080/message")
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()
    assert len(seen) == 1


async def test_a_failing_push_endpoint_does_not_re_send_the_email(tmp_path):
    """`TASK-6` forbids duplicates — one job per channel, so a push retry re-runs push only."""
    seen: list[httpx.Request] = []
    service, mail, _email, push, _settings = await _wired(
        tmp_path, client=await _push_client(seen, status=500)
    )
    await push.configure(OWNER, enabled=True, endpoint="https://ntfy.example/hephaestus")
    await service.notify(OWNER, "reminder", "Call the dentist")
    await service._deliveries.join()
    await service.stop()

    assert len(seen) > 1  # push retried
    assert len(mail.sent) == 1  # the email did not


# --- health surface -----------------------------------------------------------


async def test_channel_health_reports_every_channel_in_order(tmp_path):
    service, _mail, _email, _push, _settings = await _wired(tmp_path)
    health = await service.channel_health(OWNER)
    await service.stop()
    assert [h.key for h in health] == ["email", "push"]


async def test_default_channels_are_email_then_push(tmp_path):
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    vault = Vault(tmp_path / "keyfile.json")
    await vault.setup("pw")
    channels = default_channels(lambda: None, SettingsStore(engine), vault)
    assert [c.key for c in channels] == ["email", "push"]
