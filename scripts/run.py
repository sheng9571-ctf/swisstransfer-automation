import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.common import AutomationError, progress


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise AutomationError(f"Missing {name}")
    return value


def main():
    raw_links = os.environ.get("BUZZHEAVIER_LINKS", "").strip()
    if not raw_links:
        progress("BUZZHEAVIER_LINKS is empty; skipping download and upload")
        return 0
    # Lazy imports let the no-input path skip dependencies and credential checks.
    from scripts.buzzheavier import download_all, parse_links
    from scripts.mailslurp import MailSlurp
    from scripts.swissfer import install_binary, upload

    links = parse_links(raw_links)
    account = required("BUZZHEAVIER_ACCOUNT_ID")
    email, key, inbox = required("SWISS_EMAIL"), required("MAILSLURP_API_KEY"), required("MAILSLURP_INBOX_ID")
    version = os.environ.get("SWISSFER_VERSION", "").strip() or "3.0.0"
    duration = int(os.environ.get("SWISSFER_DURATION_DAYS", "").strip() or "30")
    downloads = int(os.environ.get("SWISSFER_MAX_DOWNLOADS", "").strip() or "20")
    if duration not in (1, 3, 7, 15, 30) or downloads not in (1, 20, 100, 200, 250):
        raise AutomationError("Unsupported SwissTransfer duration or download count")
    root = Path(__file__).resolve().parents[1]
    runtime = root / ".runtime"
    runtime.mkdir(exist_ok=True)
    results = root / "results"
    results.mkdir(exist_ok=True)
    for name in ("share-link.txt", "result.json"):
        (results / name).unlink(missing_ok=True)
    mail = MailSlurp(key, inbox, email)
    try:
        # Validate inbox configuration before downloading tens of GB.
        mail.prepare()
        binary = install_binary(version, runtime)
        with tempfile.TemporaryDirectory(prefix="transfer-", dir=runtime) as workspace:
            directory = Path(workspace) / "downloads"
            manifest = download_all(links, account, directory)
            progress(f"All {len(manifest)} files downloaded; starting one SwissTransfer transfer")
            link = upload(binary, directory, email, duration, downloads, mail)
            (results / "share-link.txt").write_text(link + "\n", encoding="utf-8")
            (results / "result.json").write_text(json.dumps({
                "share_url": link, "completed_at": datetime.now(timezone.utc).isoformat(),
                "swissfer_version": version, "duration_days": duration,
                "max_downloads": downloads,
                "files": [{"name": entry["name"], "size": entry["size"]} for entry in manifest],
                "total_bytes": sum(entry["size"] for entry in manifest),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            progress(f"Download link: {link}")
    finally:
        mail.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AutomationError as error:
        progress(f"ERROR: {error}")
        sys.exit(1)
    except Exception as error:
        # Requests/pexpect errors may contain URLs, headers, OTPs or API bodies.
        progress(f"ERROR: {type(error).__name__}; raw details withheld")
        sys.exit(1)
