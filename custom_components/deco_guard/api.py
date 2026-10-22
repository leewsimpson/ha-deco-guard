"""Local (luci) API client for TP-Link Deco routers.

Vendored and adapted from https://github.com/msageha/tplink-deco-be85-api
(GPL-3.0-or-later). Crypto was ported from pycryptodome to `cryptography`,
which Home Assistant already ships, so the integration has no extra requirements.

The client is synchronous and keeps session state, so callers must serialise
access (the coordinator runs every call in the executor under a lock).
"""

from __future__ import annotations

import json
import os
import re
from base64 import b64decode, b64encode
from hashlib import md5
from typing import Any

import requests
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_SYSAUTH_RE = re.compile(r"sysauth=([^;]+)")
# The signing RSA key is 512 bit: PKCS#1 v1.5 fits 64 - 11 = 53 bytes per block.
_SIGN_CHUNK = 53

BLACK_LIST = "admin/client?form=black_list"


class DecoError(Exception):
    """Communication or response failure. error_code is the router's code, if any."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class DecoAuthError(DecoError):
    """Login failed (wrong password, lockout, unexpected login response)."""


class DecoConnectionError(DecoError):
    """Router unreachable (timeout, refused, DNS)."""


def normalize_mac(mac: str) -> str:
    """Return the router's MAC notation: upper case, dash separated."""
    mac = mac.strip().upper().replace(":", "-")
    if not re.fullmatch(r"[0-9A-F]{2}(-[0-9A-F]{2}){5}", mac):
        raise ValueError(f"Invalid MAC address: {mac}")
    return mac


def _public_key(modulus_hex: str, exponent_hex: str) -> rsa.RSAPublicKey:
    return rsa.RSAPublicNumbers(int(exponent_hex, 16), int(modulus_hex, 16)).public_key()


def _rsa_encrypt(key: rsa.RSAPublicKey, data: bytes) -> str:
    return key.encrypt(data, asym_padding.PKCS1v15()).hex()


class _SessionCipher:
    """AES key/IV and signing material for one login session."""

    def __init__(self, sign_key: tuple[str, str], seq: int, cred_hash: str) -> None:
        # Like the web UI: 8 random bytes hex-encoded become the 16-byte AES-128 key / IV.
        self._key = os.urandom(8).hex().encode()
        self._iv = os.urandom(8).hex().encode()
        self._signer = _public_key(*sign_key)
        self._seq = seq
        self._cred_hash = cred_hash

    def _cipher(self) -> Cipher:
        return Cipher(algorithms.AES(self._key), modes.CBC(self._iv))

    def encrypt(self, plaintext: str, *, login: bool = False) -> dict[str, str]:
        """Encrypt a body and return the `sign` / `data` form fields."""
        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext.encode()) + padder.finalize()
        enc = self._cipher().encryptor()
        data = b64encode(enc.update(padded) + enc.finalize()).decode()
        fields = f"h={self._cred_hash}&s={self._seq + len(data)}"
        if login:
            fields = f"k={self._key.decode()}&i={self._iv.decode()}&{fields}"
        sign = "".join(
            _rsa_encrypt(self._signer, fields[i : i + _SIGN_CHUNK].encode())
            for i in range(0, len(fields), _SIGN_CHUNK)
        )
        return {"sign": sign, "data": data}

    def decrypt(self, data: str) -> str:
        dec = self._cipher().decryptor()
        padded = dec.update(b64decode(data)) + dec.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode()


def _decode_names(items: list[Any], *keys: str) -> list[Any]:
    """The router base64-encodes names; decode them, leaving non-base64 values alone."""
    for item in items:
        for key in keys:
            value = item.get(key)
            if not isinstance(value, str):
                continue
            try:
                item[key] = b64decode(value, validate=True).decode()
            except ValueError:
                pass
    return items


def _decrypt_envelope(cipher: _SessionCipher, envelope: dict[str, Any]) -> dict[str, Any]:
    data = envelope.get("data")
    if isinstance(data, str) and data:
        return json.loads(cipher.decrypt(data))
    return envelope


class DecoClient:
    """Synchronous client holding one login session to the main Deco node."""

    def __init__(
        self,
        host: str,
        password: str,
        *,
        account: str = "admin",
        verify_ssl: bool = False,
        timeout: int = 30,
    ) -> None:
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        self.host = host.rstrip("/")
        self._password = password
        # The local API signs with the local admin account name, not the TP-Link ID.
        self._cred_hash = md5((account + password).encode(), usedforsecurity=False).hexdigest()
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._http = requests.Session()
        self._cipher: _SessionCipher | None = None
        self._stok = ""
        self._sysauth = ""
        # Diagnostics: the last decrypted response, and how many re-logins _send has done.
        self.last_response: dict[str, Any] | None = None
        self.relogins = 0

    def close(self) -> None:
        self.logout()
        self._http.close()

    def _post(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> requests.Response:
        try:
            return self._http.post(
                f"{self.host}/cgi-bin/luci/;stok={self._stok}/{path}",
                params=params,
                data=data,
                headers={"Content-Type": "application/json"},
                cookies={"sysauth": self._sysauth} if self._sysauth else None,
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except requests.RequestException as err:
            raise DecoConnectionError(f"Cannot reach Deco at {self.host}: {err}") from err

    def _login_form(self, form: str) -> dict[str, Any]:
        resp = self._post(f"login?form={form}", params={"operation": "read"})
        try:
            return resp.json()["result"]
        except (ValueError, KeyError, TypeError) as err:
            raise DecoAuthError(
                f"Unexpected response from login form '{form}' (is this the main Deco?)"
            ) from err

    def login(self) -> _SessionCipher:
        """Log in and rebuild the session (cipher, stok, sysauth cookie)."""
        self._cipher, self._stok, self._sysauth = None, "", ""
        keys = self._login_form("keys")
        auth = self._login_form("auth")
        try:
            cipher = _SessionCipher(auth["key"], int(auth["seq"]), self._cred_hash)
            encrypted_password = _rsa_encrypt(
                _public_key(*keys["password"]), self._password.encode()
            )
        except (KeyError, TypeError, ValueError) as err:
            raise DecoAuthError("Unexpected login keys from Deco") from err
        body = {"operation": "login", "params": {"password": encrypted_password}}
        resp = self._post("login?form=login", data=cipher.encrypt(json.dumps(body), login=True))
        try:
            envelope = _decrypt_envelope(cipher, resp.json())
        except (ValueError, AttributeError) as err:
            raise DecoAuthError("Unexpected login response from Deco") from err
        try:
            stok = envelope["result"]["stok"]
        except (KeyError, TypeError) as err:
            # Wrong password: error_code -5002 with failureCount / attemptsAllowed.
            result = envelope.get("result") or {}
            raise DecoAuthError(
                f"Login failed (error_code {envelope.get('error_code')}, "
                f"attempts {result.get('failureCount')}/{result.get('attemptsAllowed')})",
                error_code=envelope.get("error_code"),
            ) from err
        match = _SYSAUTH_RE.search(resp.headers.get("set-cookie", ""))
        if match is None:
            raise DecoAuthError("Login succeeded but no sysauth cookie was returned")
        self._cipher, self._stok, self._sysauth = cipher, stok, match.group(1)
        return cipher

    def logout(self) -> None:
        if self._cipher is None:
            return
        try:
            self._post(
                "admin/system?form=logout",
                data=self._cipher.encrypt(json.dumps({"operation": "logout"})),
            )
        except DecoConnectionError:
            pass
        finally:
            self._cipher, self._stok, self._sysauth = None, "", ""

    def _send(self, path: str, body: dict[str, Any], *, retry: bool = True) -> dict[str, Any]:
        cipher = self._cipher or self.login()
        resp = self._post(path, data=cipher.encrypt(json.dumps(body)))
        try:
            envelope = resp.json()
        except ValueError:
            envelope = None
        if (
            not isinstance(envelope, dict)
            or resp.status_code in (401, 403)
            # Another login (web UI, another client) took over the session: the Deco then
            # answers HTTP 200 with an empty `data` and no error_code.
            or (envelope.get("data") == "" and "error_code" not in envelope)
        ):
            # Session expired (login HTML, 401/403 or taken over): log in again once and resend.
            if not retry:
                raise DecoError(f"Deco rejected request '{path}' (HTTP {resp.status_code})")
            self._cipher = None
            self.relogins += 1
            return self._send(path, body, retry=False)
        try:
            return _decrypt_envelope(cipher, envelope)
        except ValueError as err:
            raise DecoError(f"Cannot decrypt response for '{path}'") from err

    def request(
        self, path: str, operation: str = "read", params: dict[str, Any] | None = None
    ) -> Any:
        """Call an endpoint and return `result` (or `data`); raise DecoError on failure."""
        body: dict[str, Any] = {"operation": operation}
        if params is not None:
            body["params"] = params
        envelope = self._send(path, body)
        self.last_response = envelope
        if envelope.get("error_code") == 0:
            return envelope.get("result")
        if envelope.get("success") is True:
            return envelope.get("data")
        raise DecoError(
            f"Deco request '{path}' ({operation}) failed: error_code {envelope.get('error_code')}",
            error_code=envelope.get("error_code"),
        )

    def get_device_list(self) -> list[dict[str, Any]]:
        """Deco mesh nodes."""
        result = self.request("admin/device?form=device_list") or {}
        return _decode_names(result.get("device_list", []), "nickname", "custom_nickname")

    def get_client_list(self) -> list[dict[str, Any]]:
        """Currently connected clients (blocked clients are not listed)."""
        result = (
            self.request("admin/client?form=client_list", "read", {"device_mac": "default"}) or {}
        )
        return _decode_names(result.get("client_list", []), "name")

    def get_blocked_macs(self) -> set[str]:
        result = self.request(BLACK_LIST, "list") or {}
        clients = result.get("client_list", [])
        if isinstance(clients, dict):  # an empty Lua table comes back as {}
            clients = list(clients.values())
        return {normalize_mac(c["mac"]) for c in clients if c.get("mac")}

    def block(self, macs: list[str]) -> None:
        """Add MACs to the block list. Blocked clients are refused at the router."""
        self.request(BLACK_LIST, "block", {"client_list": [{"mac": m} for m in macs]})

    def unblock(self, macs: list[str]) -> None:
        # Unlike block, client_list is a plain list of MAC strings; objects make the
        # router's Lua gsub fail with HTTP 500.
        self.request(BLACK_LIST, "unblock", {"client_list": list(macs)})
