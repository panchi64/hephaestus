"""Quoted-text and signature separation (`EMAIL-4`), plus the RFC 5322 parse layer."""

from __future__ import annotations

from typing import cast

from plugins.mail.services.models import MailAddress, OutgoingMail
from plugins.mail.services.parse import build_outgoing, html_to_text, parse_message, snippet_of
from plugins.mail.services.quoting import split_body

REPLY_WITH_QUOTE = """Yes, Tuesday works for me.

Best regards,
Ada
Analytical Engines Ltd.

On Wed, 12 Aug 2026 at 18:04, Charles <charles@example.org> wrote:
> Could we move the review to Tuesday?
> — Charles
"""


def test_a_reply_splits_into_prose_quote_and_signature():
    parts = split_body(REPLY_WITH_QUOTE)
    assert parts.reply == "Yes, Tuesday works for me."
    assert parts.quoted is not None and "Could we move the review" in parts.quoted
    assert parts.signature is not None and "Analytical Engines Ltd." in parts.signature


def test_the_rfc_3676_delimiter_wins_over_the_sign_off_heuristic():
    parts = split_body("The report is attached.\n\n-- \nAda\nEngine Division\n")
    assert parts.reply == "The report is attached."
    assert parts.signature == "Ada\nEngine Division"


def test_a_body_with_neither_quote_nor_signature_stays_whole():
    parts = split_body("Short and complete.")
    assert parts == split_body("Short and complete.")
    assert parts.reply == "Short and complete."
    assert parts.quoted is None
    assert parts.signature is None


def test_a_one_line_thanks_is_not_eaten_as_a_signature():
    # A greeting-shaped body is the whole message; hiding it would leave nothing.
    assert split_body("Thanks!").reply == "Thanks!"


def test_a_long_trailing_block_is_prose_not_a_signature():
    body = "Best\n" + "\n".join(f"paragraph line {n}" for n in range(20))
    parts = split_body(body)
    assert parts.signature is None
    assert parts.reply == body


def test_an_empty_body_is_handled():
    assert split_body("   ").reply == ""


def test_parse_message_decodes_encoded_words_and_addresses():
    raw = (
        b"From: =?utf-8?q?Ada_Lovelace?= <ada@example.org>\r\n"
        b"To: operator@example.com, second@example.com\r\n"
        b"Cc: cc@example.com\r\n"
        b"Subject: =?utf-8?q?Caf=C3=A9_notes?=\r\n"
        b"Date: Thu, 13 Aug 2026 09:00:00 +0000\r\n"
        b"\r\n"
        b"Plain body.\r\n"
    )
    body = parse_message(raw, uid="7")
    assert body.header.subject == "Café notes"
    assert body.header.sender == MailAddress(address="ada@example.org", name="Ada Lovelace")
    assert [a.address for a in body.header.to] == ["operator@example.com", "second@example.com"]
    assert [a.address for a in body.header.cc] == ["cc@example.com"]
    assert body.header.received_at is not None
    assert body.text.strip() == "Plain body."


def test_an_html_only_message_is_reduced_to_text():
    raw = (
        b"From: ada@example.org\r\n"
        b"Subject: HTML only\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><article><h1>Heading</h1>"
        b"<p>The engine weaves algebraic patterns just as the Jacquard loom "
        b"weaves flowers and leaves, which is the whole point.</p>"
        b"</article></body></html>\r\n"
    )
    body = parse_message(raw, uid="8")
    assert body.html is not None
    assert "algebraic patterns" in body.text
    assert "<p>" not in body.text


def test_html_to_text_never_leaks_markup_on_failure():
    assert html_to_text(None) == ""
    assert "<script>" not in html_to_text("<script>alert(1)</script>")


def test_snippet_is_collapsed_and_capped():
    assert snippet_of("a\n\n  b   c") == "a b c"
    assert len(snippet_of("x" * 500)) == 200


def test_build_outgoing_threads_a_reply():
    message = OutgoingMail(
        to=(MailAddress(address="ada@example.org", name="Ada"),),
        subject="Re: Engine",
        body="Agreed.",
        in_reply_to="<parent@example.org>",
        references=("<root@example.org>",),
    )
    composed = build_outgoing(MailAddress(address="operator@example.com"), message)
    assert composed["To"] == "Ada <ada@example.org>"
    assert composed["In-Reply-To"] == "<parent@example.org>"
    assert composed["References"] == "<root@example.org> <parent@example.org>"
    assert composed["Message-ID"]
    assert cast(str, composed.get_content()).strip() == "Agreed."
