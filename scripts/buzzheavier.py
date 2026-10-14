import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from curl_cffi import CurlOpt, requests

from scripts.common import AutomationError, check_space, progress, show_disk

BASE = "https://buzzheavier.com"


def parse_links(value):
    links = [line.strip() for line in value.splitlines() if line.strip()]
    for link in links:
        parsed = urlparse(link)
        if (parsed.scheme != "https" or parsed.hostname not in
                ("buzzheavier.com", "www.buzzheavier.com") or parsed.username or
                parsed.port or not re.fullmatch(r"/fs/[A-Za-z0-9]+/?", parsed.path)
                or parsed.query or parsed.fragment):
            raise AutomationError("BUZZHEAVIER_LINKS must contain HTTPS Buzzheavier folder URLs")
    return links


def folder(client, account_id, folder_id):
    response = client.get(
        f"{BASE}/api/fs/{folder_id}",
        headers={"Authorization": f"Bearer {account_id}"}, timeout=30,
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise AutomationError(f"Buzzheavier folder API failed (HTTP {response.status_code})")
    data = response.json()
    if data.get("code") != 200 or not isinstance(data.get("data"), dict):
        raise AutomationError("Buzzheavier folder API returned an invalid response")
    return data["data"]


def collect(client, account_id, links):
    found, seen = {}, set()
    pending = [urlparse(link).path.strip("/").split("/")[1] for link in links]
    while pending:
        folder_id = pending.pop()
        if folder_id in seen:
            continue
        seen.add(folder_id)
        entries = folder(client, account_id, folder_id).get("children")
        if not isinstance(entries, list):
            raise AutomationError("Buzzheavier folder listing is incomplete")
        for entry in entries:
            fid = entry.get("id", "")
            if not isinstance(fid, str) or not re.fullmatch(r"[A-Za-z0-9]+", fid):
                raise AutomationError("Buzzheavier returned an invalid file ID")
            if entry.get("isDirectory"):
                pending.append(fid)
            else:
                found.setdefault(fid, entry)
    if not found:
        raise AutomationError("No files found in Buzzheavier folders")
    return list(found.values())


def resolve_download(client, file_id):
    page_url = f"{BASE}/{file_id}"
    page = client.get(page_url, timeout=30)
    if page.status_code != 200 or "text/html" not in page.headers.get("content-type", "").lower():
        raise AutomationError("Buzzheavier file page could not be loaded")
    token = re.search(r'download\?t=([^"\x27&\\#<>\s]+)', page.text)
    if not token:
        raise AutomationError("Buzzheavier download token missing (possibly a challenge)")
    for suffix in ("", "&alt=true"):
        endpoint = f"{page_url}/download?t={token.group(1)}{suffix}"
        response = client.get(endpoint, headers={
            "HX-Request": "true", "HX-Current-URL": page_url, "Referer": page_url,
        }, allow_redirects=False, timeout=30)
        if response.status_code >= 400:
            continue
        destination = response.headers.get("HX-Redirect") or response.headers.get("Location")
        if not destination:
            match = re.search(r'https?://[^\s"\x27<>]+/d/[^\s"\x27<>]+', response.text)
            destination = match.group(0) if match else None
        if destination:
            url = urljoin(endpoint, destination)
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username:
                return url
    raise AutomationError("Buzzheavier did not provide an HTTPS download URL")


def file_name(entry, used):
    original = str(entry.get("name") or entry["id"])
    name = re.sub(r'[\x00-\x1f\x7f/\\]', "_", original).strip(" .") or entry["id"]
    # Keep Linux's per-component byte limit, including collision suffix.
    while len(name.encode("utf-8")) > 180:
        name = name[:-1]
    if name in used:
        name = f"{entry['id']}_{name}"
    if name in used:
        raise AutomationError("Unable to allocate a unique download filename")
    used.add(name)
    return name


def download_one(client, entry, destination):
    expected = entry.get("size")
    if expected is not None and (not isinstance(expected, int) or isinstance(expected, bool) or expected < 0):
        raise AutomationError("Buzzheavier returned an invalid file size")
    show_disk(destination.parent)
    check_space(destination.parent, expected or 0)
    url = resolve_download(client, entry["id"])
    response = client.get(url, stream=True, timeout=None,
                          headers={"Referer": BASE + "/", "Accept-Encoding": "identity"})
    partial = destination.parent.parent / (entry["id"] + ".part")
    received = 0
    try:
        if response.status_code != 200:
            raise AutomationError(f"Buzzheavier file download failed (HTTP {response.status_code})")
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise AutomationError("Buzzheavier returned a page instead of file data")
        length = response.headers.get("content-length")
        length = int(length) if length is not None else None
        if expected is not None and length is not None and expected != length:
            raise AutomationError("Download length does not match Buzzheavier file size")
        total = expected if expected is not None else length
        check_space(destination.parent, total or 0)
        last_report = time.monotonic()
        with partial.open("xb") as output:
            for chunk in response.iter_content():
                if not chunk:
                    continue
                check_space(destination.parent, len(chunk))
                if total is not None and received + len(chunk) > total:
                    raise AutomationError("Download exceeds the expected file size")
                output.write(chunk)
                received += len(chunk)
                if time.monotonic() - last_report >= 15:
                    progress(f"Downloaded {received:,} bytes: {destination.name}")
                    last_report = time.monotonic()
        if total is None:
            raise AutomationError("Cannot verify download: no authoritative file size")
        if received != total:
            raise AutomationError("Incomplete Buzzheavier download")
        partial.replace(destination)
    finally:
        response.close()
        partial.unlink(missing_ok=True)
    show_disk(destination.parent)
    return {"name": destination.name, "size": received, "source_id": entry["id"]}


def download_all(links, account_id, directory):
    directory.mkdir(parents=True)
    used, manifest = set(), []
    with requests.Session(impersonate="chrome", curl_options={
            CurlOpt.CONNECTTIMEOUT: 30, CurlOpt.LOW_SPEED_LIMIT: 1,
            CurlOpt.LOW_SPEED_TIME: 120,
    }) as client:
        entries = collect(client, account_id, links)
        progress(f"Found {len(entries)} unique files")
        known = [e.get("size") for e in entries]
        if all(isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in known):
            check_space(directory, sum(known))
        for index, entry in enumerate(entries, 1):
            name = file_name(entry, used)
            progress(f"Downloading {index}/{len(entries)}: {name}")
            manifest.append(download_one(client, entry, directory / name))
    return manifest
