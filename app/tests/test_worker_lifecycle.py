"""Terminal-state and work-dir lifecycle tests (no network, no DB volume)."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="shelf-test-"))

from shelf import db, worker
from shelf.config import DEFAULTS
from shelf.convert import ConvertError
from shelf.delivery import Cancelled, Paused
from shelf.downloads import DownloadError, TransientDownloadError


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

    def test_retry_columns_and_bandwidth_default_exist(self):
        cols = {r["name"] for r in db.q("PRAGMA table_info(jobs)")}
        self.assertIn("attempts", cols)
        self.assertIn("retry_at", cols)
        self.assertIn("pause_download_while_sending", DEFAULTS)


class RetryTests(unittest.TestCase):
    def setUp(self):
        # park anything earlier tests left queued so claim_job is deterministic
        db.x("UPDATE jobs SET status='failed' WHERE status IN ('queued','running')")

    def make_job(self, kind="download"):
        return db.add_job(kind, "https://archive.org/download/item/file.pkg")

    def row(self, job_id):
        return db.q("SELECT * FROM jobs WHERE id=?", job_id)[0]

    def test_transient_classification(self):
        transient = [
            TransientDownloadError("node died"),
            requests.ConnectionError("reset"),
            requests.Timeout("slow"),
            requests.HTTPError(response=_response(500)),
            DownloadError("the download was too slow"),
            DownloadError("the download server sent a bad response header"),
            ConvertError("download ended early; retry to resume"),
            OSError("Connection reset by peer"),
        ]
        permanent = [
            Cancelled(),
            Paused(),
            DownloadError("the download server said the file was not found (HTTP 404)"),
            DownloadError("Internet Archive denied this download."),
            ConvertError("no installable PKG found"),
            ConvertError("not enough space in /data"),
            requests.HTTPError(response=_response(404)),
            OSError("No space left on device"),
        ]
        for exc in transient:
            self.assertTrue(worker.is_transient(exc), exc)
        for exc in permanent:
            self.assertFalse(worker.is_transient(exc), exc)

    def test_transient_failure_requeues_with_backoff(self):
        job_id = self.make_job()
        self.assertTrue(worker._handle_failure(self.row(job_id), TransientDownloadError("too slow")))
        row = self.row(job_id)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 1)
        self.assertIn("retry 1/", row["message"])
        self.assertGreater(row["retry_at"], time.time() + 50)
        self.assertLess(row["retry_at"], time.time() + 70)

    def test_attempts_increment_until_max_then_permanent(self):
        job_id = self.make_job()
        for attempt in range(1, worker.MAX_RETRIES + 1):
            self.assertTrue(worker._handle_failure(self.row(job_id), TransientDownloadError("too slow")))
            row = self.row(job_id)
            self.assertEqual(row["attempts"], attempt)
            self.assertGreater(row["retry_at"], time.time())
            db.update_job(job_id, status="queued", retry_at=0)
        self.assertFalse(worker._handle_failure(self.row(job_id), TransientDownloadError("too slow")))
        self.assertEqual(self.row(job_id)["attempts"], worker.MAX_RETRIES + 1)

    def test_non_transient_failure_is_not_requeued(self):
        job_id = self.make_job()
        self.assertFalse(worker._handle_failure(self.row(job_id), ConvertError("not enough space in /data")))
        row = self.row(job_id)
        self.assertEqual(row["retry_at"], 0)
        self.assertEqual(row["attempts"], 1)

    def test_claim_skips_jobs_until_retry_at(self):
        job_id = self.make_job()
        db.update_job(job_id, retry_at=time.time() + 3600)
        self.assertIsNone(db.claim_job(["download"]))
        db.update_job(job_id, retry_at=0)
        claimed = db.claim_job(["download"])
        assert claimed is not None
        self.assertEqual(claimed["id"], job_id)

    def test_finish_done_resets_retry_state(self):
        job_id = self.make_job()
        worker._handle_failure(self.row(job_id), TransientDownloadError("too slow"))
        db.finish_job(job_id, "done", progress=1)
        row = self.row(job_id)
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["retry_at"], 0)

    def test_cancel_keeps_partial_on_disk(self):
        job_id = self.make_job()
        created = self.row(job_id)["created"]
        d = worker.work_dir(job_id, created)
        d.mkdir(parents=True)
        part = d / "file.pkg.part"
        part.write_bytes(b"partial")
        job = self.row(job_id)
        with patch.object(worker, "is_aria2", return_value=False), \
                patch.object(worker, "download", side_effect=Cancelled):
            with self.assertRaises(Cancelled):
                worker.run_fetch(job)
        self.assertTrue(part.exists())


def _response(status):
    r = requests.Response()
    r.status_code = status
    return r


if __name__ == "__main__":
    unittest.main()
