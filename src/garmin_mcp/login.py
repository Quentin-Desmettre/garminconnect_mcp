"""One-time interactive Garmin Connect login.

Run it inside the container (Dokploy > service > Terminal, or `docker exec -it`):

    garmin-mcp-login

It asks for your Garmin email, password and MFA code if enabled, then saves
long-lived tokens to $DATA_DIR/garmin_tokens.json. The password itself is
never stored. Restart is not needed: the server picks up the file on the
next tool call.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from garminconnect import Garmin, GarminConnectTooManyRequestsError


def main() -> None:
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    tokens = data_dir / "garmin_tokens.json"

    if tokens.exists() and "--force" not in sys.argv:
        api = Garmin()
        try:
            api.login(tokenstore=str(tokens))
            print(f"Already connected as {api.full_name} ({api.display_name}). Use --force to log in again.")
            return
        except Exception as exc:  # noqa: BLE001
            print(f"Existing tokens are not valid ({exc}); logging in again.")
    if tokens.exists():
        tokens.unlink()

    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")
    api = Garmin(email, password, prompt_mfa=lambda: input("MFA code: ").strip())
    try:
        api.login(tokenstore=str(tokens))
    except GarminConnectTooManyRequestsError as exc:
        sys.exit(f"Garmin rate-limited the login: {exc}\nWait 15-30 minutes before retrying.")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Login failed: {exc}")
    if not tokens.exists():
        api.client.dump(str(tokens))
    print(f"Connected as {api.full_name} ({api.display_name}). Tokens saved to {tokens}")


if __name__ == "__main__":
    main()
