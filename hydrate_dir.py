#!/usr/bin/env python3
"""
Standalone hydration script for icloud-linux.
Downloads all unhydrated files under a given iCloud path prefix
using drive.get_file(docwsid, zone) - bypasses FUSE entirely.

Usage:
    .venv/bin/python hydrate_dir.py [/Downloads/Move2]
"""

import hashlib, logging, os, sqlite3, sys, tempfile, shutil, time
import yaml, requests as _req
from pyicloud import PyiCloudService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hydrate")

CONFIG_PATH = os.path.expanduser("~/.config/icloud-linux/config.yaml")
config      = yaml.safe_load(open(CONFIG_PATH))

CACHE_DIR   = os.path.expanduser(config["cache_dir"])
MIRROR_ROOT = os.path.join(CACHE_DIR, "mirror")
DB_PATH     = os.path.join(CACHE_DIR, "state.sqlite3")
COOKIE_DIR  = os.path.expanduser(config.get("cookie_dir", "~/.config/icloud-linux/cookies"))
USERNAME    = config["username"]
PASSWORD    = config["password"]

TARGET = sys.argv[1] if len(sys.argv) > 1 else "/Downloads/Move2"
if not TARGET.startswith("/"):
    TARGET = "/" + TARGET

log.info("Target: %s", TARGET)

# ── Auth ─────────────────────────────────────────────────────────────────────
log.info("Authenticating as %s ...", USERNAME)
_r = _req.post("https://setup.icloud.com/setup/ws/1/validate", json={})
_partition = _r.headers.get("x-apple-user-partition")
api = PyiCloudService(USERNAME, PASSWORD,
                      cookie_directory=COOKIE_DIR, authenticate=False)
if _partition:
    api._setup_endpoint = f"https://p{_partition}-setup.icloud.com/setup/ws/1"
api.authenticate()
if api.requires_2fa or api.requires_2sa:
    raise RuntimeError("2FA required - run ./icloudctl auth first")
log.info("Auth OK")

# ── Load unhydrated rows ──────────────────────────────────────────────────────
db  = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
cur = db.cursor()

rows = cur.execute("""
    SELECT path, remote_docwsid, remote_zone, size, mtime
    FROM   entries
    WHERE  (path = ? OR path LIKE ?)
      AND  type='file' AND hydrated=0 AND tombstone=0
      AND  remote_docwsid IS NOT NULL
    ORDER  BY size ASC
""", (TARGET, TARGET + "/%")).fetchall()

total = len(rows)
log.info("%d unhydrated files to download", total)
if total == 0:
    log.info("Nothing to do")
    sys.exit(0)

# ── Helpers ───────────────────────────────────────────────────────────────────
def write_atomic(dest, data, mtime=None):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mtime:
            os.utime(tmp, (mtime, mtime))
        shutil.move(tmp, dest)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def mark_hydrated(path, data):
    ck = hashlib.sha256(data).hexdigest()
    cur.execute("UPDATE entries SET hydrated=1, local_sha256=?, size=? WHERE path=?",
                (ck, len(data), path))
    db.commit()

# ── Main loop ─────────────────────────────────────────────────────────────────
ok = failed = skipped = 0
t0 = time.time()

for i, row in enumerate(rows, 1):
    path      = row["path"]
    docwsid   = row["remote_docwsid"]
    zone      = row["remote_zone"] or "com.apple.CloudDocs"
    size      = row["size"] or 0
    mtime     = row["mtime"]
    dest      = os.path.join(MIRROR_ROOT, path.lstrip("/"))

    # Already fully on disk - just mark hydrated
    if os.path.exists(dest) and os.path.getsize(dest) == size and size > 80:
        data = open(dest, "rb").read()
        mark_hydrated(path, data)
        skipped += 1
        if i % 100 == 0 or i == total:
            log.info("[%d/%d] already-ok (skipped dl): %s", i, total, os.path.basename(path))
        continue

    for attempt in range(1, 4):
        try:
            resp    = api.drive.get_file(docwsid, zone=zone, stream=True)
            content = resp.raw.read()
            write_atomic(dest, content, mtime)
            mark_hydrated(path, content)
            ok += 1
            elapsed = time.time() - t0
            rate    = ok / elapsed if elapsed > 0 else 0
            eta     = (total - i) / rate if rate > 0 else 0
            log.info("[%d/%d] OK %s (%d B)  %.1f/min  ETA %ds",
                     i, total, os.path.basename(path), len(content), rate*60, eta)
            break
        except KeyboardInterrupt:
            log.info("Interrupted. ok=%d failed=%d skipped=%d", ok, failed, skipped)
            db.close(); sys.exit(1)
        except Exception as exc:
            log.warning("[%d/%d] attempt %d failed for %s: %s", i, total, attempt, path, exc)
            if attempt < 3:
                time.sleep(3 * attempt)
            else:
                log.error("[%d/%d] GIVING UP: %s", i, total, path)
                failed += 1

db.close()
log.info("Done in %.0fs — downloaded=%d already-ok=%d failed=%d",
         time.time()-t0, ok, skipped, failed)
