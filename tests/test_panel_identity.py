"""Bounded Recents identity regressions; also runnable on stock Python without pytest.

No encoder, real jobs, desktop configuration or recording folders are touched.
Rename/focus membership regressions are exercised against real QML in the local
platform harness, rather than by duplicating the UI's algorithm in Python.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dolly import health, library


class RecentsIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def check_job_identity(self, link_kind):
        real_root = self.root / "real-library"
        real_project = real_root / "take"
        real_project.mkdir(parents=True)
        (real_project / "capture.json").write_text('{"started": 1700000000}')
        (real_project / "exports").mkdir()
        root = self.root / "library-alias"
        if link_kind == "library":
            root.symlink_to(real_root, target_is_directory=True)
            action_path = root / "take"
        else:
            root.mkdir()
            action_path = root / "project-alias"
            action_path.symlink_to(real_project, target_is_directory=True)
        run = self.root / "run"
        run.mkdir()
        with patch.dict(os.environ, {"DOLLY_DIR": str(root)}), \
                patch.object(health, "runtime_dir", return_value=run), patch.object(health, "_refresh"):
            row = library.entry(action_path, with_poster=False)
            job = health.Job("rendering", action_path.name, str(action_path / "exports" / "demo.mp4"))
            try:
                self.assertEqual(row["dir"], str(action_path))  # actions retain symlink spelling
                self.assertEqual(row["projectdir"], str(real_project))
                self.assertEqual(row["projectdir"], job.info["projectdir"])
                self.assertEqual(health.jobs()[0]["projectdir"], row["projectdir"])
            finally:
                job.done()

    def test_symlink_library_identity_matches_job(self):
        self.check_job_identity("library")

    def test_symlink_project_identity_matches_job(self):
        self.check_job_identity("project")

    def check_cache_identity(self, old_key):
        root = self.root / "real-library"
        project = root / "take"
        project.mkdir(parents=True)
        alias = self.root / "library-alias"
        alias.symlink_to(root, target_is_directory=True)
        row = {"dir": str(alias / "take"), "id": "take", "created": 1, "bytes": 0,
               "raw_bytes": 0, "exported": False, "raw": False}
        if old_key:
            row["projectdir"] = old_key
        with patch.dict(os.environ, {"DOLLY_DIR": str(alias), "XDG_CACHE_HOME": str(self.root / "cache")}), \
                patch.object(library, "_live_dir", return_value=None), \
                patch.object(library, "entry", side_effect=AssertionError("must not rescan")), \
                patch.object(library, "poster", side_effect=AssertionError("must not make a poster")):
            library._write_atomic(library.cache_path(), {"version": 3, "items": [row]})
            self.assertEqual(library._load_cache()["items"][0]["projectdir"], str(project))
            loaded = library.index(refresh=False, posters=False)["items"][0]
            self.assertEqual(loaded["projectdir"], str(project))
            self.assertEqual(loaded["dir"], row["dir"])
            self.assertEqual(json.loads(library.cache_path().read_text())["items"][0]["projectdir"], str(project))

    def test_v3_cache_backfills_missing_identity_without_rescan(self):
        self.check_cache_identity(None)

    def test_v3_cache_refreshes_stale_identity_without_rescan(self):
        self.check_cache_identity("/outdated/canonical/path")


if __name__ == "__main__":
    unittest.main()
