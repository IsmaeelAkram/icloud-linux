# icloud-linux

Mount iCloud Drive on Linux using FUSE.

## TL;DR (fast setup)

```bash
git clone https://github.com/ismaeelakram/icloud-linux.git
cd icloud-linux
./icloudctl quickstart ~/iCloud
```

`quickstart` will:
1. create a virtualenv and install Python deps
2. initialize user config/service files
3. prompt for Apple ID credentials
4. run one-time interactive 2FA auth
5. start the user service

## Why this works with 2FA

Systemd services are non-interactive, so they cannot wait for 2FA input.

This project uses a **two-phase auth flow**:
- `./icloudctl auth` (interactive, one-time) stores trusted session cookies
- `icloud.service` (background) reuses saved cookies and runs non-interactively

If cookies expire, just run `./icloudctl auth` again.

## Commands

```bash
./icloudctl init [mount_dir]      # prepare venv/config/service
./icloudctl configure [email]      # write config.yaml interactively
./icloudctl auth                   # one-time 2FA bootstrap
./icloudctl start|stop|restart
./icloudctl status
./icloudctl logs
./icloudctl doctor
```

## Files and paths

- Config: `~/.config/icloud-linux/config.yaml`
- Service env: `~/.config/icloud-linux/icloud.env`
- Session cookies: `~/.config/icloud-linux/cookies`
- Cache: `~/.cache/icloud-linux`
- Logs: `~/.local/state/icloud-linux/icloud.log`
- Systemd user service: `~/.config/systemd/user/icloud.service`

## Debian/Ubuntu prerequisites

You need FUSE + Python tooling installed once:

```bash
sudo apt-get update
sudo apt-get install -y fuse libfuse-dev pkg-config python3-venv
```

## Troubleshooting

### Service won’t start

```bash
./icloudctl status
./icloudctl logs
```

### Auth/2FA errors

Run again:

```bash
./icloudctl auth
./icloudctl restart
```

### Check setup health

```bash
./icloudctl doctor
```
