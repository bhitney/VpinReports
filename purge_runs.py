"""
Applies purge manifests exported from the report (purge_<timestamp>.json) to the
PinUP Popper database by soft-deleting the listed CustomPerfStats rows
(Deleted = 1, DeletedUtc = now). Nothing is physically removed; reports skip the rows.
Needs migration 008_add_soft_delete.sql (VPinPerfMon) to have been run.

It is a dry run unless --apply is given.

Usage:
    python purge_runs.py [PATH ...] [options]

PATH is a manifest file, a folder (its purge*.json files), or a wildcard pattern such as
    "p:\\somefolder\\somefiles\\purge*.json"
Quote patterns so the shell does not expand them. With no PATH, purge*.json in the
current folder is used.

Options:
    --db FILE             database to update (default: DB_PATH from report_config.py)
    --apply               make the changes (default: only show what would happen)
    --backup [DIR]        before applying, copy the database to <name>.<timestamp>.bak in DIR
                          (default: next to the database). Off unless given.
    --delete-manifests    after --apply, delete each manifest whose entries were all handled
                          (purged, already purged, or already gone). Manifests with mismatches
                          or errors are kept.

Each entry is checked against its row before anything changes: the Id must exist, and
entryDateTime and fileAssetStem must match the row. Anything that does not match is skipped
and reported, never purged. Running it twice is safe.

Exit code: 0 = nothing wrong, 1 = some entries or files were skipped, 2 = could not run.
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

MANIFEST_PATTERN = "purge*.json"


def find_manifests(paths):
    """Expand files, folders and wildcard patterns into a sorted, de-duplicated list of files."""
    found = {}
    for raw in paths or ["."]:
        if os.path.isdir(raw):
            hits = glob.glob(os.path.join(glob.escape(raw), MANIFEST_PATTERN))
        elif glob.has_magic(raw):
            hits = [p for p in glob.glob(raw, recursive=True) if os.path.isfile(p)]
        elif os.path.isfile(raw):
            hits = [raw]
        else:
            print(f"Not found: {raw}")
            hits = []
        for p in hits:
            found[os.path.normcase(os.path.abspath(p))] = os.path.abspath(p)
    return sorted(found.values(), key=lambda p: (os.path.basename(p).lower(), p.lower()))


def load_manifest(path):
    """Returns (entries, error)."""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"cannot read: {e}"
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return None, "no 'entries' list"
    if doc.get("version", 1) != 1:
        return None, f"unsupported version {doc.get('version')}"
    return entries, None


def check_entry(row, entry):
    """Compare a manifest entry with its database row. Returns (status, detail)."""
    if row is None:
        return "missing", "no row with this Id"
    db_when, db_stem, deleted = row
    if str(entry.get("entryDateTime") or "").strip() != str(db_when or "").strip():
        return "mismatch", f"entryDateTime {entry.get('entryDateTime')!r} != database {db_when!r}"
    want_stem = entry.get("fileAssetStem")
    if want_stem and db_stem and want_stem != db_stem:
        return "mismatch", f"fileAssetStem {want_stem!r} != database {db_stem!r}"
    if deleted:
        return "already", "already purged"
    return "purge", ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Soft-delete runs listed in purge manifests (dry run unless --apply).")
    ap.add_argument("paths", nargs="*", help="manifest file, folder, or wildcard (default: purge*.json here)")
    ap.add_argument("--db", help="database to update (default: DB_PATH in report_config.py)")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--backup", nargs="?", const="", default=None, metavar="DIR",
                    help="copy the database before applying (default folder: next to the database)")
    ap.add_argument("--delete-manifests", action="store_true",
                    help="after --apply, delete manifests whose entries were all handled")
    args = ap.parse_args(argv)

    if (args.backup is not None or args.delete_manifests) and not args.apply:
        print("Note: --backup and --delete-manifests only act with --apply.")

    db_path = args.db
    if not db_path:
        try:
            import report_config
            db_path = report_config.DB_PATH
        except Exception:
            print("No --db given and DB_PATH could not be read from report_config.py.")
            return 2
    if not os.path.isfile(db_path):
        print(f"Database not found: {db_path}")
        return 2

    manifests = find_manifests(args.paths)
    if not manifests:
        print("No purge manifests found.")
        return 0

    mode = "APPLY" if args.apply else "DRY RUN (add --apply to make changes)"
    print(f"{mode}\nDatabase: {db_path}\n")

    con = sqlite3.connect(db_path)
    exit_code = 0
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(CustomPerfStats)")}
        if "Deleted" not in cols or "DeletedUtc" not in cols:
            print("CustomPerfStats has no Deleted / DeletedUtc columns. Run VPinPerfMon migration 008_add_soft_delete.sql first.")
            return 2
        has_stem = "FileAssetStem" in cols
        stem_sql = "FileAssetStem" if has_stem else "NULL"

        plan = []   # (manifest, [ids to purge], clean)
        for path in manifests:
            entries, err = load_manifest(path)
            print(path)
            if err:
                print(f"  SKIPPED: {err}")
                exit_code = 1
                plan.append((path, [], False))
                continue
            ids, clean = [], True
            for e in entries:
                label = f"Id {e.get('id')} {e.get('table', '')} {e.get('entryDateTime', '')}".strip()
                try:
                    rid = int(e.get("id"))
                except (TypeError, ValueError):
                    print(f"  SKIP     {label}: entry has no usable id")
                    clean = False
                    continue
                row = con.execute(
                    f"SELECT EntryDate, {stem_sql}, Deleted FROM CustomPerfStats WHERE Id = ?", (rid,)).fetchone()
                status, detail = check_entry(row, e)
                if status == "purge":
                    if rid not in ids:
                        ids.append(rid)
                    print(f"  PURGE    {label}")
                elif status == "already":
                    print(f"  already  {label}")
                elif status == "missing":
                    print(f"  gone     {label}: {detail}")
                else:
                    print(f"  SKIP     {label}: {detail}")
                    clean = False
            if not clean:
                exit_code = 1
            plan.append((path, ids, clean))

        total = len({i for _, ids, _ in plan for i in ids})
        print(f"\n{total} run(s) to purge from {len(manifests)} manifest(s).")

        if not args.apply:
            return exit_code

        if total and args.backup is not None:
            folder = args.backup or os.path.dirname(os.path.abspath(db_path))
            os.makedirs(folder, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(folder, f"{os.path.basename(db_path)}.{stamp}.bak")
            with sqlite3.connect(dest) as bak:
                con.backup(bak)
            bak.close()
            print(f"Backup: {dest}")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        changed = 0
        with con:
            for _, ids, _ in plan:
                for rid in ids:
                    changed += con.execute(
                        "UPDATE CustomPerfStats SET Deleted = 1, DeletedUtc = ? WHERE Id = ? AND Deleted = 0",
                        (now, rid)).rowcount
        print(f"Purged {changed} run(s).")

        if args.delete_manifests:
            for path, _, clean in plan:
                if not clean:
                    print(f"Kept (not fully handled): {path}")
                    continue
                try:
                    os.remove(path)
                    print(f"Deleted manifest: {path}")
                except OSError as e:
                    print(f"Could not delete {path}: {e}")
                    exit_code = 1
        return exit_code
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
