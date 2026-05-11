#!/usr/bin/env python3
"""
fix_filenames.py — sanitize filenames in the icloud-linux mirror cache
for safe copying to CIFS/NAS destinations.

Fixes:
  - Backslash octal escapes (e.g. \374 -> ü)
  - Backslash-escaped characters (e.g. \_ -> _)
  - Bare backslashes (removed)
  - Leading/trailing spaces per path component
  - Characters illegal on CIFS/Windows: < > : " | ? *
    (replaced with a visually similar safe character or removed)

Updates the SQLite state DB so the icloud-linux driver stays in sync.

Usage:
    .venv/bin/python fix_filenames.py [/path/to/mirror/subdir]

    Defaults to the full Downloads mirror if no path given.

Options:
    --dry-run   Print what would be renamed without doing anything
"""

import os, re, sys, sqlite3, argparse

MIRROR_ROOT = os.path.expanduser('~/.cache/icloud-linux/mirror')
DB_PATH     = os.path.expanduser('~/.cache/icloud-linux/state.sqlite3')

# Characters illegal on CIFS (Windows) filesystems
CIFS_ILLEGAL = r'<>:"|?*'
CIFS_REPLACE = {
    '<':  '(',
    '>':  ')',
    ':':  '-',
    '"':  "'",
    '|':  '-',
    '?':  '',
    '*':  '',
}

def clean_name(name):
    """Return a CIFS-safe version of a filename component."""
    # 1. Backslash octal escapes e.g. \374 -> ü
    def replace_octal(m):
        try:
            return bytes([int(m.group(1), 8)]).decode('latin-1')
        except Exception:
            return m.group(0)
    name = re.sub(r'\\([0-7]{3})', replace_octal, name)

    # 2. Backslash + specific char e.g. \_ -> _
    name = re.sub(r'\\(.)', r'\1', name)

    # 3. CIFS-illegal characters
    for ch, repl in CIFS_REPLACE.items():
        name = name.replace(ch, repl)

    # 4. Strip leading/trailing spaces and dots (illegal on Windows)
    name = name.strip(' .')

    # 5. Collapse multiple spaces
    name = re.sub(r'  +', ' ', name)

    return name

def mirror_to_icloud_path(disk_path):
    """Convert an absolute mirror path to the iCloud-relative path stored in DB."""
    rel = disk_path[len(MIRROR_ROOT):]
    return rel if rel else '/'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?',
                        default=os.path.join(MIRROR_ROOT, 'Downloads'),
                        help='Directory to scan (default: mirror/Downloads)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print renames without executing them')
    args = parser.parse_args()

    scan_root = os.path.abspath(args.path)
    if not os.path.isdir(scan_root):
        print(f'ERROR: not a directory: {scan_root}')
        sys.exit(1)

    print(f'Scanning: {scan_root}')
    print(f'Dry run:  {args.dry_run}')
    print()

    db  = sqlite3.connect(DB_PATH)
    cur = db.cursor()

    renamed = skipped = errors = 0

    for dirpath, dirnames, filenames in os.walk(scan_root, topdown=False):
        for fname in filenames:
            new_fname = clean_name(fname)
            if new_fname == fname:
                continue

            old_path = os.path.join(dirpath, fname)
            new_path = os.path.join(dirpath, new_fname)

            old_db = mirror_to_icloud_path(old_path)
            new_db = mirror_to_icloud_path(new_path)

            print(f'  OLD: {fname}')
            print(f'  NEW: {new_fname}')

            if args.dry_run:
                print('  (dry run - skipped)')
                skipped += 1
            else:
                try:
                    os.rename(old_path, new_path)
                    rows = cur.execute(
                        'UPDATE entries SET path=?, parent_path=? WHERE path=?',
                        (new_db, os.path.dirname(new_db), old_db)
                    ).rowcount
                    db.commit()
                    print(f'  -> OK  (DB rows: {rows})')
                    renamed += 1
                except Exception as e:
                    print(f'  -> ERROR: {e}')
                    errors += 1
            print()

        # Also rename directories themselves
        for dname in dirnames:
            new_dname = clean_name(dname)
            if new_dname == dname:
                continue
            old_dpath = os.path.join(dirpath, dname)
            new_dpath = os.path.join(dirpath, new_dname)
            old_db = mirror_to_icloud_path(old_dpath)
            new_db = mirror_to_icloud_path(new_dpath)
            print(f'  DIR OLD: {dname}')
            print(f'  DIR NEW: {new_dname}')
            if not args.dry_run:
                try:
                    os.rename(old_dpath, new_dpath)
                    # Update all entries under this dir
                    cur.execute(
                        "UPDATE entries SET path = replace(path, ?, ?), "
                        "parent_path = replace(parent_path, ?, ?) "
                        "WHERE path LIKE ? OR path = ?",
                        (old_db + '/', new_db + '/',
                         old_db + '/', new_db + '/',
                         old_db + '/%', old_db)
                    )
                    db.commit()
                    print(f'  -> DIR OK')
                    renamed += 1
                except Exception as e:
                    print(f'  -> DIR ERROR: {e}')
                    errors += 1
            else:
                skipped += 1
            print()

    db.close()

    if args.dry_run:
        print(f'Dry run complete: {skipped} would be renamed')
    else:
        if renamed == 0 and errors == 0:
            print('No problematic filenames found.')
        else:
            print(f'Done: {renamed} renamed, {errors} errors')

if __name__ == '__main__':
    main()
