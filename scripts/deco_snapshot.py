"""Snapshot every readable Deco setting to JSON, and diff two snapshots (for exploring it).

Reads each known GET opcode over TMP (via tmp_probe) and a few luci forms (via the integration's
API), using the same .env as the other scripts. Only reads: the opcode list below is GETs taken
from tmpkit's map and pydeco's capture, minus anything that starts a scan.

  uv run --no-project --with paramiko --with requests python scripts/deco_snapshot.py snap.json
  python scripts/deco_snapshot.py --diff before.json after.json [--noise base1.json base2.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import tmp_probe

GETS = [
    0x4001, 0x4004, 0x4006, 0x4009, 0x400A, 0x400C, 0x400D, 0x400F, 0x4010, 0x4012, 0x4014,
    0x4015, 0x4018, 0x401A, 0x401E, 0x4024, 0x4026, 0x4028, 0x4029, 0x402D, 0x402F, 0x4031,
    0x4032, 0x4035, 0x4036, 0x4039, 0x403A, 0x403B, 0x403D, 0x4040, 0x4045, 0x4049, 0x4050,
    0x4060, 0x4070, 0x4079, 0x4080, 0x408C, 0x40A0, 0x40B0, 0x40C0, 0x40D0, 0x40E0, 0x4201,
    0x4202, 0x4208, 0x420D, 0x4211, 0x4213, 0x4215, 0x4219, 0x421B, 0x421D, 0x4222, 0x4224,
    0x4226, 0x4228, 0x4229, 0x422D, 0x422F, 0x4230, 0x4234, 0x4235, 0x4239, 0x423A, 0x423D,
    0x4242, 0x424A, 0x424C, 0x424E, 0x4251, 0x4265, 0x4267, 0x4270, 0x4278, 0x427C, 0x427E,
    0x428B, 0x4293, 0x4294, 0x4298, 0x42E0, 0x42F0, 0x4301, 0x4318, 0x432B, 0x4330, 0x4360,
    0x4370, 0x4378, 0x4381, 0x4383, 0x4385, 0x43B0, 0x43BE, 0x43C4, 0x43D0, 0x43E0,
]  # fmt: skip
LUCI = [
    ("admin/client?form=black_list", "list", None),
    ("admin/client?form=client_list", "read", {"device_mac": "default"}),
    ("admin/client?form=client_access", "read", None),
    ("admin/client?form=addr_reservation", "getlist", None),
    ("admin/device?form=device_list", "read", None),
]
# Keys that obviously change on their own; left out of diffs. Anything else that drifts shows up
# when two baselines are diffed, and --noise drops it.
VOLATILE = re.compile(
    r"speed|traffic|signal|rssi|stamp|config_version|uptime|packets|bytes", re.IGNORECASE
)


def snapshot(env: Path) -> dict:
    tmp_probe.load_env(env)
    host = os.environ["DECO_HOST"]
    email, password = os.environ["DECO_USERNAME"], os.environ["DECO_PASSWORD"]
    out: dict = {"taken": time.strftime("%H:%M:%S"), "tmp": {}, "luci": {}}

    transport, channel = tmp_probe.open_ssh(host.split("://")[-1].strip("/"), email, password)
    try:
        tmp = tmp_probe.TmpSession(channel, verbose=False)
        tmp.request(tmp_probe.OP_TOKEN_ALLOC)
        for opcode in GETS:
            body = json.dumps({"config_version": int(time.time() * 1000)}).encode()
            try:
                raw = tmp.request(opcode, body)
                out["tmp"][f"{opcode:#06x}"] = json.loads(raw) if raw else None
            except (tmp_probe.TmpError, ValueError, OSError) as err:
                out["tmp"][f"{opcode:#06x}"] = {"__error__": str(err)}
    finally:
        transport.close()

    import deco_probe

    client = deco_probe.api.DecoClient(host, password)
    client.login()
    try:
        for path, operation, params in LUCI:
            try:
                out["luci"][f"{path} {operation}"] = client.raw(path, operation, params)
            except deco_probe.api.DecoError as err:
                out["luci"][f"{path} {operation}"] = {"__error__": str(err)}
    finally:
        client.close()
    return out


def flatten(value, prefix: str = "") -> dict[str, object]:
    """Path -> leaf value. Lists of objects with a mac are keyed by mac, so order doesn't matter."""
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        if value and all(isinstance(v, dict) and "mac" in v for v in value):
            items = ((f"[{v['mac']}]", v) for v in value)
        else:
            items = ((f"[{i}]", v) for i, v in enumerate(value))
    else:
        return {prefix: value}
    flat: dict[str, object] = {}
    for key, sub in items:
        flat.update(flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
    return flat


def changed(before: dict, after: dict) -> dict[str, tuple[object, object]]:
    a = {k: v for k, v in flatten(before).items() if not VOLATILE.search(k)}
    b = {k: v for k, v in flatten(after).items() if not VOLATILE.search(k)}
    keys = (a.keys() | b.keys()) - {"taken"}
    return {
        k: (a.get(k, "<absent>"), b.get(k, "<absent>"))
        for k in sorted(keys)
        if a.get(k, "<absent>") != b.get(k, "<absent>")
    }


def diff(before: dict, after: dict, noise: set[str]) -> None:
    print(f"{before.get('taken')} -> {after.get('taken')} ({len(noise)} noisy keys ignored)")
    for key, (old, new) in changed(before, after).items():
        if key not in noise:
            print(f"  {key}: {old!r} -> {new!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--diff", action="store_true", help="diff two snapshot files")
    parser.add_argument(
        "--noise",
        nargs=2,
        type=Path,
        help="two baselines; keys that differ between them are ignored",
    )
    parser.add_argument("--env", type=Path, default=tmp_probe.ROOT.parent / ".env")
    args = parser.parse_args()

    if args.diff:
        load = lambda f: json.loads(f.read_text(encoding="utf-8"))
        noise = set(changed(*map(load, args.noise))) if args.noise else set()
        diff(load(args.files[0]), load(args.files[1]), noise)
        return 0
    snap = snapshot(args.env)
    args.files[0].write_text(json.dumps(snap, indent=1, ensure_ascii=False), encoding="utf-8")
    errors = [k for k, v in snap["tmp"].items() if isinstance(v, dict) and "__error__" in v]
    print(
        f"{snap['taken']}: {len(snap['tmp'])} TMP reads ({len(errors)} errors), "
        f"{len(snap['luci'])} luci reads -> {args.files[0]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
