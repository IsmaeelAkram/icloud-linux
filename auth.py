#!/usr/bin/env python3
"""
auth.py — icloud-linux one-time authentication bootstrap.

Modes:
  1. Normal (default): triggers Apple's preferred 2FA delivery (push to device)
  2. --force-sms: bypasses push delivery and forces an SMS code to your trusted
     phone number — useful when iOS beta push doesn't show a numeric code
  3. --trust-token <value>: inject a browser trust-token extracted from
     icloud.com DevTools → Application → Cookies → X-APPLE-WEBAUTH-HSA-TRUST

Usage:
  ./icloudctl auth
  .venv/bin/python auth.py ~/.config/icloud-linux/config.yaml
  .venv/bin/python auth.py ~/.config/icloud-linux/config.yaml --force-sms
  .venv/bin/python auth.py ~/.config/icloud-linux/config.yaml --trust-token <TOKEN>

iOS 26 beta workaround:
  If Apple is sending a push popup instead of a numeric code, use --force-sms.
  This PUTs directly to /appleauth/auth/verify/phone with mode=sms, bypassing
  the push/trusted-device bridge that iOS 26 beta fails to complete.
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


def print_auth_diagnostics(api):
    """Print debug info about what auth mode Apple is reporting."""
    print("\n--- Auth diagnostics ---")
    auth_data = getattr(api, '_auth_data', {})
    print(f"  mode (raw):            {auth_data.get('mode', '(not set)')}")
    print(f"  two_factor_delivery:   {getattr(api, 'two_factor_delivery_method', '?')}")
    print(f"  requires_2fa:          {api.requires_2fa}")
    tp = api._trusted_phone_number() if hasattr(api, '_trusted_phone_number') else None
    if tp:
        print(f"  trusted_phone id:      {tp.device_id}")
        print(f"  trusted_phone mode:    {tp.push_mode}")
        print(f"  trusted_phone nonFTEU: {tp.non_fteu}")
    else:
        print("  trusted_phone:         (none found)")
    supports_bridge = api._supports_trusted_device_bridge() if hasattr(api, '_supports_trusted_device_bridge') else '?'
    can_sms = api._can_request_sms_2fa_code() if hasattr(api, '_can_request_sms_2fa_code') else '?'
    print(f"  supports_bridge:       {supports_bridge}")
    print(f"  can_request_sms:       {can_sms}")
    print("------------------------\n")


def do_sms_forced(api):
    """Force SMS delivery regardless of what push_mode Apple reported.

    iOS 26 beta reports push_mode='push' on the trusted phone number, which
    makes _can_request_sms_2fa_code() return False even though the SMS endpoint
    still works.  We call _request_sms_2fa_code() directly to bypass the guard.
    """
    print("\nForcing SMS delivery to your trusted phone number...")
    try:
        sent = api._request_sms_2fa_code()
    except Exception as exc:
        print(f"ERROR triggering SMS: {exc}", file=sys.stderr)
        sys.exit(1)

    if not sent:
        print("Apple declined to send an SMS (no trusted phone number on the account?).", file=sys.stderr)
        sys.exit(1)

    print("SMS sent.  Check your phone for a 6-digit code.")
    code = input("Enter 2FA code: ").strip()

    # _validate_sms_code is the correct path when delivery mode is sms
    try:
        api._validate_sms_code(code)
    except Exception as exc:
        print(f"Code validation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if not api.is_trusted_session:
        print("Trusting session...")
        api.trust_session()


def do_2fa(api):
    """Standard 2FA: use Apple's preferred method (push or bridge), then accept code."""
    print("\n2FA required.")
    # Try to trigger whichever delivery Apple prefers for this account
    try:
        triggered = api.request_2fa_code()
    except Exception as exc:
        print(f"Warning: could not trigger 2FA delivery: {exc}", file=sys.stderr)
        triggered = False

    method = api.two_factor_delivery_method
    if method == "trusted_device":
        print("A 2FA prompt has been sent to your trusted Apple device.")
        print("Tap Allow on your device, then enter the 6-digit code that appears.")
    elif method == "sms":
        print("A 2FA code has been sent via SMS to your trusted phone number.")
    else:
        print(f"2FA delivery mode: {method}")
        print("Watch your iPhone/iPad for a popup or SMS with a 6-digit code.")
        if not triggered:
            print("\nHINT: If you see a push popup instead of a code, re-run with --force-sms")

    code = input("\nEnter 2FA code: ").strip()
    if not api.validate_2fa_code(code):
        print("Invalid 2FA code.", file=sys.stderr)
        sys.exit(1)
    if not api.is_trusted_session:
        print("Trusting session...")
        api.trust_session()


def main():
    args = sys.argv[1:]
    config_path = os.path.expanduser(
        args[0] if args and not args[0].startswith("--") else "~/.config/icloud-linux/config.yaml"
    )

    force_sms = "--force-sms" in args
    trust_token = None
    if "--trust-token" in args:
        idx = args.index("--trust-token")
        if idx + 1 < len(args):
            trust_token = args[idx + 1]
    debug = "--debug" in args

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
            pass  # requires_2fa check below handles this

    if debug:
        print_auth_diagnostics(api)

    # After authenticate(), requires_2fa may still be True even without exception
    if api.requires_2fa:
        if force_sms:
            do_sms_forced(api)
        else:
            do_2fa(api)

    print(f"\nrequires_2fa:  {api.requires_2fa}")
    print(f"is_trusted:    {api.is_trusted_session}")

    if api.requires_2fa:
        print("\nAuthentication incomplete — still requires 2FA.", file=sys.stderr)
        print("If you saw a push popup instead of a numeric code, try:", file=sys.stderr)
        print("  ./icloudctl auth --force-sms", file=sys.stderr)
        sys.exit(1)

    print("\nAUTH_OK")
    print(f"Session saved to: {cookie_dir}")

    if hasattr(api, 'data') and api.data and "dsInfo" in api.data:
        print(f"Authenticated as: {api.data['dsInfo'].get('appleId', '?')}")


if __name__ == "__main__":
    main()
