# NixOS Installation Guide

> This guide covers installing `icloud-linux` on NixOS, including headless/server setups with no GUI. It was tested on NixOS 25.11 with Python 3.13 and fuse-2.9.9.

## Key Differences from Debian

NixOS is fundamentally different from Debian-based systems in ways that affect every step of this installation. Understanding these differences upfront is crucial.

| Concern | Debian | NixOS |
|---|---|---|
| Install FUSE | `apt install fuse libfuse-dev` | Declarative `configuration.nix` option |
| `fusermount` setuid | Handled by apt | Requires `programs.fuse.enable = true` |
| Python `venv` + `pip` | Just works | Needs `nix-ld` + explicit `CFLAGS`/`LDFLAGS` |
| `fuse-python` build | Builds from sdist automatically | Must force source build; pip prefers a broken pre-built wheel |
| `systemctl --user` on headless | Works after login | Requires lingering + `machinectl shell` to access |
| Systemd user services at boot | Standard | Requires `linger = true` in NixOS config |

## Step 1 — System Configuration

Add the following to your `/etc/nixos/configuration.nix`. Replace `<youruser>` with your actual
username.

```nix
{ config, pkgs, lib, ... }:
{
  # Enables FUSE, sets up fusermount/fusermount3 as setuid wrappers,
  # and writes /etc/fuse.conf.
  programs.fuse = {
    enable = true;
    userAllowOther = true;
  };

  users.users.<youruser> = {
    isNormalUser = true;
    # Spawns the systemd user manager at boot even when the user is not logged in:
    linger = true;
  };

  # Allows dynamically-linked binaries (e.g. pip-installed C extensions)
  # to find system libraries.
  programs.nix-ld = {
    enable = true;
    libraries = with pkgs; [
      zlib
      zstd
      stdenv.cc.cc
      openssl
      libffi
      fuse
    ];
  };

  environment.systemPackages = with pkgs; [
    git
    python3
    gcc        # C compiler for native extensions
    pkg-config # Needed by fuse-python's setup.py
    fuse       # libfuse runtime library
    fuse.dev   # fuse.h headers + fuse.pc file
    # A Python wrapper that exports the nix-ld library path into the
    # venv so pip-installed packages with native extensions work.
    (pkgs.writeShellScriptBin "pythonld" ''
      export LD_LIBRARY_PATH=$NIX_LD_LIBRARY_PATH
      exec -a "$0" ${pkgs.python3}/bin/python "$@"
    '')
  ];
}
```

Apply the configuration:

```bash
sudo nixos-rebuild switch
```

## Step 2 — Open a Proper User Session

> **Do not use `su - youruser -c "..."`**. It does not go through PAM's `pam_systemd`, so `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` are never set and `systemctl --user` will fail with `Failed to connect to bus`.

The correct tool is `machinectl shell`, which creates a full PAM login session:

```bash
# As root, open a proper login shell for your user
machinectl shell youruser@

# Verify the session environment is set up correctly
echo $XDG_RUNTIME_DIR          # Should be: /run/user/1000 (or your UID)
echo $DBUS_SESSION_BUS_ADDRESS  # Should be: unix:path=/run/user/1000/bus
systemctl --user status        # Should show the user manager running
```

All subsequent steps should be run inside this session, or over a direct SSH session as your
user (SSH also creates a proper PAM login session).

## Step 3 — Clone the Repository

```bash
git clone https://github.com/IsmaeelAkram/icloud-linux.git ~/icloud-linux
cd ~/icloud-linux
```

## Step 4 — Create the Python Virtual Environment

Use the `pythonld` wrapper declared in `configuration.nix` instead of bare `python3`. This ensures pip-installed native C extensions can find system libraries via `nix-ld`.

```bash
pythonld -m venv .venv
.venv/bin/python --version  # Verify the venv is working
```

## Step 5 — Install Python Dependencies

`fuse-python` is a C extension that compiles against `libfuse`. On NixOS this requires several extra steps compared to a standard Linux distribution.

### 5a — Install build prerequisites into the venv

`--no-build-isolation` (used later) requires `setuptools` and `wheel` to already be present in the venv. NixOS's Python omits them by default:

```bash
.venv/bin/pip install setuptools wheel
```

### 5b — Find the fuse header and library paths

NixOS stores libraries in `/nix/store` rather than `/usr`. We need to locate the exact paths:

```bash
# Find the fuse.h header (look for the -dev entry)
find /nix/store -name "fuse.h" 2>/dev/null
# Example output:
# /nix/store/743aqrp5v9i9bnb8m1n5a8qgiz7x2kyq-fuse-2.9.9-dev/include/fuse.h

# Find libfuse.so (exclude the -dev entry)
find /nix/store -name "libfuse.so*" -not -path "*/dev/*" -not -path "*/nix-ld/*" 2>/dev/null
# Example output:
# /nix/store/wigrqszr23p12g7x240h9kqmxjgfhcjh-fuse-2.9.9/lib/libfuse.so
```

Set variables from the output above (your hashes will differ):

```bash
FUSE_DEV="/nix/store/<hash>-fuse-2.9.9-dev"
FUSE_LIB="/nix/store/<hash>-fuse-2.9.9/lib"
```

### 5c — Build fuse-python from source

> **Do not let pip use its cached wheel.** The pre-built `fuse_python-*.whl` on PyPI is linked against a `libfuse` that does not have `fuse_teardown` properly exported, which causes an `ImportError: undefined symbol: fuse_teardown` at runtime. You must force a source build.

First, purge the cached wheel so pip cannot fall back to it:

```bash
.venv/bin/pip cache purge
```

Then download the [source tarball manually](https://pypi.org/project/fuse-python/#files) and build it with the correct flags:

```bash
mkdir -p ~/icloud-linux/fuse-python-src && cd ~/icloud-linux/fuse-python-src
curl -L "https://files.pythonhosted.org/packages/8f/41/1e372623fc863df2199f329d3548ef14376a9c1c7024743483547f166e5b/fuse_python-1.0.9.tar.gz" \
  -o fuse-python-1.0.9.tar.gz
tar -xzf fuse-python-1.0.9.tar.gz
cd fuse-python-1.0.9

CFLAGS="-I$FUSE_DEV/include" \
LDFLAGS="-L$FUSE_LIB -lfuse -Wl,-rpath,$FUSE_LIB" \
LD_LIBRARY_PATH="$FUSE_LIB:$NIX_LD_LIBRARY_PATH" \
  ~/icloud-linux/.venv/bin/pip install . --no-build-isolation
```

The key compiler flags are:
- `CFLAGS="-I$FUSE_DEV/include"` — tells `gcc` where to find `fuse.h`
- `LDFLAGS="-L$FUSE_LIB -lfuse"` — tells the linker to link against `libfuse.so`
- `LDFLAGS="-Wl,-rpath,$FUSE_LIB"` — bakes the Nix store path into the `.so` so it finds `libfuse` at runtime without needing `LD_LIBRARY_PATH`
- `--no-build-isolation` — prevents pip from spawning a clean subprocess that would strip all the env vars above

### 5d — Install remaining dependencies

```bash
cd ~/icloud-linux

FUSE_DEV="/nix/store/<hash>-fuse-2.9.9-dev"
FUSE_LIB="/nix/store/<hash>-fuse-2.9.9/lib"

CFLAGS="-I$FUSE_DEV/include" \
LDFLAGS="-L$FUSE_LIB -lfuse -Wl,-rpath,$FUSE_LIB" \
LD_LIBRARY_PATH="$FUSE_LIB:$NIX_LD_LIBRARY_PATH" \
  .venv/bin/pip install -r requirements.txt --no-build-isolation
```

### 5e — Verify

```bash
.venv/bin/python -c "import fuse; print('OK — fuse-python version:', fuse.__version__)"
# Expected: OK — fuse-python version: 1.0.x

ldd .venv/lib/python3.*/site-packages/fuseparts/_fuse*.so | grep fuse
# Expected: libfuse.so.2 => /nix/store/...-fuse-2.9.9/lib/libfuse.so.2
```

## Step 6 — Fix the Service File for NixOS

The generated systemd service file hardcodes `/usr/bin/fusermount`, which does not exist on NixOS. The setuid-wrapped binary lives at `/run/wrappers/bin/fusermount`.

```bash
cd ~/icloud-linux
./icloudctl init ~/iCloud

# Patch the generated service file
sed -i 's|/usr/bin/fusermount|/run/wrappers/bin/fusermount|g' \
  ~/.config/systemd/user/icloud.service

systemctl --user daemon-reload

# Verify the fix
grep fusermount ~/.config/systemd/user/icloud.service
# Should show: /run/wrappers/bin/fusermount
```

## Step 7 — Configure and Authenticate

```bash
cd ~/icloud-linux
./icloudctl configure
./icloudctl auth

# If you receive a notification popup instead of a numeric code, use:
./icloudctl auth --force-sms
```

> The `auth` step **must be run interactively** in a live TTY or SSH session. The systemd service cannot handle 2FA prompts. A session cookie will be saved to `~/.config/icloud-linux/cookies` and reused automatically by the background service.

## Step 8 — Start the Service

```bash
./icloudctl start
./icloudctl status
./icloudctl doctor
./icloudctl logs
```

> **Note:** On the very first start you will see this warning in the logs:
> ```
> fusermount: entry for /home/youruser/iCloud not found in /etc/mtab
> ```
> This is harmless, it comes from the `ExecStartPre` cleanup step trying to unmount a stale mount that doesn't exist yet on first run. The service starts correctly regardless, and the warning disappears on all subsequent restarts.

For the next steps refer to the [primary installation guide](README.md).

## Troubleshooting

### `systemctl --user` fails with `Failed to connect to bus`

You are using `su -c` instead of `machinectl shell`. See [Step 2](#step-2--open-a-proper-user-session).

### `fuse.h: No such file or directory` during pip install

`fuse.dev` is not in scope for the build. Make sure `fuse.dev` is in `environment.systemPackages` in your `configuration.nix` and that you have run `sudo nixos-rebuild switch`.

### `ImportError: undefined symbol: fuse_teardown`

pip installed the pre-built wheel instead of compiling from source. Purge the pip cache with `.venv/bin/pip cache purge` and follow [Step 5c](#5c--build-fuse-python-from-source) to force a source build.

### `BackendUnavailable: Cannot import 'setuptools.build_meta'`

`setuptools` is not installed in the venv. Run `.venv/bin/pip install setuptools wheel` before retrying. Double-check [Step 5a](#5a--install-build-prerequisites-into-the-venv).

### `Linger=no` — user session not starting at boot

Either `linger = true` was not applied in `configuration.nix`, or the rebuild hasn't been run yet. Force it immediately without a rebuild:

```bash
sudo loginctl enable-linger <youruser>
loginctl show-user <youruser> | grep Linger  # Should show: Linger=yes
```
