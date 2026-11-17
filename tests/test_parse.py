"""Query translation and Gmail JSON parsing."""

from __future__ import annotations

import base64
import tempfile
import time
import unittest

from gmail_tui.gmail import NullGmail
from gmail_tui.http_gmail import parse_batch_response, parse_history_page, parse_message
from gmail_tui.search import parse_query
from gmail_tui.sync import Engine
from gmail_tui.textutil import header_fields, parse_rfc822


class SearchParseTests(unittest.TestCase):
    def test_operators_become_fts_and_filters(self) -> None:
        parsed = parse_query('is:unread from:ada subject:"weekly notes" platypus')
        self.assertTrue(parsed.unread)
        self.assertIn('from_addr : "ada"', parsed.fts or "")
        self.assertIn('subject : "weekly notes"', parsed.fts or "")
        self.assertIn('"platypus"', parsed.fts or "")
        self.assertIsNone(parsed.label)

    def test_label_alias(self) -> None:
        parsed = parse_query("in:inbox is:starred")
        self.assertEqual(parsed.label, "INBOX")
        self.assertTrue(parsed.starred)
        self.assertIsNone(parsed.fts)


class HttpParseTests(unittest.TestCase):
    def test_history_page(self) -> None:
        page = parse_history_page(
            {
                "historyId": 150,
                "history": [
                    {
                        "id": "149",
                        "messagesAdded": [
                            {"message": {"id": "abc", "threadId": "thr", "labelIds": ["INBOX"]}}
                        ],
                        "labelsRemoved": [
                            {
                                "message": {"id": "abc", "threadId": "thr"},
                                "labelIds": ["UNREAD"],
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(page.history_id, "150")
        self.assertEqual(page.records[0].messages_added[0].id, "abc")
        self.assertEqual(page.records[0].labels_removed[0].label_ids, ["UNREAD"])

    def test_raw_message_decodes_headers(self) -> None:
        import base64

        raw = b"Subject: Hello\r\nFrom: Ada <ada@example.com>\r\n\r\nbody text\r\n"
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        message = parse_message(
            {
                "id": "abc",
                "threadId": "thr",
                "labelIds": ["INBOX"],
                "internalDate": "2000",
                "snippet": "body text",
                "raw": encoded,
            }
        )
        self.assertEqual(message.subject, "Hello")
        self.assertEqual(message.from_addr, "Ada <ada@example.com>")
        self.assertEqual(message.raw, raw)
        self.assertEqual(message.label_ids, ["INBOX"])
        self.assertFalse(message.has_attachment)

    def test_header_fields_keep_order_and_decode_each_value(self) -> None:
        raw = (
            b"Received: from a.example\r\n\tby b.example\r\n"
            b"Received: from c.example\r\n"
            b"Subject: =?utf-8?q?Caf=C3=A9?=\r\n"
            b"X-Odd: caf\xe9\r\n"
            b"\r\n"
            b"body\r\n"
        )
        fields = header_fields(raw)
        self.assertEqual(
            [name for name, _value in fields], ["Received", "Received", "Subject", "X-Odd"]
        )
        self.assertEqual(fields[0][1], "from a.example by b.example")
        self.assertEqual(fields[2][1], "Café")
        self.assertNotIn("body", " ".join(value for _name, value in fields))
        # Undecodable 8-bit bytes become a replacement mark, not a lone surrogate.
        fields[3][1].encode("utf-8")

    def test_a_raw_message_is_parsed_once_and_kept(self) -> None:
        raw = b"Subject: Hello\r\nFrom: Ada <ada@example.com>\r\n\r\nplatypus lives\r\n"
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        message = parse_message(
            {
                "id": "m1",
                "threadId": "t1",
                "labelIds": ["INBOX"],
                "raw": encoded,
            }
        )
        self.assertIn("platypus lives", message.body_text or "")
        calls = {"n": 0}
        import gmail_tui.sync as sync_mod

        original = sync_mod.parse_rfc822

        def counting(data: bytes):
            calls["n"] += 1
            return original(data)

        sync_mod.parse_rfc822 = counting
        try:
            with tempfile.TemporaryDirectory() as tmp:
                engine = Engine.open(tmp, NullGmail())
                try:
                    engine._save_body(message)
                    self.assertEqual(calls["n"], 0)
                    preview = engine.store.preview_message("m1")
                    self.assertIsNotNone(preview)
                    assert preview is not None
                    self.assertIn("platypus lives", preview.body_text)
                finally:
                    engine.store.close()
        finally:
            sync_mod.parse_rfc822 = original

    def test_attachment_size_is_the_decoded_length(self) -> None:
        payload = base64.b64encode(b"hello world").decode()
        raw = (
            "Subject: File\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=mix\r\n"
            "\r\n"
            "--mix\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "hi\r\n"
            "--mix\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "Content-Disposition: attachment; filename=\"hi.bin\"\r\n"
            "\r\n"
            f"{payload}\r\n"
            "--mix--\r\n"
        ).encode()
        parsed = parse_rfc822(raw)
        self.assertEqual(parsed.attachments[0].filename, "hi.bin")
        self.assertEqual(parsed.attachments[0].size, len(b"hello world"))

    def test_a_large_attachment_is_measured_without_reading_it(self) -> None:
        payload = b"01234567" * (512 * 1024)
        logo = b"logo-bytes"
        raw = (
            "Subject: Big\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=mix\r\n"
            "\r\n"
            "--mix\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "before the file\r\n"
            "--mix\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "Content-Disposition: attachment; filename=\"big.bin\"\r\n"
            "\r\n"
            + base64.encodebytes(payload).decode("ascii")
            + "--mix\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "after the file\r\n"
            "--mix\r\n"
            "Content-Type: image/jpeg\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "Content-Disposition: inline; filename=\"logo.jpg\"\r\n"
            "\r\n"
            + base64.b64encode(logo).decode("ascii")
            + "\r\n"
            "--mix--\r\n"
        ).encode()
        started = time.perf_counter()
        parsed = parse_rfc822(raw)
        elapsed = time.perf_counter() - started
        self.assertIn("before the file", parsed.text)
        self.assertIn("after the file", parsed.text)
        self.assertEqual(
            [(item.filename, item.size) for item in parsed.attachments],
            [("big.bin", len(payload)), ("logo.jpg", len(logo))],
        )
        self.assertLess(elapsed, 0.03)

    def test_payload_attachment_flag(self) -> None:
        message = parse_message(
            {
                "id": "abc",
                "threadId": "thr",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {"mimeType": "text/plain", "filename": ""},
                        {
                            "mimeType": "application/pdf",
                            "filename": "note.pdf",
                            "body": {"attachmentId": "A1", "size": 10},
                        },
                    ],
                },
            }
        )
        self.assertTrue(message.has_attachment)

    def test_batch_response_keeps_each_status(self) -> None:
        payload = (
            b"--batch_1\r\n"
            b"Content-Type: application/http\r\n"
            b"\r\n"
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b'{"id":"a","threadId":"t","labelIds":["INBOX"]}\r\n'
            b"--batch_1\r\n"
            b"Content-Type: application/http\r\n"
            b"\r\n"
            b"HTTP/1.1 404 Not Found\r\n"
            b"Content-Type: application/json\r\n"
            b"\r\n"
            b'{"error":{"code":404}}\r\n'
            b"--batch_1--\r\n"
        )
        parts = parse_batch_response("multipart/mixed; boundary=batch_1", payload)
        self.assertEqual(parts[0][0], 200)
        self.assertEqual(parts[0][1]["id"], "a")
        self.assertEqual(parts[1][0], 404)


if __name__ == "__main__":
    unittest.main()
