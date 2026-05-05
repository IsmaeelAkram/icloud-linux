#!/usr/bin/env python3
"""
auth.py — icloud-linux one-time authentication bootstrap.

Two modes:
  1. Normal: prompts for 2FA code (push to Apple device)
  2. Trust-token mode: supply --trust-token <value> extracted from a browser
     session at icloud.com to bypass the 2FA push entirely.

Usage:
  ./icloudctl auth
  .venv/bin/python auth.py ~/.config/icloud-linux/config.yaml
  .venv/bin/python auth.py ~/.config/icloud-linux/config.yaml --trust-token <TOKEN>
"""
import os
import sys
import yaml
import requests
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloud2FARequiredException


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_partition():
    """Detect Apple's account shard to avoid 421 errors."""
    s = requests.Session()
    s.headers.update({
        "Origin": "https://www.icloud.com",
        "Referer": "https://www.icloud.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    })
    r = s.post("https://setup.icloud.com/setup/ws/1/validate", json={})
    return r.headers.get("x-apple-user-partition")


def main():
    args = sys.argv[1:]
    config_path = os.path.expanduser(
        args[0] if args and not args[0].startswith("--") else "~/.config/icloud-linux/config.yaml"
    )

    trust_token = None
    if "--trust-token" in args:
        idx = args.index("--trust-token")
        if idx + 1 < len(args):
            trust_token = args[idx + 1]

    cfg = load_config(config_path)
    username = cfg.get("username")
    password = cfg.get("password")
    cookie_dir = os.path.expanduser(
        cfg.get("cookie_dir", "~/.config/icloud-linux/cookies")
    )
    os.makedirs(cookie_dir, exist_ok=True)

    if not username or not password:
        print("Missing username/password in config.", file=sys.stderr)
        sys.exit(1)

    print("Detecting Apple account partition...")
    partition = get_partition()
    if partition:
        endpoint = f"https://p{partition}-setup.icloud.com/setup/ws/1"
        print(f"Partition {partition} → {endpoint}")
    else:
        endpoint = None
        print("Using default endpoint.")

    api = PyiCloudService(
        username, password,
        cookie_directory=cookie_dir,
        authenticate=False,
    )
    if endpoint:
        api._setup_endpoint = endpoint

    if trust_token:
        # Browser trust-token mode: inject token and authenticate directly
        print("Trust-token mode: injecting browser session token...")
        api.session.data["trust_token"] = trust_token
        api.authenticate(force_refresh=True)
    else:
        # Standard mode: authenticate and handle 2FA interactively
        try:
            api.authenticate(force_refresh=True)
        except PyiCloud2FARequiredException:
            print("\n2FA required.")
            print("Watch your iPhone or iPad for a system popup — tap Allow.")
            print("A 6-digit code will appear on the device after you tap Allow.")
            code = input("\nEnter 2FA code: ").strip()
            if not api.validate_2fa_code(code):
                print("Invalid 2FA code.", file=sys.stderr)
                sys.exit(1)
            if not api.is_trusted_session:
                api.trust_session()

    print(f"\nrequires_2fa:  {api.requires_2fa}")
    print(f"is_trusted:    {api.is_trusted_session}")

    if api.requires_2fa:
        print("Authentication incomplete — still requires 2FA.", file=sys.stderr)
        sys.exit(1)

    print("\nAUTH_OK")
    print(f"Session saved to: {cookie_dir}")

    if "dsInfo" in api.data:
        print(f"Authenticated as: {api.data['dsInfo'].get('appleId', '?')}")


if __name__ == "__main__":
    main()
