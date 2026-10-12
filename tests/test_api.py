"""DecoClient session handling (no router needed)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.deco_guard.api import DecoClient, DecoError


def _resp(status: int, body) -> MagicMock:
    resp = MagicMock(status_code=status, text=str(body))
    resp.json.return_value = body
    return resp


class _Cipher:
    def encrypt(self, plaintext, *, login=False):
        return {"sign": "s", "data": plaintext}

    def decrypt(self, data):
        return data


@pytest.mark.parametrize(
    "expired",
    [
        _resp(200, {"data": ""}),  # session taken over by another login
        _resp(403, {"error_code": -1}),
    ],
)
def test_relogin_when_session_lost(expired):
    client = DecoClient("192.168.68.1", "pw")
    logins = []

    def login():
        logins.append(1)
        client._cipher = _Cipher()
        return client._cipher

    client.login = login
    client._cipher = _Cipher()
    client._post = MagicMock(
        side_effect=[expired, _resp(200, {"error_code": 0, "result": {"x": 1}})]
    )
    assert client.request("admin/client?form=black_list", "list") == {"x": 1}
    assert logins == [1]


def test_gives_up_after_one_relogin():
    client = DecoClient("192.168.68.1", "pw")
    client.login = lambda: setattr(client, "_cipher", _Cipher()) or client._cipher
    client._cipher = _Cipher()
    client._post = MagicMock(return_value=_resp(200, {"data": ""}))
    with pytest.raises(DecoError):
        client.request("admin/client?form=black_list", "list")
    assert client._post.call_count == 2
