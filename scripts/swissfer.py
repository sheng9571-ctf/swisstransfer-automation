import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import pexpect
import requests

from scripts.common import AutomationError, progress

LINK_PATTERN = r"Download link: (https://www\.swisstransfer\.com/dl/[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})(?=\s|$)"


def install_binary(version, directory):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise AutomationError("Invalid SWISSFER_VERSION")
    directory.mkdir(parents=True, exist_ok=True)
    asset = f"swissfer-{version}-x86_64-unknown-linux-musl.tar.gz"
    url = f"https://github.com/Blutsh/swissfer/releases/download/{version}/{asset}"
    archive = directory / asset
    try:
        with requests.get(url, stream=True, timeout=(10, 60)) as response:
            if response.status_code != 200:
                raise AutomationError(f"swissfer release download failed (HTTP {response.status_code})")
            with archive.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    output.write(chunk)
        # Extract only the executable; do not trust archive paths or links.
        with tarfile.open(archive, "r:gz") as tar:
            candidates = [member for member in tar.getmembers()
                          if member.isfile() and Path(member.name).name == "swissfer"]
            if len(candidates) != 1:
                raise AutomationError("swissfer archive does not contain a unique binary")
            binary = directory / "swissfer"
            with tar.extractfile(candidates[0]) as source, binary.open("wb") as output:
                import shutil
                shutil.copyfileobj(source, output)
        binary.chmod(0o700)
        result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True)
        if not re.search(rf"\bswissfer\s+{re.escape(version)}\b", result.stdout):
            raise AutomationError("Downloaded swissfer version does not match configuration")
        progress(f"Using swissfer {version}")
        return binary
    finally:
        archive.unlink(missing_ok=True)


def upload(binary, directory, email, duration, downloads, mail):
    if duration not in (1, 3, 7, 15, 30) or downloads not in (1, 20, 100, 200, 250):
        raise AutomationError("Unsupported SwissTransfer duration or download count")
    since, existing = mail.prepare()
    # Isolate validation tokens, never persist them or include them in artifacts.
    with tempfile.TemporaryDirectory(prefix="swissfer-config-") as config:
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = config
        child = pexpect.spawn(str(binary), ["-e", email, "-d", str(duration),
                              "-n", str(downloads), str(directory)],
                              env=env, encoding="utf-8", codec_errors="replace",
                              echo=False, timeout=15, maxread=4096)
        link, verified = None, False
        last_progress = time.monotonic()
        try:
            while True:
                event = child.expect([r"Code:\s*", LINK_PATTERN, pexpect.EOF, pexpect.TIMEOUT])
                # Consume but never print raw CLI output or buffers.
                if event == 0:
                    if verified:
                        raise AutomationError("swissfer requested verification again; OTP may have been rejected")
                    progress("Waiting for SwissTransfer verification email")
                    code = mail.wait_code(since, existing)
                    child.sendline(code)
                    verified = True
                    progress("Verification code submitted; uploading")
                elif event == 1:
                    link = child.match.group(1)
                elif event == 2:
                    break
                if time.monotonic() - last_progress >= 30:
                    progress("swissfer is running; waiting for upload completion")
                    last_progress = time.monotonic()
            child.close()
            if child.exitstatus != 0 or child.signalstatus is not None:
                raise AutomationError("swissfer upload failed (CLI output withheld to protect credentials)")
            if not link:
                raise AutomationError("swissfer exited without a completed download link")
            return link
        finally:
            if child.isalive():
                child.terminate(force=True)
            child.close()
