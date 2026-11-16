import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import purge_runs  # noqa: E402


class PurgeRunsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.db = os.path.join(self.dir, "test.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE CustomPerfStats (Id INTEGER PRIMARY KEY AUTOINCREMENT, EntryDate TEXT, "
                    "FileAssetStem TEXT, Deleted INTEGER NOT NULL DEFAULT 0, DeletedUtc TEXT)")
        con.executemany("INSERT INTO CustomPerfStats (Id, EntryDate, FileAssetStem) VALUES (?,?,?)", [
            (1, "2026-11-15 21:42:33", "stem one"),
            (2, "2026-10-30 02:50:24", "stem two"),
            (3, "2026-10-01 00:00:00", "stem three"),
        ])
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, name, *entries):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": list(entries)}, f)
        return path

    def entry(self, rid, when, stem):
        return {"id": rid, "table": "T", "entryDateTime": when, "fileAssetStem": stem}

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = purge_runs.main(["--db", self.db, *args])
        return code, out.getvalue()

    def deleted(self):
        con = sqlite3.connect(self.db)
        try:
            return dict(con.execute("SELECT Id, Deleted FROM CustomPerfStats"))
        finally:
            con.close()

    def test_dry_run_changes_nothing(self):
        m = self.manifest("purge_a.json", self.entry(1, "2026-11-15 21:42:33", "stem one"))
        code, out = self.run_main(m)
        self.assertEqual(code, 0)
        self.assertIn("PURGE", out)
        self.assertEqual(self.deleted(), {1: 0, 2: 0, 3: 0})
        self.assertTrue(os.path.exists(m))

    def test_apply_sets_flag_and_timestamp(self):
        m = self.manifest("purge_a.json", self.entry(1, "2026-11-15 21:42:33", "stem one"))
        code, _ = self.run_main(m, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.deleted(), {1: 1, 2: 0, 3: 0})
        con = sqlite3.connect(self.db)
        stamp = con.execute("SELECT DeletedUtc FROM CustomPerfStats WHERE Id = 1").fetchone()[0]
        con.close()
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(os.path.exists(m), "manifest is kept without --delete-manifests")

    def test_mismatch_is_skipped_and_manifest_kept(self):
        m = self.manifest("purge_a.json",
                          self.entry(1, "2026-11-15 21:42:33", "stem one"),
                          self.entry(2, "2000-01-01 00:00:00", "stem two"),
                          self.entry(3, "2026-10-01 00:00:00", "wrong stem"))
        code, out = self.run_main(m, "--apply", "--delete-manifests")
        self.assertEqual(code, 1)
        self.assertEqual(self.deleted(), {1: 1, 2: 0, 3: 0})
        self.assertTrue(os.path.exists(m))
        self.assertIn("Kept", out)

    def test_rerun_is_idempotent_and_missing_ids_are_not_errors(self):
        m = self.manifest("purge_a.json", self.entry(1, "2026-11-15 21:42:33", "stem one"),
                          self.entry(99, "2026-01-01 00:00:00", "nope"))
        self.run_main(m, "--apply")
        code, out = self.run_main(m, "--apply", "--delete-manifests")
        self.assertEqual(code, 0)
        self.assertIn("already", out)
        self.assertFalse(os.path.exists(m))

    def test_delete_manifests_after_clean_apply(self):
        a = self.manifest("purge_a.json", self.entry(1, "2026-11-15 21:42:33", "stem one"))
        b = self.manifest("purge_b.json", self.entry(2, "2026-10-30 02:50:24", "stem two"))
        keep = self.manifest("other.json", self.entry(3, "2026-10-01 00:00:00", "stem three"))
        code, _ = self.run_main(os.path.join(self.dir, "purge*.json"), "--apply", "--delete-manifests")
        self.assertEqual(code, 0)
        self.assertEqual(self.deleted(), {1: 1, 2: 1, 3: 0})
        self.assertFalse(os.path.exists(a) or os.path.exists(b))
        self.assertTrue(os.path.exists(keep))

    def test_folder_path_and_backup(self):
        self.manifest("purge_a.json", self.entry(1, "2026-11-15 21:42:33", "stem one"))
        bak = os.path.join(self.dir, "bak")
        code, _ = self.run_main(self.dir, "--apply", "--backup", bak)
        self.assertEqual(code, 0)
        files = os.listdir(bak)
        self.assertEqual(len(files), 1)
        con = sqlite3.connect(os.path.join(bak, files[0]))
        self.assertEqual(con.execute("SELECT Deleted FROM CustomPerfStats WHERE Id = 1").fetchone()[0], 0)
        con.close()

    def test_no_backup_unless_requested(self):
        self.manifest("purge_a.json", self.entry(1, "2026-11-15 21:42:33", "stem one"))
        self.run_main(self.dir, "--apply")
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".bak")], [])

    def test_bad_manifest_is_reported_not_fatal(self):
        with open(os.path.join(self.dir, "purge_bad.json"), "w") as f:
            f.write("{not json")
        good = self.manifest("purge_good.json", self.entry(1, "2026-11-15 21:42:33", "stem one"))
        code, out = self.run_main(self.dir, "--apply", "--delete-manifests")
        self.assertEqual(code, 1)
        self.assertEqual(self.deleted()[1], 1)
        self.assertFalse(os.path.exists(good))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "purge_bad.json")))

    def test_unmigrated_database_is_refused(self):
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE CustomPerfStats DROP COLUMN Deleted")
        con.commit()
        con.close()
        m = self.manifest("purge_a.json", self.entry(1, "2026-11-15 21:42:33", "stem one"))
        code, out = self.run_main(m, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("migration 008", out)


if __name__ == "__main__":
    unittest.main()
