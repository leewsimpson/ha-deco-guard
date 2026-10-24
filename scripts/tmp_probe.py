"""Send raw TMP (appv2) requests to the Deco's app management service (for exploring it).

This is the channel the Deco app uses. TMP listens on 127.0.0.1:20002 on the master Deco,
behind an SSH server on port 20001. The SSH login is the TP-Link account: the username is
hex(SHA-1(account email)) and the password is the account password.

With --ssh the script logs in itself (needs paramiko), reading DECO_HOST / DECO_USERNAME
(the account email) / DECO_PASSWORD from the environment or the .env next to the ha-vibe
checkout. It makes one login attempt only: repeated bad logins can lock the account.

  uv run --no-project --with paramiko python scripts/tmp_probe.py --ssh blocked_list_get

Without --ssh it connects to the local end of a tunnel you open yourself:

  python scripts/tmp_probe.py --user you@example.com     # prints the SSH username
  ssh -N -p 20001 -L 20002:127.0.0.1:20002 <username>@192.168.68.1

Then, in another terminal:

  python scripts/tmp_probe.py                            # allocate a session token only
  python scripts/tmp_probe.py blocked_list_get           # one read (name or opcode)
  python scripts/tmp_probe.py 0x4403 -v                  # same, hex-dumping every frame
  python scripts/tmp_probe.py --assoc blocked_list_get   # send the ASSOC handshake first
  python scripts/tmp_probe.py --write blocked_list_unblock '{"client_list": ["AA-BB-CC-DD-EE-FF"]}'

Framing follows two public clients: mthsg/pydeco-tmp (from a captured app session) and
roger-/tmpkit (from the Android app). They disagree on one point: pydeco says the app sends
no handshake over SSH and that ASSOC corrupts the session; tmpkit sends ASSOC. Try without
--assoc first. Opcodes not in READS are refused unless --write is given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import struct
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRC_SEED = 0x5A6B7C8D
ASSOC_REQ, ASSOC_ACCEPT, ASSOC_REFUSE, HELLO, DATA, BYE = 1, 2, 3, 4, 5, 6
PUSH, PUSH_ACK, PULL, PULL_ACK = 2, 3, 4, 5
TPH = struct.Struct("!HBBII")  # payload length, 0, 0, serial, crc
APPV2 = struct.Struct("!BBHBBHIII")  # 1, 2, opcode, flag, error, trans, crc, total, start/ack
OP_TOKEN_ALLOC = 0x0001

# Opcodes from tmpkit (app 1.10.5) and handoff 3 (newer app). Names are for convenience only.
READS = {
    "comp_negotiate": 0x4001,
    "device_list_get": 0x400F,
    "client_list_get": 0x4012,
    "blocked_list_get": 0x4018,
    "network_access_get": 0x4401,
    "blocked_list_get_v2": 0x4403,
    "parental_pause_get": 0x4278,
    "acl_get": 0x4293,
}
WRITES = {
    "client_list_set": 0x4013,
    "client_list_block": 0x4017,
    "blocked_list_unblock": 0x4019,
    "client_list_remove": 0x4090,
    "client_list_block_v2": 0x4404,
    "blocked_list_unblock_v2": 0x4405,
    "client_access_set": 0x4407,
}


class TmpError(Exception):
    pass


def dump(label: str, raw: bytes) -> None:
    print(f"{label} ({len(raw)} bytes)", file=sys.stderr)
    for i in range(0, len(raw), 16):
        chunk = raw[i : i + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {i:04x}  {chunk.hex(' '):<47}  {text}", file=sys.stderr)


class TmpSession:
    def __init__(self, sock: socket.socket, verbose: bool) -> None:
        self.sock = sock
        self.verbose = verbose
        self.serial = 1
        self.trans = 0

    def send(self, raw: bytes) -> None:
        if self.verbose:
            dump(">>", raw)
        self.sock.sendall(raw)

    def recvn(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise TmpError(f"connection closed ({len(buf)}/{n} bytes of a frame)")
            buf += chunk
        return buf

    def control(self, code: int) -> None:
        self.send(bytes([1, 1, code, 0]))

    def frame(self) -> tuple[int, bytes]:
        """Read one frame; answer keep-alives. Returns (control code, data payload)."""
        while True:
            head = self.recvn(4)
            if head[2] == HELLO:
                if self.verbose:
                    dump("<< HELLO", head)
                self.control(HELLO)
                continue
            if head[2] != DATA:
                if self.verbose:
                    dump("<<", head)
                return head[2], b""
            tph = self.recvn(TPH.size)
            length, _, _, _, crc = TPH.unpack(tph)
            payload = self.recvn(length)
            if self.verbose:
                dump("<<", head + tph + payload)
            seeded = head + tph[:8] + struct.pack("!I", CRC_SEED) + payload
            if zlib.crc32(seeded) & 0xFFFFFFFF != crc:
                print("warning: TMP frame CRC mismatch", file=sys.stderr)
            return DATA, payload

    def data(
        self,
        opcode: int,
        flag: int,
        trans: int,
        body: bytes = b"",
        total: int | None = None,
        start: int = 0,
    ) -> None:
        crc = zlib.crc32(body) & 0xFFFFFFFF if body else 0
        total = len(body) if total is None else total
        payload = APPV2.pack(1, 2, opcode, flag, 0, trans, crc, total, start) + body
        head = bytes([1, 1, DATA, 0]) + TPH.pack(len(payload), 0, 0, self.serial, CRC_SEED)
        self.serial += 1
        crc = zlib.crc32(head + payload) & 0xFFFFFFFF
        self.send(head[:12] + struct.pack("!I", crc) + payload)

    def assoc(self) -> None:
        self.control(ASSOC_REQ)
        while True:
            code, _ = self.frame()
            if code == ASSOC_ACCEPT:
                self.control(ASSOC_ACCEPT)
                self.control(HELLO)
                return
            if code in (ASSOC_REFUSE, BYE):
                raise TmpError(f"association refused (control {code})")

    def request(self, opcode: int, body: bytes = b"") -> bytes:
        trans, self.trans = self.trans, (self.trans + 1) & 0xFFFF
        self.data(opcode, PUSH, trans, body)
        chunks: dict[int, bytes] = {}
        total = None
        pulled = False
        while total is None or sum(map(len, chunks.values())) < total:
            code, payload = self.frame()
            if code == BYE:
                raise TmpError("server sent BYE")
            if code != DATA or len(payload) < APPV2.size:
                continue
            _, _, op, flag, err, tr, _, size, start = APPV2.unpack_from(payload)
            if tr != trans or op != opcode:
                continue
            if err:
                raise TmpError(f"appv2 error {err} (opcode {opcode:#06x}, flag {flag})")
            if flag == PUSH_ACK:
                if not pulled and not chunks:
                    self.data(opcode, PULL, trans)
                    pulled = True
                continue
            if flag not in (PULL_ACK, PUSH):
                continue
            total = size
            chunks[start] = payload[APPV2.size :]
            end = start + len(chunks[start])
            if flag == PUSH:
                self.data(opcode, PUSH_ACK, trans, total=0, start=end)
            if end < total:
                self.data(opcode, PULL, trans, start=end)
                pulled = True
        return b"".join(chunks[k] for k in sorted(chunks))[:total]


def opcode_of(name: str) -> int:
    if name in READS or name in WRITES:
        return {**READS, **WRITES}[name]
    return int(name, 0)


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def open_ssh(host: str, email: str, password: str):
    """Log in to the Deco's SSH server once and open a channel to its TMP service."""
    import paramiko

    transport = paramiko.Transport(socket.create_connection((host, 20001), timeout=10))
    transport.start_client(timeout=10)
    try:
        transport.auth_password(hashlib.sha1(email.encode()).hexdigest(), password)
    except paramiko.BadAuthenticationType as err:
        transport.close()
        raise TmpError(f"password login not offered; server allows {err.allowed_types}") from None
    except paramiko.AuthenticationException:
        transport.close()
        raise TmpError("SSH login rejected (not retrying: repeated failures can lock the account)")
    channel = transport.open_channel("direct-tcpip", ("127.0.0.1", 20002), ("127.0.0.1", 54321))
    channel.settimeout(10)
    return transport, channel


def show(raw: bytes) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except ValueError:
        return raw.decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("opcode", nargs="?", help="name from READS/WRITES, or a number (0x4403)")
    parser.add_argument("params", nargs="?", help="JSON object sent as params")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=20002)
    parser.add_argument("--assoc", action="store_true", help="send tmpkit's ASSOC handshake first")
    parser.add_argument("--write", action="store_true", help="allow an opcode that isn't a read")
    parser.add_argument("--user", metavar="EMAIL", help="print the SSH username for EMAIL")
    parser.add_argument("--ssh", action="store_true", help="log in with the .env credentials")
    parser.add_argument("--env", type=Path, default=ROOT.parent / ".env")
    parser.add_argument("-v", "--verbose", action="store_true", help="hex-dump every frame")
    args = parser.parse_args()

    if args.user:
        print(hashlib.sha1(args.user.strip().encode()).hexdigest())
        return 0
    try:
        opcode = opcode_of(args.opcode) if args.opcode else None
    except ValueError:
        parser.error(f"unknown opcode {args.opcode!r}")
    if opcode is not None and opcode not in READS.values() and not args.write:
        parser.error(f"{opcode:#06x} isn't a known read; pass --write to send it")

    transport = None
    if args.ssh:
        load_env(args.env)
        host, email = os.environ.get("DECO_HOST"), os.environ.get("DECO_USERNAME")
        password = os.environ.get("DECO_PASSWORD")
        if not host or not email or not password:
            print(
                f"Set DECO_HOST, DECO_USERNAME and DECO_PASSWORD (env or {args.env})",
                file=sys.stderr,
            )
            return 2
        try:
            # DECO_HOST is the web UI's URL (http://192.168.68.1); SSH needs the bare host.
            host = host.split("://")[-1].strip("/")
            transport, sock = open_ssh(host, email, password)
        except (TmpError, OSError) as err:
            print(f"Failed: {str(err) or type(err).__name__}", file=sys.stderr)
            return 1
    else:
        try:
            sock = socket.create_connection((args.host, args.port), timeout=10)
        except OSError as err:
            print(
                f"Can't reach {args.host}:{args.port} ({err}). Is the SSH tunnel open?",
                file=sys.stderr,
            )
            return 2
    with sock:
        tmp = TmpSession(sock, args.verbose)
        try:
            if args.assoc:
                tmp.assoc()
            token = tmp.request(OP_TOKEN_ALLOC)
            print(f"token: {token.decode('ascii', 'replace')}", file=sys.stderr)
            if opcode is not None:
                body = {"config_version": int(time.time() * 1000)}
                if args.params:
                    body["params"] = json.loads(args.params)
                print(show(tmp.request(opcode, json.dumps(body, separators=(",", ":")).encode())))
        except (TmpError, OSError) as err:
            print(f"Failed: {str(err) or type(err).__name__}", file=sys.stderr)
            return 1
        finally:
            try:
                tmp.control(BYE)
            except OSError:
                pass
            if transport:
                transport.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
