"""Terminal-state and work-dir lifecycle tests (no network, no DB volume)."""
import os
import tempfile
import unittest

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="shelf-test-"))

from shelf import db, worker


class JobLifecycleTests(unittest.TestCase):
    def make_job(self):
        return db.add_job("download", "https://archive.org/download/item/file.pkg")

    def test_finish_after_cancel_writes_terminal_state(self):
        job_id = self.make_job()
        db.update_job(job_id, status="cancelled", message="Cancelling…")
        db.finish_job(job_id, "failed", message="Cancelled")
        row = db.q("SELECT status, message FROM jobs WHERE id=?", job_id)[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["message"], "Cancelled")

    def test_finish_after_pause_writes_done(self):
        job_id = self.make_job()
        db.update_job(job_id, status="paused")
        db.finish_job(job_id, "done", progress=1, message="added: Game")
        row = db.q("SELECT status, progress, message FROM jobs WHERE id=?", job_id)[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["progress"], 1)
        self.assertEqual(row["message"], "added: Game")

    def test_finish_does_not_need_running_status(self):
        job_id = self.make_job()
        self.assertEqual(db.q("SELECT status FROM jobs WHERE id=?", job_id)[0]["status"], "queued")
        db.finish_job(job_id, "failed", message="boom")
        self.assertEqual(db.q("SELECT status FROM jobs WHERE id=?", job_id)[0]["status"], "failed")

    def test_work_dir_is_deterministic_and_removed(self):
        job_id = self.make_job()
        created = db.q("SELECT created FROM jobs WHERE id=?", job_id)[0]["created"]
        path = worker.work_dir(job_id)
        self.assertEqual(path, worker.work_dir(job_id, created))
        path.mkdir(parents=True)
        (path / "file.pkg.part").write_bytes(b"partial")
        worker.remove_work_dir(job_id)
        self.assertFalse(path.exists())

    def test_remove_missing_work_dir_is_noop(self):
        job_id = self.make_job()
        self.assertFalse(worker.work_dir(job_id).exists())
        worker.remove_work_dir(job_id)


if __name__ == "__main__":
    unittest.main()
