"""Client for the Deco app's own management protocol (TMP), used to block and unblock.

A luci `black_list` write lists a block but the router doesn't enforce it, and a luci unblock
leaves an enforced block in place. The Deco app's block/unblock over TMP does both properly.

TMP listens on 127.0.0.1:20002 on the main Deco, behind an SSH server on port 20001. The SSH
login is the TP-Link account: username hex(SHA-1(email)), password the account password (the
same one the local web UI takes). Framing follows mthsg/pydeco-tmp and roger-/tmpkit; checked
on a BE85 (fw 1.2.4), which needs no ASSOC handshake and answers each PUSH with a PULL_ACK.

A blocked client can't reach the Deco at all, so block/unblock must come from another host.

The client is synchronous and opens one SSH session per operation, so callers must serialise
access (the coordinator runs every call in the executor under a lock).
"""

from __future__ import annotations

import json
import socket
import struct
import time
import zlib
from base64 import b64encode
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha1
from typing import Any

import paramiko

from .api import DecoAuthError, DecoConnectionError, DecoError, normalize_mac

SSH_PORT = 20001
TMP_ADDR = ("127.0.0.1", 20002)

OP_TOKEN_ALLOC = 0x0001
OP_CLIENT_LIST = 0x4012
OP_BLOCK = 0x4017
OP_BLOCKED_LIST = 0x4018
OP_UNBLOCK = 0x4019

_CRC_SEED = 0x5A6B7C8D
_HELLO, _DATA, _BYE = 4, 5, 6
_PUSH, _PUSH_ACK, _PULL, _PULL_ACK = 2, 3, 4, 5
_TPH = struct.Struct("!HBBII")  # payload length, 0, 0, serial, crc
_APPV2 = struct.Struct("!BBHBBHIII")  # 1, 2, opcode, flag, error, trans, crc, total, start/ack


class TmpAuthError(DecoAuthError):
    """The Deco rejected the TP-Link ID login on the TMP channel."""


class TmpSession:
    """TMP framing over an open channel (anything with sendall/recv)."""

    def __init__(self, channel: Any) -> None:
        self._chan = channel
        self._serial = 1
        self._trans = 0

    def _recvn(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._chan.recv(n - len(buf))
            if not chunk:
                raise DecoConnectionError("Deco closed the TMP channel")
            buf += chunk
        return buf

    def _frame(self) -> tuple[int, bytes]:
        """Next frame as (control code, payload); keep-alives are answered and skipped."""
        while True:
            head = self._recvn(4)
            if head[2] == _HELLO:
                self._chan.sendall(bytes([1, 1, _HELLO, 0]))
                continue
            if head[2] != _DATA:
                return head[2], b""
            length = _TPH.unpack(self._recvn(_TPH.size))[0]
            return _DATA, self._recvn(length)

    def _send(self, opcode: int, flag: int, trans: int, body: bytes = b"", start: int = 0) -> None:
        crc = zlib.crc32(body) & 0xFFFFFFFF if body else 0
        total = 0 if flag == _PUSH_ACK else len(body)
        payload = _APPV2.pack(1, 2, opcode, flag, 0, trans, crc, total, start) + body
        head = bytes([1, 1, _DATA, 0]) + _TPH.pack(len(payload), 0, 0, self._serial, _CRC_SEED)
        self._serial += 1
        crc = zlib.crc32(head + payload) & 0xFFFFFFFF
        self._chan.sendall(head[:12] + struct.pack("!I", crc) + payload)

    def request(self, opcode: int, body: bytes = b"") -> bytes:
        """PUSH one request and collect the response, pulling further chunks if needed."""
        trans, self._trans = self._trans, (self._trans + 1) & 0xFFFF
        self._send(opcode, _PUSH, trans, body)
        chunks: dict[int, bytes] = {}
        total: int | None = None
        pulled = False
        while total is None or sum(map(len, chunks.values())) < total:
            code, payload = self._frame()
            if code == _BYE:
                raise DecoConnectionError("Deco ended the TMP session")
            if code != _DATA or len(payload) < _APPV2.size:
                continue
            _, _, op, flag, err, tr, _, size, start = _APPV2.unpack_from(payload)
            if tr != trans or op != opcode:
                continue
            if err:
                # 12 means the firmware doesn't know the opcode.
                raise DecoError(f"TMP opcode {opcode:#06x} failed (appv2 error {err})")
            if flag == _PUSH_ACK:
                if not pulled and not chunks:
                    self._send(opcode, _PULL, trans)
                    pulled = True
                continue
            if flag not in (_PULL_ACK, _PUSH):
                continue
            total = size
            chunks[start] = payload[_APPV2.size :]
            end = start + len(chunks[start])
            if flag == _PUSH:
                self._send(opcode, _PUSH_ACK, trans, start=end)
            if end < total:
                self._send(opcode, _PULL, trans, start=end)
                pulled = True
        return b"".join(chunks[k] for k in sorted(chunks))[:total]


class TmpClient:
    """Block/unblock through the Deco app's protocol, one SSH session per call."""

    def __init__(self, host: str, email: str, password: str, *, timeout: int = 15) -> None:
        # The luci host is a URL (http://192.168.68.1); SSH needs the bare address.
        self.host = host.split("://")[-1].split("/")[0].split(":")[0]
        self._user = sha1(email.strip().encode()).hexdigest()
        self._password = password
        self._timeout = timeout
        self._auth_failed = False

    @contextmanager
    def _session(self) -> Iterator[TmpSession]:
        if self._auth_failed:
            # One rejected login is enough: repeated failures lock the TP-Link account.
            raise TmpAuthError("TMP login was rejected earlier; not retrying")
        try:
            sock = socket.create_connection((self.host, SSH_PORT), timeout=self._timeout)
        except OSError as err:
            raise DecoConnectionError(f"Cannot reach Deco SSH at {self.host}: {err}") from err
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=self._timeout)
            try:
                transport.auth_password(self._user, self._password)
            except paramiko.AuthenticationException as err:
                self._auth_failed = True
                raise TmpAuthError(
                    "TMP login failed: check the TP-Link ID email and password"
                ) from err
            channel = transport.open_channel(
                "direct-tcpip", TMP_ADDR, ("127.0.0.1", 54321), timeout=self._timeout
            )
            channel.settimeout(self._timeout)
            session = TmpSession(channel)
            session.request(OP_TOKEN_ALLOC)
            yield session
        except (paramiko.SSHException, OSError) as err:
            raise DecoConnectionError(f"TMP session to {self.host} failed: {err}") from err
        finally:
            transport.close()

    @staticmethod
    def _call(session: TmpSession, opcode: int, params: dict[str, Any] | None = None) -> dict:
        """Send one request and return the decoded response; raise DecoError on failure."""
        body: dict[str, Any] = {"config_version": int(time.time() * 1000)}
        if params is not None:
            body["params"] = params
        raw = session.request(opcode, json.dumps(body, separators=(",", ":")).encode())
        try:
            response = json.loads(raw) if raw else {}
        except ValueError as err:
            raise DecoError(f"TMP opcode {opcode:#06x}: unreadable response") from err
        if response.get("error_code", 0) != 0:
            raise DecoError(
                f"TMP opcode {opcode:#06x} failed: error_code {response['error_code']}",
                error_code=response["error_code"],
            )
        return response

    def blocked_macs(self) -> set[str]:
        with self._session() as session:
            result = self._call(session, OP_BLOCKED_LIST).get("result") or {}
        return {normalize_mac(c["mac"]) for c in result.get("client_list", []) if c.get("mac")}

    def block(self, macs: list[str]) -> dict[str, Any]:
        """Block MACs. Each entry carries the client's name and type, as the Deco app sends."""
        with self._session() as session:
            result = self._call(session, OP_CLIENT_LIST).get("result") or {}
            known = {
                normalize_mac(c["mac"]): c for c in result.get("client_list", []) if c.get("mac")
            }
            entries = []
            for mac in macs:
                client = known.get(normalize_mac(mac), {})
                entries.append(
                    {
                        "mac": mac,
                        "name": client.get("name") or b64encode(mac.encode()).decode(),
                        "client_type": client.get("client_type") or "other",
                    }
                )
            return self._call(session, OP_BLOCK, {"client_list": entries})

    def unblock(self, macs: list[str]) -> dict[str, Any]:
        with self._session() as session:
            return self._call(session, OP_UNBLOCK, {"client_list": list(macs)})
