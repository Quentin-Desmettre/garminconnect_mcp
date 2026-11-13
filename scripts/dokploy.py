#!/usr/bin/env python3
"""Deploy to the VPS through the Dokploy API.

    python3 scripts/dokploy.py status   last deployments + health
    python3 scripts/dokploy.py deploy   deploy `main` as pushed to GitHub, wait, check /health

The API key is never in the repo: DOKPLOY_API_KEY, or DOKPLOY_API_KEY_FILE
(path of a file containing it). Non-secret ids live in deploy/dokploy.json.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "deploy" / "dokploy.json").read_text())


def _key() -> str:
    key = os.getenv("DOKPLOY_API_KEY")
    if not key and os.getenv("DOKPLOY_API_KEY_FILE"):
        key = Path(os.environ["DOKPLOY_API_KEY_FILE"]).expanduser().read_text().strip()
    if not key:
        sys.exit("Missing API key: set DOKPLOY_API_KEY or DOKPLOY_API_KEY_FILE.")
    return key


def api(route: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{CONFIG['url']}/api/{route}",
        method="GET" if body is None else "POST",
        headers={"x-api-key": _key(), "content-type": "application/json"},
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{route} -> HTTP {exc.code}: {exc.read().decode()[:500]}") from exc
    return json.loads(text) if text else None


def deployments() -> list[dict]:
    return api(f"deployment.all?applicationId={CONFIG['applicationId']}") or []


def health() -> str:
    try:
        with urllib.request.urlopen(f"{CONFIG['domain']}/health", timeout=10) as resp:
            return "ok" if resp.status == 200 else f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return f"unreachable ({exc})"


def summary(d: dict) -> str:
    title = str(d.get("title") or "").split("\n")[0]
    return f"{d.get('status', '?'):<8} {str(d.get('createdAt', ''))[:19]}  {title}"


def deploy() -> None:
    known = {d["deploymentId"] for d in deployments()}
    api("application.deploy", {"applicationId": CONFIG["applicationId"]})
    print("Deployment started...")
    for _ in range(90):
        time.sleep(10)
        fresh = next((d for d in deployments() if d["deploymentId"] not in known), None)
        if fresh is None:
            continue
        if fresh["status"] == "error":
            sys.exit(f"Deployment failed: {summary(fresh)}")
        if fresh["status"] == "done":
            for _ in range(24):  # Traefik + Let's Encrypt can take a little while
                time.sleep(5)
                if health() == "ok":
                    print(f"Deployed and healthy: {summary(fresh)}")
                    return
            sys.exit(f"Deployed, but /health is {health()}: check the logs in Dokploy.")
    sys.exit("Deployment did not finish within 15 minutes.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "status":
        for d in deployments()[:5]:
            print(summary(d))
        print(f"health: {health()}")
    elif cmd == "deploy":
        deploy()
    else:
        sys.exit("Usage: python3 scripts/dokploy.py status | deploy")


if __name__ == "__main__":
    main()
