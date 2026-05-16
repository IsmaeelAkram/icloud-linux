# icloud-linux

Mount iCloud Drive on Linux as a fast local-first FUSE filesystem with persistent caching, selective hydration, and on-demand sync.

## What This Is

`icloud-linux` makes your iCloud Drive show up like a normal folder on Linux.

It is designed to feel much more local than a naive network mount:

- folders and filenames are cached on disk
- file contents are downloaded into a local mirror on demand or in the background
- reads usually come from local storage, not from iCloud on every access
- local changes are written immediately and synced back on the next sync pass
- remote changes are pulled in by the sync engine on demand or on a timer

In practice, that means `find`, editors, shells, and normal file browsing work against a persistent local cache instead of blocking on iCloud for every operation.

## How It Works

There are three main pieces:

- Metadata crawl: the first run scans your iCloud Drive and builds a local index.
- Hydration: file contents are downloaded into the local cache — either on demand when a file is opened (lazy mode) or proactively in the background (background mode).
- Sync engine: local edits upload and remote changes are refreshed either automatically on a timer or on demand via `icloudctl sync`.

Important behavior:

- The mount is local-first.
- Restarts reuse the existing cache instead of starting from zero.
- If you open a file before it has finished hydrating, that file is downloaded first and then served locally.
- Failed warmup downloads are retried automatically with backoff.
- If a path changed both locally and remotely, the local version is preserved as a conflict copy instead of being silently overwritten.
- With `auto_sync: false`, the driver starts and mounts immediately without spawning background polling threads. Run `icloudctl sync` when you want a fresh pull from iCloud.

## Who This Is For

This project is for people who want:

- a normal folder they can browse on Linux
- Apple ID + 2FA support
- a persistent local cache
- control over which folders are hydrated vs stub-only
- on-demand syncing instead of constant background polling

If you want a quick setup and do not care about the internal details, use `./icloudctl quickstart`.

## Requirements

You need:

- Linux
- Python 3 with `venv`
- FUSE
- `systemctl --user`

### Debian / Ubuntu

```bash
sudo apt-get update
sudo apt-get install -y fuse libfuse-dev pkg-config python3-venv
```

### Fedora

```bash
sudo dnf install python3-devel fuse fuse-libs fuse-devel gcc make
```

## Fast Setup

```bash
git clone https://github.com/mslaughter21228/icloud-linux.git
cd icloud-linux
./icloudctl quickstart ~/iCloud
```

This will:

1. create the Python virtual environment
2. install dependencies
3. create the config and user service
4. ask for your Apple ID email and password
5. run the one-time interactive authentication flow
6. start the background service

After setup, your files will be mounted at `~/iCloud` unless you chose another path.

## Simple Setup, Step By Step

If you prefer to do setup one step at a time:

```bash
./icloudctl init ~/iCloud
./icloudctl configure
./icloudctl auth
./icloudctl start
```

## Why Authentication Is Split Into Two Steps

Systemd user services are non-interactive. They cannot pause and wait for a 2FA code.

So this project uses:

- `./icloudctl auth` for the interactive one-time Apple login and 2FA flow
- a generated user service that reuses the saved session cookies in the background

If Apple expires your session, run:

```bash
./icloudctl auth
./icloudctl restart
```

### iOS 26 Beta 2FA Workaround

iOS 26 beta may deliver a push notification popup instead of a numeric 2FA code. If this happens, use the `--force-sms` flag to bypass the push path and request an SMS code directly:

```bash
./icloudctl auth --force-sms
```

Use `--debug` to see Apple's reported auth mode and diagnose delivery issues:

```bash
./icloudctl auth --debug
```

## Everyday Commands

```bash
./icloudctl start
./icloudctl stop
./icloudctl restart
./icloudctl status
./icloudctl logs
./icloudctl doctor
./icloudctl hydrate [--dry-run] [--verbose]
./icloudctl sync [--timeout SECONDS] [--quiet]
./icloudctl clear-cache
./icloudctl uninstall
```

What they do:

- `start`: starts the background user service
- `stop`: stops the service and unmounts the folder
- `restart`: restarts the service cleanly
- `status`: shows whether the service is running
- `logs`: tails the service logs
- `doctor`: checks common setup issues
- `hydrate`: blocks until all eligible files (per `sync_paths` / `exclude_paths`) are fully downloaded locally. Use this before copying files to ensure nothing triggers a mid-copy download. `--dry-run` shows what would be downloaded without actually downloading.
- `sync`: triggers an on-demand remote metadata sync in the running driver (sends SIGUSR1, waits for completion). Useful when `auto_sync: false` is set.
- `clear-cache`: deletes the local mirror and sync database, then rebuilds them on next start
- `uninstall`: removes the generated user service

## What Happens After You Start It

On the first run:

- the service crawls your iCloud Drive metadata
- it mounts the folder
- if `warmup_mode: background`, it starts downloading file contents into the local cache
- if `warmup_mode: lazy`, file contents download only when each file is first opened

On later runs:

- it reuses the cache stored on disk
- if `auto_sync: true`, it refreshes remote metadata and uploads local changes on a timer
- if `auto_sync: false`, it mounts immediately with no background polling; run `icloudctl sync` on demand

## Controlling What Gets Hydrated

By default all of iCloud Drive is indexed (stubs created everywhere), but you can restrict which paths have their file contents downloaded.

**`sync_paths`** — allow-list. Only paths in this list will have file contents downloaded. Everything else is stubs only.

**`exclude_paths`** — deny-list. Paths matching these prefixes are never hydrated, even if they fall under a `sync_path`. The deny-list is evaluated first.

Example config:

```yaml
# Only hydrate /Downloads
sync_paths:
  - /Downloads

# But skip this large subfolder for now
exclude_paths:
  - /Downloads/Michael Priority
```

With this config:
- `/Downloads/*` → file contents downloaded
- `/Downloads/Michael Priority/*` → stubs only, no download
- Everything else on iCloud → stubs only

When you're ready to hydrate an excluded folder, remove it from `exclude_paths` and restart the service.

## Auto-Sync vs On-Demand Sync

**`auto_sync: true`** (default) — background threads poll iCloud on the configured `upload_interval_seconds` and `remote_refresh_interval_seconds` schedules. Good for setups where files change frequently.

**`auto_sync: false`** — no polling threads start. The driver mounts and waits. Use `icloudctl sync` to pull the latest remote changes when you need them. Good for low-churn libraries where constant polling is wasteful and you want predictable resource usage.

When `auto_sync: false`, the recommended workflow is:

```bash
icloudctl sync          # pull latest metadata from iCloud
icloudctl hydrate       # download file contents for all eligible paths
# now copy from mirror: ~/.cache/icloud-linux/mirror/Downloads/
```

## Copying Files to Another Location

Once files are hydrated, copy from the local mirror rather than from the FUSE mount:

```bash
cp -r ~/.cache/icloud-linux/mirror/Downloads/ ~/Desktop/intake/
```

The mirror is a plain directory on disk — reads are instant, no network involved. Copying from the FUSE mount works too but may trigger downloads for any files not yet hydrated.

## Local Cache And Sync State

The project keeps its local state here:

- Config: `~/.config/icloud-linux/config.yaml`
- Session cookies: `~/.config/icloud-linux/cookies`
- Service env: `~/.config/icloud-linux/icloud.env`
- User service: `~/.config/systemd/user/icloud.service`
- Local cache root: `~/.cache/icloud-linux`
- Local mirror: `~/.cache/icloud-linux/mirror`
- Sync state database: `~/.cache/icloud-linux/state.sqlite3`
- Logs: `~/.local/state/icloud-linux/icloud.log`
- On-demand sync marker: `~/.local/state/icloud-linux/sync_done`

## What "Sync Engine" Means Here

This repo is not just a read-only mount and it is not only a foreground downloader.

The sync engine:

- tracks local dirty files and directories
- uploads local changes (when `auto_sync: true` or on `icloudctl sync`)
- refreshes remote metadata (when `auto_sync: true` or on `icloudctl sync`)
- hydrates missing file contents on demand or in the background
- preserves local conflict copies when local and remote diverge

That makes it closer to a real cached sync client than a simple network filesystem wrapper.

## Troubleshooting

### The service will not start

Run:

```bash
./icloudctl status
./icloudctl doctor
./icloudctl logs
```

### Authentication expired

Run:

```bash
./icloudctl auth
./icloudctl restart
```

### Service starts but auth fails silently (unauthenticated mode)

When running under systemd (no TTY), a failed auth parks the service in unauthenticated mode instead of crashing — this prevents the service from crash-looping and triggering Apple's account lockout. You will see `UNAUTHENTICATED mode` in the logs. Fix by running:

```bash
./icloudctl auth
./icloudctl restart
```

### I want to rebuild everything locally

Run:

```bash
./icloudctl clear-cache
```

That removes the local mirror and sync database. The next start will rebuild them from iCloud.

### I want to confirm it is using the local cache

After the service has had time to hydrate files:

```bash
find ~/iCloud -type f | head
```

You can watch logs in another terminal:

```bash
./icloudctl logs
```

Normal activity with `auto_sync: false` will show the reconcile pass at startup and then go quiet until you run `icloudctl sync`.

### I want to check hydration progress

```bash
./icloudctl hydrate --dry-run
```

This reports how many files are already local vs need downloading, without downloading anything.

## Notes

- Warmup downloads are intentionally conservative because iCloud file downloads are sensitive to aggressive parallelism.
- The generated systemd unit is created by `./icloudctl`; the repo does not rely on checked-in service files anymore.
- This project currently targets a user-level systemd service, not a system-wide root service.
- When `auto_sync: false`, the SIGUSR1 signal triggers a one-shot sync in the background; `icloudctl sync` handles sending that signal and waiting for completion.
