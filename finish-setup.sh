#!/usr/bin/env bash
# finish-setup.sh — Run this in YOUR OWN terminal to complete icloud-linux setup.
# This script requires a D-Bus session (i.e., a normal desktop terminal).

set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "======================================"
echo " icloud-linux — Final Setup Steps"
echo "======================================"
echo ""
echo "Step 1: Enter your Apple ID credentials"
bash "$REPO_DIR/icloudctl" configure
echo ""

echo "Step 2: Authenticate (2FA — you'll get a code on your Apple device)"
bash "$REPO_DIR/icloudctl" auth
echo ""

echo "Step 3: Register and start the background service"
systemctl --user daemon-reload
systemctl --user enable icloud.service
systemctl --user start icloud.service
echo ""

echo "Step 4: Check status"
systemctl --user --no-pager status icloud.service
echo ""

echo "======================================"
echo " Done! iCloud Drive will be mounted at:"
echo "   ~/iCloud"
echo ""
echo " NOTE: warmup_mode is set to 'lazy' — no files are downloaded"
echo " until you explicitly access them. Your 100GB+ iCloud Drive"
echo " will appear as a full folder tree, but only files you touch"
echo " will use local disk space."
echo ""
echo " To copy ONLY your Downloads folder to /mnt/downloads:"
echo ""
echo "   # Wait ~5 min after start for the metadata index to build, then:"
echo "   rsync -av --progress ~/iCloud/Downloads/ /mnt/downloads/"
echo ""
echo " To copy a single file:"
echo "   cp ~/iCloud/Downloads/somefile.pdf /mnt/downloads/"
echo ""
echo " To check how much local disk the cache is using:"
echo "   du -sh ~/.cache/icloud-linux/"
echo ""
echo " To clear the local cache (frees disk, re-downloads on next access):"
echo "   cd ~/projects/icloud-linux && ./icloudctl clear-cache"
echo "======================================"
