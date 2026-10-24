"""Send raw requests to the Deco's local API from the command line (for exploring it).

Reads DECO_HOST / DECO_PASSWORD from the environment or from an .env file
(--env, default ../../.env next to the ha-vibe checkout). The password is never printed.

  python scripts/deco_probe.py "admin/client?form=black_list" list
  python scripts/deco_probe.py "admin/client?form=client_list" read '{"device_mac": "default"}'
  python scripts/deco_probe.py --batch probes.json      # [[path, operation, params?], ...]

Only one login is allowed at a time, so this briefly takes the session over from
Home Assistant (Deco Guard logs in again on its next poll).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "deco_api", ROOT / "custom_components" / "deco_guard" / "api.py"
)
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?")
    parser.add_argument("operation", nargs="?", default="read")
    parser.add_argument("params", nargs="?", help="JSON object")
    parser.add_argument("--batch", type=Path, help="JSON file: [[path, operation, params?], ...]")
    parser.add_argument("--env", type=Path, default=ROOT.parent / ".env")
    args = parser.parse_args()

    load_env(args.env)
    host, password = os.environ.get("DECO_HOST"), os.environ.get("DECO_PASSWORD")
    if not host or not password:
        print(f"Set DECO_HOST and DECO_PASSWORD (env or {args.env})", file=sys.stderr)
        return 2

    if args.batch:
        calls = json.loads(args.batch.read_text(encoding="utf-8"))
    elif args.path:
        calls = [[args.path, args.operation, json.loads(args.params) if args.params else None]]
    else:
        parser.error("give a path or --batch")

    client = api.DecoClient(host, password)
    try:
        client.login()
    except api.DecoError as err:
        # Stop after one failure: repeated bad logins lock the admin account.
        print(f"Login failed: {err}", file=sys.stderr)
        return 1
    try:
        for call in calls:
            path, operation, params = (list(call) + [None, None])[:3]
            try:
                result = client.raw(path, operation or "read", params)
            except api.DecoError as err:
                result = {"exception": str(err)}
            print(json.dumps({"path": path, "operation": operation, "params": params,
                              "response": result}, ensure_ascii=False))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
