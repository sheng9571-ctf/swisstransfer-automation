import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import buzzheavier, mailslurp, swissfer
from scripts.common import AutomationError, RESERVE, check_space
from scripts.run import main

LINK = "https://www.swisstransfer.com/dl/01a1392e-5737-7317-a781-e63eb4f11a28"


class FilePageTests(unittest.TestCase):
    def response(self, status=200, headers=None, text=""):
        response = Mock(status_code=status, headers=headers or {}, text=text)
        client = Mock()
        client.get.return_value = response
        return client, response

    def test_http_error_reports_status_without_body(self):
        for status in (403, 404, 429, 503):
            client, response = self.response(status, text="SECRET_DOWNLOAD_TOKEN")
            with self.assertRaises(AutomationError) as caught:
                buzzheavier.read_file_page(client, "https://buzzheavier.com/file")
            self.assertIn(f"HTTP {status}", str(caught.exception))
            self.assertNotIn("SECRET_DOWNLOAD_TOKEN", str(caught.exception))
            response.close.assert_called_once()

    def test_challenge_is_distinct_from_normal_html(self):
        for headers, body in (
                ({"cf-mitigated": "challenge"}, "SECRET"),
                ({"content-type": "text/html"}, "<html><title>Just a moment...</title>")):
            client, response = self.response(403, headers, body)
            with self.assertRaisesRegex(AutomationError, "anti-bot challenge"):
                buzzheavier.read_file_page(client, "https://buzzheavier.com/file")
            response.close.assert_called_once()

    def test_missing_or_mislabelled_type_accepts_html_document(self):
        for headers in ({}, {"content-type": "text/plain"}):
            body = '<!DOCTYPE html><html>download?t=secret</html>'
            client, response = self.response(200, headers, body)
            self.assertEqual(buzzheavier.read_file_page(client, "https://buzzheavier.com/file"), body)
            response.close.assert_called_once()

    def test_non_html_is_rejected(self):
        client, response = self.response(200, {"content-type": "application/json"}, '{"secret": "token"}')
        with self.assertRaisesRegex(AutomationError, "JSON"):
            buzzheavier.read_file_page(client, "https://buzzheavier.com/file")
        response.close.assert_called_once()



class DownloadTests(unittest.TestCase):
    def test_folder_links_and_invalid_host(self):
        self.assertEqual(len(buzzheavier.parse_links(" \nhttps://buzzheavier.com/fs/abc123\n")), 1)
        for value in ("https://buzzheavier.com.evil/fs/a", "http://buzzheavier.com/fs/a",
                      "https://buzzheavier.com/file", "https://u@buzzheavier.com/fs/a"):
            with self.assertRaises(AutomationError):
                buzzheavier.parse_links(value)

    def test_recursive_dedup_and_fail_closed(self):
        listings = {
            "a": {"children": [{"id": "b", "isDirectory": True}, {"id": "f", "size": 3}]},
            "b": {"children": [{"id": "a", "isDirectory": True}, {"id": "f", "size": 3}]},
        }
        with patch.object(buzzheavier, "folder", side_effect=lambda c, a, fid: listings[fid]):
            result = buzzheavier.collect(None, "secret", ["https://buzzheavier.com/fs/a",
                                                         "https://buzzheavier.com/fs/b"])
        self.assertEqual([e["id"] for e in result], ["f"])
        with patch.object(buzzheavier, "folder", side_effect=AutomationError("API failed")):
            with self.assertRaises(AutomationError):
                buzzheavier.collect(None, "secret", ["https://buzzheavier.com/fs/a"])

    def test_safe_names_and_collisions(self):
        used = set()
        first = buzzheavier.file_name({"id": "a", "name": "../test.zip"}, used)
        second = buzzheavier.file_name({"id": "b", "name": "../test.zip"}, used)
        self.assertNotIn("/", first)
        self.assertNotEqual(first, second)

    def test_disk_space(self):
        with patch("scripts.common.shutil.disk_usage", return_value=Mock(free=RESERVE + 2)):
            with self.assertRaises(AutomationError):
                check_space(Path("."), 3)

    def download(self, chunks, size, length="3", space_error=None):
        response = Mock(status_code=200, headers={"content-length": length,
                                                 "content-type": "application/octet-stream"})
        response.iter_content.return_value = iter(chunks)
        client = Mock()
        client.get.return_value = response
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "downloads"
            directory.mkdir()
            target = directory / "test.zip"
            with patch.object(buzzheavier, "resolve_download", return_value="https://cdn.example/d/a"), \
                    patch.object(buzzheavier, "show_disk"), \
                    patch.object(buzzheavier, "check_space", side_effect=space_error):
                if space_error or sum(map(len, chunks)) != size:
                    with self.assertRaises(AutomationError):
                        buzzheavier.download_one(client, {"id": "a", "size": size}, target)
                    self.assertFalse(target.exists())
                else:
                    buzzheavier.download_one(client, {"id": "a", "size": size}, target)
                    self.assertEqual(target.read_bytes(), b"".join(chunks))
                self.assertFalse((Path(root) / "a.part").exists())
        response.close.assert_called_once()
        self.assertIsNone(client.get.call_args.kwargs["timeout"])

    def test_full_stream(self):
        self.download([b"a", b"bc"], 3)

    def test_truncated_stream(self):
        self.download([b"a"], 3)

    def test_space_failure_during_stream(self):
        self.download([b"abc"], 3, space_error=[None, None, AutomationError("disk full")])


class MailTests(unittest.TestCase):
    def setUp(self):
        self.since = datetime.now(timezone.utc)
        self.email = {"from": "noreply@swisstransfer.com", "to": ["test@example.com"],
                      "subject": "Swisstransfer confirmation code: 95HHDK",
                      "createdAt": self.since.isoformat()}

    def test_alphanumeric_and_old_or_unrelated_mail(self):
        self.assertEqual(mailslurp.extract_code(self.email, "test@example.com", self.since), "95HHDK")
        for changes in ({"from": "evil@example.com"}, {"to": ["other@example.com"]},
                        {"createdAt": (self.since - timedelta(seconds=1)).isoformat()},
                        {"subject": "Unrelated 95HHDK"}, {"createdAt": "invalid"}):
            self.assertIsNone(mailslurp.extract_code(self.email | changes, "test@example.com", self.since))

    def test_wait_ignores_existing_ids(self):
        mail = mailslurp.MailSlurp("secret", "inbox", "test@example.com")
        with patch.object(mail, "get", side_effect=[[{"id": "old"}, {"id": "new"}], self.email]) as get:
            self.assertEqual(mail.wait_code(self.since, {"old"}), "95HHDK")
            self.assertEqual(get.call_count, 2)
        mail.close()

    def test_rate_limit_is_safe(self):
        mail = mailslurp.MailSlurp("secret", "inbox", "test@example.com")
        response = Mock(status_code=429)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(mail.client, "get", return_value=response):
            with self.assertRaisesRegex(AutomationError, "rate limit"):
                mail.get("/test")
        mail.close()

    def test_timeout(self):
        mail = mailslurp.MailSlurp("secret", "inbox", "test@example.com")
        with self.assertRaisesRegex(AutomationError, "Timed out"):
            mail.wait_code(self.since, set(), timeout=0)
        mail.close()


class RunTests(unittest.TestCase):
    def test_empty_links_skips_everything(self):
        with patch.dict(os.environ, {"BUZZHEAVIER_LINKS": " \n"}, clear=True):
            with patch("scripts.buzzheavier.download_all") as download:
                self.assertEqual(main(), 0)
                download.assert_not_called()

    def test_link_extraction(self):
        match = re.search(swissfer.LINK_PATTERN, "Download link: " + LINK + "\r\n")
        self.assertEqual(match.group(1), LINK)

    def test_cli_exit_code_is_required(self):
        child = Mock(exitstatus=1, signalstatus=None)
        child.expect.side_effect = [1, 2]
        child.match.group.return_value = LINK
        child.isalive.return_value = False
        mail = Mock()
        mail.prepare.return_value = (datetime.now(timezone.utc), set())
        with patch.object(swissfer.pexpect, "spawn", return_value=child, create=True):
            with self.assertRaisesRegex(AutomationError, "upload failed"):
                swissfer.upload(Path("/fake"), Path("/files"), "test@example.com", 30, 20, mail)


@unittest.skipUnless(os.name == "posix", "Real PTY requires Linux/macOS")
class PtyTests(unittest.TestCase):
    def run_cli(self, exit_code=0, include_link=True, prompt=True):
        # Exercise actual stdin interaction and EOF handling without external APIs.
        with tempfile.TemporaryDirectory() as root:
            executable = Path(root) / "fake-swissfer"
            executable.write_text("#!" + sys.executable + "\n" +
                ("print('Code: ', end='', flush=True)\ncode = input()\nassert code == '95HHDK'\n" if prompt else "") +
                (f"print('Download link: {LINK}')\n" if include_link else "") +
                f"raise SystemExit({exit_code})\n")
            executable.chmod(0o700)
            mail = Mock()
            mail.prepare.return_value = (datetime.now(timezone.utc), set())
            mail.wait_code.return_value = "95HHDK"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = swissfer.upload(executable, Path(root), "test@example.com", 30, 20, mail)
            self.assertNotIn("95HHDK", output.getvalue())
            self.assertEqual(mail.wait_code.call_count, int(prompt))
            return result

    def test_real_otp_pty(self):
        self.assertEqual(self.run_cli(), LINK)

    def test_no_prompt(self):
        self.assertEqual(self.run_cli(prompt=False), LINK)

    def test_failure_after_link(self):
        with self.assertRaises(AutomationError):
            self.run_cli(exit_code=1)

    def test_missing_link(self):
        with self.assertRaises(AutomationError):
            self.run_cli(include_link=False)


class BinaryTests(unittest.TestCase):
    def test_archive_extracts_only_regular_binary(self):
        import tarfile
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as archive:
            info = tarfile.TarInfo("nested/swissfer")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"test"))
            unwanted = tarfile.TarInfo("../../outside")
            unwanted.size = 3
            archive.addfile(unwanted, io.BytesIO(b"bad"))
        response = Mock(status_code=200)
        response.iter_content.return_value = [payload.getvalue()]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as root, \
                patch.object(swissfer.requests, "get", return_value=response), \
                patch.object(swissfer.subprocess, "run", return_value=Mock(stdout="swissfer 3.0.0\n")):
            binary = swissfer.install_binary("3.0.0", Path(root) / "bin")
            self.assertEqual(binary.read_bytes(), b"test")
            self.assertFalse((Path(root) / "outside").exists())
            self.assertEqual(list(binary.parent.iterdir()), [binary])

    def test_rejects_version_url_injection(self):
        with self.assertRaises(AutomationError):
            swissfer.install_binary("../latest", Path("."))


class StreamIntegrationTests(unittest.TestCase):
    def test_actual_curl_stream_local_server(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from curl_cffi import CurlOpt, requests as curl_requests

        content = b"full-stream-test" * 10000

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as root, curl_requests.Session(
                    impersonate="chrome", curl_options={CurlOpt.CONNECTTIMEOUT: 30,
                    CurlOpt.LOW_SPEED_LIMIT: 1, CurlOpt.LOW_SPEED_TIME: 120}) as client:
                directory = Path(root) / "downloads"
                directory.mkdir()
                target = directory / "file.bin"
                url = f"http://127.0.0.1:{server.server_port}/file"
                with patch.object(buzzheavier, "resolve_download", return_value=url), \
                        patch.object(buzzheavier, "show_disk"):
                    result = buzzheavier.download_one(client, {"id": "test", "size": len(content)}, target)
                self.assertEqual(target.read_bytes(), content)
                self.assertEqual(result["size"], len(content))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == "__main__":
    unittest.main()
