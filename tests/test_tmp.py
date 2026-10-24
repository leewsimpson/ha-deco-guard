"""TMP (Deco app protocol) client: framing against a fake Deco, and block/unblock requests."""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import threading
import zlib
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import paramiko
import pytest

from custom_components.deco_guard.api import DecoClient, DecoError
from custom_components.deco_guard.tmp import (
    OP_BLOCK,
    OP_CLIENT_LIST,
    OP_UNBLOCK,
    TmpAuthError,
    TmpClient,
    TmpSession,
)

SEED = 0x5A6B7C8D


def tmp_frame(payload: bytes, serial: int) -> bytes:
    """A TMP data frame as the public pydeco-tmp client builds it (from a captured session)."""
    base = struct.pack(">BBBBHBBI", 1, 1, 5, 0, len(payload), 0, 0, serial)
    crc = zlib.crc32(base + struct.pack(">I", SEED) + payload) & 0xFFFFFFFF
    return base + struct.pack(">I", crc) + payload


def appv2(op, flag, trans, body=b"", start=0, total=None, err=0) -> bytes:
    crc = zlib.crc32(body) & 0xFFFFFFFF if body else 0
    total = len(body) if total is None else total
    return struct.pack(">BBHBBHIII", 1, 2, op, flag, err, trans, crc, total, start) + body


def recvn(sock, n):
    buf = b""
    while len(buf) < n:
        buf += sock.recv(n - len(buf))
    return buf


def serve(sock, reply: bytes, mode: str, received: list, err: int = 0) -> None:
    """Fake Deco: answers each PUSH with `reply`, either directly or PUSH_ACK + 16-byte PULLs."""
    serial = 1
    while True:
        head = recvn(sock, 4)
        if head[2] == 4:  # the client's keep-alive echo
            received.append(("hello",))
            continue
        if head[2] != 5:
            return
        rest = recvn(sock, 12)
        payload = recvn(sock, struct.unpack(">H", rest[:2])[0])
        frame = head + rest + payload
        assert frame == tmp_frame(payload, struct.unpack(">I", rest[4:8])[0])
        _, _, op, flag, _, trans, _, total, start = struct.unpack_from(">BBHBBHIII", payload)
        assert payload == appv2(op, flag, trans, payload[20:], start, total)
        received.append((op, flag, start, payload[20:]))
        if flag == 2 and (err or mode == "direct"):
            sock.sendall(tmp_frame(appv2(op, 5, trans, reply, err=err), serial))
        elif flag == 2:
            sock.sendall(bytes([1, 1, 4, 0]))  # keep-alive mid-exchange
            sock.sendall(tmp_frame(appv2(op, 3, trans, start=len(payload) - 20, total=0), serial))
        elif flag == 4:
            chunk = reply[start : start + 16]
            sock.sendall(tmp_frame(appv2(op, 5, trans, chunk, start, len(reply)), serial))
        serial += 1


@contextmanager
def fake_deco(reply: bytes, mode: str = "direct", err: int = 0):
    client, server = socket.socketpair()
    client.settimeout(5)
    received: list = []
    thread = threading.Thread(target=serve, args=(server, reply, mode, received, err))
    thread.start()
    try:
        yield TmpSession(client), received
    finally:
        client.sendall(bytes([1, 1, 6, 0]))
        thread.join(5)
        client.close()
        server.close()


@pytest.mark.parametrize("mode", ["direct", "chunked"])
def test_request_round_trip(mode):
    reply = json.dumps({"error_code": 0, "result": {"client_list": ["X"] * 20}}).encode()
    with fake_deco(reply, mode) as (session, received):
        assert session.request(0x4018, b'{"config_version":1}') == reply
    pulls = [r for r in received if len(r) > 1 and r[1] == 4]
    assert received[0][:2] == (0x4018, 2)
    assert (len(pulls) > 1) if mode == "chunked" else not pulls
    assert (("hello",) in received) == (mode == "chunked")


def test_appv2_error_raises():
    with fake_deco(b"", err=12) as (session, _), pytest.raises(DecoError, match="appv2 error 12"):
        session.request(0x4403)


class FakeSession:
    """Stands in for an open TMP session: records requests, answers from `answers`."""

    def __init__(self, answers: dict[int, dict]) -> None:
        self.answers = answers
        self.sent: list[tuple[int, dict]] = []

    def request(self, opcode, body=b""):
        self.sent.append((opcode, json.loads(body)))
        return json.dumps(self.answers.get(opcode, {"error_code": 0})).encode()


def tmp_client(session: FakeSession) -> TmpClient:
    client = TmpClient("http://192.168.68.1", "me@example.com", "pw")

    @contextmanager
    def _session():
        yield session

    client._session = _session
    return client


def test_block_sends_name_and_type_like_the_app():
    session = FakeSession(
        {
            OP_CLIENT_LIST: {
                "error_code": 0,
                "result": {
                    "client_list": [
                        {"mac": "AA-AA-AA-AA-AA-01", "name": "TGFwdG9w", "client_type": "pc"}
                    ]
                },
            }
        }
    )
    tmp_client(session).block(["AA-AA-AA-AA-AA-01", "AA-AA-AA-AA-AA-02"])
    opcode, body = session.sent[-1]
    assert opcode == OP_BLOCK
    assert body["params"] == {
        "client_list": [
            {"mac": "AA-AA-AA-AA-AA-01", "name": "TGFwdG9w", "client_type": "pc"},
            {
                "mac": "AA-AA-AA-AA-AA-02",
                "name": "QUEtQUEtQUEtQUEtQUEtMDI=",
                "client_type": "other",
            },
        ]
    }


def test_unblock_sends_plain_macs():
    session = FakeSession({})
    tmp_client(session).unblock(["AA-AA-AA-AA-AA-01"])
    [(opcode, body)] = session.sent
    assert opcode == OP_UNBLOCK
    assert body["params"] == {"client_list": ["AA-AA-AA-AA-AA-01"]}


def test_router_error_code_raises():
    session = FakeSession({OP_UNBLOCK: {"error_code": -40101}})
    with pytest.raises(DecoError) as err:
        tmp_client(session).unblock(["AA-AA-AA-AA-AA-01"])
    assert err.value.error_code == -40101


def test_rejected_login_is_not_retried():
    """Repeated bad logins lock the TP-Link account, so one rejection stops further tries."""
    transport = MagicMock()
    transport.auth_password.side_effect = paramiko.AuthenticationException("bad")
    client = TmpClient("192.168.68.1", "me@example.com", "wrong")
    with (
        patch("socket.create_connection") as connect,
        patch("paramiko.Transport", return_value=transport),
    ):
        with pytest.raises(TmpAuthError):
            client.unblock(["AA-AA-AA-AA-AA-01"])
        with pytest.raises(TmpAuthError):
            client.unblock(["AA-AA-AA-AA-AA-01"])
    assert connect.call_count == 1
    transport.auth_password.assert_called_once_with(client._user, "wrong")


def test_ssh_username_is_sha1_of_email():
    client = TmpClient("http://192.168.68.1/", " me@example.com ", "pw")
    assert client.host == "192.168.68.1"
    assert client._user == hashlib.sha1(b"me@example.com").hexdigest()


def test_block_without_tmp_login_refuses():
    """A luci block is listed but not enforced, so DecoClient never falls back to it."""
    with pytest.raises(DecoError, match="TP-Link ID email"):
        DecoClient("192.168.68.1", "pw").block(["AA-AA-AA-AA-AA-01"])
