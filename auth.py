#!/usr/bin/env python3
import os
import sys
import yaml
from pyicloud import PyiCloudService


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    config_path = os.path.expanduser(
        sys.argv[1] if len(sys.argv) > 1 else "~/.config/icloud-linux/config.yaml"
    )
    cfg = load_config(config_path)

    username = cfg.get("username")
    password = cfg.get("password")
    cookie_dir = os.path.expanduser(
        cfg.get("cookie_dir", "~/.config/icloud-linux/cookies")
    )
    os.makedirs(cookie_dir, exist_ok=True)

    if not username or not password:
        print("Missing username/password in config", file=sys.stderr)
        sys.exit(1)

    api = PyiCloudService(username, password, cookie_directory=cookie_dir)

    if api.requires_2fa:
        print("2FA required. Enter code from your Apple device.")
        code = input("2FA code: ").strip()
        if not api.validate_2fa_code(code):
            print("Invalid 2FA code", file=sys.stderr)
            sys.exit(1)
        if not api.is_trusted_session:
            api.trust_session()

    if api.requires_2sa:
        print("2SA required.")
        devices = api.trusted_devices
        for i, device in enumerate(devices):
            label = device.get("deviceName") or f"SMS to {device.get('phoneNumber', 'unknown')}"
            print(f"{i}: {label}")
        idx = int(input("Select device index [0]: ").strip() or "0")
        device = devices[idx]
        if not api.send_verification_code(device):
            print("Failed to send verification code", file=sys.stderr)
            sys.exit(1)
        code = input("Verification code: ").strip()
        if not api.validate_verification_code(device, code):
            print("Invalid verification code", file=sys.stderr)
            sys.exit(1)

    if api.requires_2fa or api.requires_2sa:
        print("Authentication incomplete", file=sys.stderr)
        sys.exit(1)

    print("AUTH_OK")
    print(f"Cookie/session data stored under: {cookie_dir}")


if __name__ == "__main__":
    main()
