# icloud-linux

Mount iCloud Drive on Linux with a local-first FUSE filesystem, persistent disk cache, and a real background sync engine.

`icloud-linux` is built to feel fast under normal filesystem workloads:
- directory walks, `find`, `rg`, editors, and shell tools run against a local mirror instead of waiting on iCloud for every operation
- file contents are cached on disk under `~/.cache/icloud-linux/mirror`
- local writes complete immediately and sync back to iCloud asynchronously
- remote changes are pulled in by a periodic refresh loop instead of blocking foreground reads

## Why it feels fast

- **Persistent local mirror**: metadata and file contents are cached on disk, not only in memory.
- **Local-first reads**: once metadata is known, normal filesystem operations are served from the mirror.
- **Persistent restarts**: after the first initialization crawl, restarts reuse the existing mirror and sync state instead of rebuilding cache from scratch.
- **Background warmup**: the first initialization does a metadata sync before mount, then hydrates file contents in the background.
- **Real sync engine**: uploads and remote refreshes happen on timers, decoupled from `open`, `read`, `write`, and `readdir`.
- **Retrying hydrator**: transient iCloud download failures stay queued and are retried with backoff until they succeed.
- **Conflict preservation**: if local and remote both changed, the local version is kept as a conflict copy instead of being silently lost.

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

## How the filesystem behaves now

- The mount is **local-first**. On first initialization, a metadata crawl runs before mount. After that, restarts reuse the persistent disk mirror in `~/.cache/icloud-linux/mirror` and refresh remote metadata in the background.
- File contents warm in the background by default. If you open a file before it has been hydrated, that one file is downloaded first and then served locally.
- Local writes are committed to the mirror immediately and uploaded asynchronously every `30s` by default.
- Remote metadata is refreshed every `300s` by default.
- File downloads that fail during warmup are retried automatically with backoff until they hydrate successfully.
- If the same path changed locally and remotely before sync, the local version is preserved as `*.local-conflict-<timestamp>`.

## Commands

```bash
./icloudctl init [mount_dir]      # prepare venv/config/service
./icloudctl configure [email]      # write config.yaml interactively
./icloudctl auth                   # one-time 2FA bootstrap
./icloudctl clear-cache            # remove local mirror/state and rebuild it
./icloudctl start|stop|restart
./icloudctl status
./icloudctl logs
./icloudctl doctor
./icloudctl uninstall [--purge]
```

## Files and paths

- Config: `~/.config/icloud-linux/config.yaml`
- Service env: `~/.config/icloud-linux/icloud.env`
- Session cookies: `~/.config/icloud-linux/cookies`
- Cache root: `~/.cache/icloud-linux`
- Local mirror: `~/.cache/icloud-linux/mirror`
- Sync state DB: `~/.cache/icloud-linux/state.sqlite3`
- Logs: `~/.local/state/icloud-linux/icloud.log`
- Systemd user service: `~/.config/systemd/user/icloud.service`

## Debian/Ubuntu prerequisites

You need FUSE + Python tooling installed once:

```bash
sudo apt-get update
sudo apt-get install -y fuse libfuse-dev pkg-config python3-venv
```

## Fedora prerequisites

```bash
sudo dnf install python3-devel fuse fuse-libs fuse-devel gcc make
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

### Verify local-first behavior

After the service has had time to warm the cache, a large traversal should run without triggering foreground iCloud fetches:

```bash
find ~/iCloud -type f | head
rg --files ~/iCloud | head
```

Use `./icloudctl logs` in another terminal and confirm activity is mostly background refresh/upload logging rather than a remote fetch per file operation.

### Rebuild the persistent cache

If you want to throw away the local mirror and sync DB without reinstalling the service:

```bash
./icloudctl clear-cache
```
