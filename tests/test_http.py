"""Gmail requests keep the TCP connection instead of opening one per message."""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gmail_tui.gmail import Offline
from gmail_tui.http_gmail import HttpGmail


class _KeepAlive(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.ports.append(self.client_address[1])
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.server.ports.append(self.client_address[1])
        length = int(self.headers.get("Content-Length") or 0)
        self.server.posts.append((self.path, self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ConnectionReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAlive)
        self.server.ports = []
        self.server.posts = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.client = HttpGmail("token", base=f"http://{host}:{port}", timeout=5)

    def tearDown(self) -> None:
        for conn in self.client._connections.values():
            conn.close()
        self.server.shutdown()
        self.server.server_close()

    def test_two_messages_share_one_connection(self) -> None:
        self.client._json("GET", "/gmail/v1/users/me/profile")
        self.client._json("GET", "/gmail/v1/users/me/profile")
        self.assertEqual(self.server.ports[0], self.server.ports[1])
        self.assertEqual(len(self.client._connections), 1)

    def test_batch_modify_sends_every_id_in_one_request(self) -> None:
        self.client.batch_modify(["m1", "m2", "m3"], ["TRASH"], ["INBOX"])
        self.assertEqual(len(self.server.posts), 1)
        path, body = self.server.posts[0]
        self.assertEqual(path, "/gmail/v1/users/me/messages/batchModify")
        self.assertEqual(
            json.loads(body),
            {"ids": ["m1", "m2", "m3"], "addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX"]},
        )

    def test_a_batch_and_a_read_share_one_connection(self) -> None:
        # One delete of many rows is one TCP round trip, and the next poll
        # reuses the same socket.
        self.client.batch_modify(["m1", "m2"], ["TRASH"], ["INBOX"])
        self.client._json("GET", "/gmail/v1/users/me/profile")
        self.assertEqual(len(self.server.ports), 2)
        self.assertEqual(self.server.ports[0], self.server.ports[1])

    def test_a_dead_connection_is_opened_again(self) -> None:
        self.client._json("GET", "/gmail/v1/users/me/profile")
        for conn in self.client._connections.values():
            assert conn.sock is not None
            conn.sock.close()
        self.client._json("GET", "/gmail/v1/users/me/profile")
        self.assertEqual(len(self.server.ports), 2)
        self.assertNotEqual(self.server.ports[0], self.server.ports[1])

    def test_a_refused_port_is_offline(self) -> None:
        client = HttpGmail("token", base="http://127.0.0.1:1", timeout=2)
        with self.assertRaises(Offline):
            client._json("GET", "/gmail/v1/users/me/profile")

    def test_reauthorize_replaces_the_bearer_token(self) -> None:
        tokens = {"value": "next-token"}

        def refresh(*, force: bool = False) -> str | None:
            return tokens["value"]

        client = HttpGmail("old-token", refresh=refresh)
        self.assertTrue(client.reauthorize(force=True))
        self.assertEqual(client.token, "next-token")


class _DropSend(BaseHTTPRequestHandler):
    """Accept one send, then drop the socket before a response."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.posts += 1
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _Stall(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.started.set()
        self.server.release.wait(5)
        body = b"{}"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class AbortTests(unittest.TestCase):
    def test_a_dropped_send_is_not_posted_again(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _DropSend)
        server.posts = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        client = HttpGmail("token", base=f"http://{host}:{port}", timeout=2)
        try:
            client._json("GET", "/gmail/v1/users/me/profile")
            with self.assertRaises(Offline):
                client.send_message(b"From: a@b.c\r\n\r\nHi\r\n", "")
            self.assertEqual(server.posts, 1)
        finally:
            client.close()
            server.shutdown()
            server.server_close()

    def test_abort_refuses_the_next_request(self) -> None:
        client = HttpGmail("token", base="http://127.0.0.1:1", timeout=5)
        client.abort()
        started = time.monotonic()
        with self.assertRaises(Offline) as caught:
            client.get_profile()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("stopped", str(caught.exception))

    def test_abort_unblocks_a_hung_request(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Stall)
        server.started = threading.Event()
        server.release = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        client = HttpGmail("token", base=f"http://{host}:{port}", timeout=30)
        holder: dict[str, BaseException] = {}

        def call() -> None:
            try:
                client.get_profile()
            except BaseException as exc:
                holder["exc"] = exc

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        try:
            self.assertTrue(server.started.wait(2))
            started = time.monotonic()
            client.abort()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - started, 1)
            self.assertIsInstance(holder.get("exc"), Offline)
        finally:
            server.release.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
