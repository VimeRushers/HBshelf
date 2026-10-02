"""Offline authentication and resume regression tests (use a temporary DATA_DIR)."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from shelf import worker
from shelf.downloads import (DownloadError, TransientDownloadError, archive_candidates, archive_checksum,
                             archive_servers, download_session, is_archive_url, login_archive, verify_checksum)


def response(status=200, payload=None, body=b"package", url="https://archive.org/download/item/file.pkg"):
    result = Mock()
    result.status_code = status
    result.is_redirect = 300 <= status < 400
    result.url = url
    result.headers = {"content-length": str(len(body))}
    result.json.return_value = payload
    result.iter_content.return_value = iter([body])
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


class AuthenticationTests(unittest.TestCase):
    def test_host_detection(self):
        for url in ("https://archive.org/file", "https://ia800.us.archive.org/file", "https://ARCHIVE.ORG/file"):
            self.assertTrue(is_archive_url(url))
        for url in ("https://archive.org.evil.test/file", "https://example.com/?url=archive.org", "https://archive.org@example.com/file"):
            self.assertFalse(is_archive_url(url))

    @patch.dict(os.environ, {"ARCHIVE_USER": "", "ARCHIVE_PASS": ""})
    def test_missing_credentials(self):
        session = Mock()
        with self.assertRaisesRegex(DownloadError, "ARCHIVE_USER"):
            login_archive(session)
        session.post.assert_not_called()

    @patch.dict(os.environ, {"ARCHIVE_USER": "test@example.com", "ARCHIVE_PASS": "test-password"})
    def test_login_installs_scoped_secure_cookies(self):
        with requests.Session() as session:
            payload = {"success": True, "values": {"cookies": {"logged-in-user": "user", "logged-in-sig": "signature"}}}
            with patch.object(session, "post", return_value=response(payload=payload)) as post:
                login_archive(session)
            self.assertEqual(post.call_args.kwargs["data"]["email"], "test@example.com")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            self.assertEqual(len(session.cookies), 2)
            for cookie in session.cookies:
                self.assertEqual(cookie.domain, ".archive.org")
                self.assertTrue(cookie.secure)
            for url, expected in (("https://ia800.us.archive.org/file", True), ("https://example.com/file", False), ("http://archive.org/file", False)):
                prepared = session.prepare_request(requests.Request("GET", url))
                self.assertEqual("Cookie" in prepared.headers, expected)

    @patch.dict(os.environ, {"ARCHIVE_USER": "test@example.com", "ARCHIVE_PASS": "test-password"})
    def test_bad_and_malformed_login_responses(self):
        for payload in ({"success": False}, [], {"success": True}, {"success": True, "values": {"cookies": {}}}):
            with self.subTest(payload=payload):
                session = Mock()
                session.post.return_value = response(payload=payload)
                with self.assertRaises(DownloadError):
                    login_archive(session)

    @patch.dict(os.environ, {"ARCHIVE_USER": "test@example.com", "ARCHIVE_PASS": "test-password"})
    def test_login_http_and_network_errors(self):
        for status in (401, 403, 302):
            session = Mock()
            session.post.return_value = response(status=status)
            with self.assertRaises(DownloadError):
                login_archive(session)
        for error in (requests.Timeout(), requests.ConnectionError()):
            session = Mock()
            session.post.side_effect = error
            with self.assertRaises(DownloadError):
                login_archive(session)

    def test_invalid_json(self):
        with patch.dict(os.environ, {"ARCHIVE_USER": "test@example.com", "ARCHIVE_PASS": "test-password"}):
            session = Mock()
            session.post.return_value = response()
            session.post.return_value.json.side_effect = ValueError("not JSON")
            with self.assertRaisesRegex(DownloadError, "invalid login response"):
                login_archive(session)

    def test_session_closes_after_failure_and_standard_hosts_skip_login(self):
        for url in ("https://example.com/file", "https://archive.org/file"):
            with patch("shelf.downloads.requests.Session") as factory, patch("shelf.downloads.login_archive") as login:
                with self.assertRaises(RuntimeError):
                    with download_session(url):
                        raise RuntimeError("transfer failed")
                self.assertEqual(login.call_count, int(is_archive_url(url)))
                factory.return_value.__exit__.assert_called_once()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.patch = patch.object(worker, "WORK", self.work)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.job = {"id": 1, "created": 1, "source": "http://archive.org/download/item/file.pkg"}

    def run_download(self, result):
        with patch.object(worker, "download_session") as factory, patch.object(worker, "need_space"), \
                patch.object(worker, "archive_checksum", return_value={}):
            session = factory.return_value.__enter__.return_value
            session.get.return_value = result
            folder = worker.download(self.job, Mock())
            return folder, session.get.call_args

    def test_stream_download_upgrades_archive_to_https(self):
        folder, call = self.run_download(response())
        self.assertEqual((folder / "file.pkg").read_bytes(), b"package")
        self.assertTrue(call.args[0].startswith("https://archive.org/"))
        self.assertTrue(call.kwargs["stream"])
        self.assertFalse(list(folder.glob("*.part")))

    def test_resumes_partial_file(self):
        folder = self.work / "dl-1-1"
        folder.mkdir()
        (folder / "file.pkg.part").write_bytes(b"pack")
        folder, call = self.run_download(response(status=206, body=b"age"))
        self.assertEqual(call.kwargs["headers"]["Range"], "bytes=4-")
        self.assertEqual((folder / "file.pkg").read_bytes(), b"package")

    def test_server_ignoring_range_restarts_file(self):
        folder = self.work / "dl-1-1"
        folder.mkdir()
        (folder / "file.pkg.part").write_bytes(b"old")
        folder, _ = self.run_download(response())
        self.assertEqual((folder / "file.pkg").read_bytes(), b"package")

    def test_denied_access_or_login_page_never_saved_as_package(self):
        for result in (response(status=403), response(url="https://archive.org/account/login")):
            with self.assertRaisesRegex(DownloadError, "denied"):
                self.run_download(result)
        self.assertFalse(list(self.work.rglob("*.part")))

    def test_standard_download(self):
        self.job["source"] = "https://example.com/file.pkg"
        folder, call = self.run_download(response(url=self.job["source"]))
        self.assertEqual(call.args[0], self.job["source"])
        self.assertEqual((folder / "file.pkg").read_bytes(), b"package")

    def test_checksum_mismatch_is_transient_and_keeps_file(self):
        with patch.object(worker, "download_session") as factory, patch.object(worker, "need_space"), \
                patch.object(worker, "archive_checksum", return_value={"md5": "0" * 32}):
            session = factory.return_value.__enter__.return_value
            session.get.return_value = response()
            with self.assertRaises(TransientDownloadError):
                worker.download(self.job, Mock())
        self.assertTrue((self.work / "dl-1-1" / "file.pkg").exists())

    def test_failed_transfer_keeps_partial_for_retry(self):
        result = response()

        def chunks():
            yield b"pack"
            raise requests.ConnectionError("connection lost")

        result.iter_content.return_value = chunks()
        with self.assertRaises(requests.ConnectionError):
            self.run_download(result)
        self.assertEqual((self.work / "dl-1-1" / "file.pkg.part").read_bytes(), b"pack")


class Aria2RotationTests(unittest.TestCase):
    URL = "https://archive.org/download/item/file.pkg"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(worker, "WORK", Path(self.temp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.job = {"id": 7, "created": 1, "source": self.URL}

    def test_rotates_to_next_server_after_transient_failure(self):
        candidates = [self.URL, "https://ia800/0/items/item/file.pkg", "https://ia900/0/items/item/file.pkg"]
        tried = []

        def fake_poll(cmd, job, part, total, name, report):
            tried.append(cmd[-1])
            if len(tried) == 1:
                raise TransientDownloadError("source node stalled")
            part.write_bytes(b"x" * total)
            return 0

        session = Mock()
        ctx = Mock()
        ctx.__enter__ = Mock(return_value=session)
        ctx.__exit__ = Mock(return_value=False)
        with patch.object(worker, "download_session", return_value=ctx), \
                patch.object(worker, "write_cookies", return_value=False), \
                patch.object(worker, "head_size", return_value=100), \
                patch.object(worker, "archive_candidates", return_value=candidates), \
                patch.object(worker, "archive_url_ok", return_value=True), \
                patch.object(worker, "archive_checksum", return_value={}), \
                patch.object(worker, "need_space"), \
                patch.object(worker, "_poll_aria2", side_effect=fake_poll):
            folder = worker.download_aria2(self.job, Mock())
        self.assertEqual(tried, [self.URL, candidates[1]])
        self.assertTrue((folder / "file.pkg").exists())


class ArchiveChecksumTests(unittest.TestCase):
    URL = "https://archive.org/download/item/file.pkg"

    def test_servers_are_ordered_and_deduplicated(self):
        meta = {"server": "ia800", "d1": "dn1", "d2": "dn1", "workable_servers": ["ia900", "ia800"]}
        self.assertEqual(archive_servers(meta), ["ia800", "dn1", "ia900"])

    def test_candidates_put_redirect_first_then_each_server(self):
        meta = {"dir": "/0/items/item", "server": "ia800", "workable_servers": ["ia900"]}
        with patch("shelf.downloads.archive_metadata", return_value=meta) as fetch:
            self.assertEqual(archive_candidates(self.URL), [
                self.URL,
                "https://ia800/0/items/item/file.pkg",
                "https://ia900/0/items/item/file.pkg",
            ])
        fetch.assert_called_once_with("item", None)

    def test_checksum_lookup_matches_exact_then_basename(self):
        meta = {"files": [{"name": "nested/file.pkg", "md5": "a" * 32, "sha1": "b" * 40, "crc32": "deadbeef", "size": "1"}]}
        with patch("shelf.downloads.archive_metadata", return_value=meta):
            self.assertEqual(archive_checksum(self.URL), {"md5": "a" * 32, "sha1": "b" * 40, "crc32": "deadbeef"})

    def test_checksum_lookup_unknown_file_is_empty(self):
        with patch("shelf.downloads.archive_metadata", return_value={"files": [{"name": "other.bin"}]}):
            self.assertEqual(archive_checksum(self.URL, "file.pkg"), {})

    def test_checksum_lookup_needs_no_network_when_not_archive(self):
        self.assertEqual(archive_checksum("https://example.com/file.pkg"), {})

    def test_verify_checksum_accepts_match_and_no_expected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "file.bin"
        path.write_bytes(b"package")
        verify_checksum(path, {})  # nothing expected: no-op
        verify_checksum(path, {"md5": hashlib.md5(b"package").hexdigest(), "sha1": hashlib.sha1(b"package").hexdigest()})

    def test_verify_checksum_mismatch_is_transient(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "file.bin"
        path.write_bytes(b"package")
        with self.assertRaisesRegex(TransientDownloadError, "checksum mismatch"):
            verify_checksum(path, {"md5": "0" * 32})
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
