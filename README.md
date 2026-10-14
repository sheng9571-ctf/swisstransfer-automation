# SwissTransfer automation

Python 3 automation for downloading every file in Buzzheavier folders, then uploading
the files sequentially in **one swissfer invocation / one shared transfer**.
No Docker or Rust build is required.

## GitHub setup

Use a public repository with a standard `ubuntu-24.04` GitHub-hosted runner.
Configure **Settings → Secrets and variables → Actions**:

### Repository Secrets

| Secret | Required when links are set | Description |
| --- | --- | --- |
| `BUZZHEAVIER_LINKS` | Yes | One HTTPS `https://buzzheavier.com/fs/<folder-id>` folder URL per line |
| `BUZZHEAVIER_ACCOUNT_ID` | Yes | Bearer credential used by the Buzzheavier folder API |
| `SWISS_EMAIL` | Yes | MailSlurp inbox email address |
| `MAILSLURP_API_KEY` | Yes | MailSlurp API key |
| `MAILSLURP_INBOX_ID` | Yes | Inbox ID matching `SWISS_EMAIL` |

No `links.txt` fallback exists. An unset/whitespace-only `BUZZHEAVIER_LINKS`
skips the job's work successfully, without requiring any other secrets.

### Optional Repository Variables

| Variable | Default | Supported values |
| --- | --- | --- |
| `SWISSFER_VERSION` | `3.0.0` | Official release version with the Linux x64 musl asset |
| `SWISSFER_DURATION_DAYS` | `30` | `1, 3, 7, 15, 30` |
| `SWISSFER_MAX_DOWNLOADS` | `20` | `1, 20, 100, 200, 250` |

The workflow maps these secrets/variables to same-named process environment variables.
Run **Actions → Buzzheavier to SwissTransfer → Run workflow** after publishing the
workflow to the default branch. No schedule is included.

## What runs

1. Check for links; skip if empty.
2. Free disk space using the configured cleanup commands. Python setup runs
   **after** deleting the hosted tool cache.
3. Install Python 3.14 and dependencies, then run tests.
4. Validate the MailSlurp inbox configuration.
5. Download the configured official binary from
   `https://github.com/Blutsh/swissfer/releases/download/<version>/swissfer-<version>-x86_64-unknown-linux-musl.tar.gz`.
   Extract only its regular executable member and verify `--version`.
6. Traverse Buzzheavier folders recursively and deduplicate file IDs.
7. Download one complete file at a time, at available network speed.
8. Run `swissfer -e <email> -d 30 -n 20 <download-directory>` once.
9. On `Code:`, poll MailSlurp for a new matching email and submit its six-character
   alphanumeric code without logging it.
10. Require exit status zero and the final `Download link:`, write results, then
    delete local transfer files and temporary validation cache.

The binary accepts a single directory argument, rather than multiple positional
file arguments. Downloads are flattened into that directory. Unsafe filename
characters are replaced, long names shortened, and collisions get a file-ID prefix.
File upload order follows swissfer's directory enumeration; it is sequential but
is not guaranteed to match the input URL order.

### Download integrity and disk space

There is no application bandwidth cap, fixed download duration, or partial-download
mode. A 30-second connection timeout and 120-second period with less than one byte
per second detect stalled connections; they do not cap normal transfer duration.

The download directory's actual filesystem is checked before the batch, before
each file and before each write. One GiB is reserved for runner bookkeeping.
`df -h` is printed before/after each file. Streamed files are checked against the
Buzzheavier size and/or HTTP Content-Length. Missing authoritative size, truncated
data, size disagreement, insufficient space or any folder/download failure fails
the batch instead of uploading a subset. Temporary files are outside the upload
directory and removed on failure. There is no automatic retry/resume in this MVP.

All files must fit on disk together before swissfer starts. Your observed 109G
free space is sufficient for a 30–50GB batch, but capacity is checked on each run.
The workflow's 360-minute timeout is GitHub's hosted-job maximum, not a promise that
all remote services will finish within six hours.

### Email verification and results

MailSlurp REST requests use `x-api-key`. The inbox must match `SWISS_EMAIL`.
The polling window begins before launching swissfer; known IDs are excluded and
received timestamps, sender `noreply@swisstransfer.com`, recipient and subject
`Swisstransfer confirmation code: XXXXXX` are checked. OTP wait is 120 seconds.
A saturated 100-message window fails rather than selecting from an incomplete list.
Workflow concurrency prevents overlapping runs of this workflow from using the
same inbox; use a dedicated inbox to avoid unrelated concurrent verification requests.

The complete share URL is intentionally printed in Actions logs. On success,
download the `swisstransfer-result` artifact (7-day retention):

- `share-link.txt`: final share URL.
- `result.json`: URL, completion time, configured parameters, filenames and sizes.

Original files, API keys, OTPs and validation tokens are excluded from artifacts.
Raw swissfer output and HTTP exception details are withheld because they can expose
credentials and signed upload URLs. Validation state is isolated per run.
The release binary is fetched over HTTPS and version-checked; no independent
publisher checksum is currently verified.

## Local Linux use and tests

Python 3.14 is used in CI. Install the pinned stable package versions confirmed on
2026-10-14: curl_cffi 0.16.3, requests 2.34.2, pexpect 4.9.0.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
# Set the same environment variables securely, then:
python -m scripts.run
```

The upload process needs Linux/macOS PTYs; production binary installation targets
Linux x64. Windows can run mocked tests, with four actual-PTY tests skipped.

## Operational limits and current validation

- Standard GitHub-hosted runners in public repositories are free for execution,
  but hosted jobs have a six-hour limit. No fixed external-transfer GB quota is
  documented; this is not a guarantee of unlimited bandwidth.
- GitHub-hosted Actions must serve the repository software project's production,
  testing, deployment or publication. Unrelated bulk file relay and excessive
  bandwidth can fall outside GitHub's permitted use.
- SwissTransfer's service limits and refusal/rate-limiting still apply.
  `-d 30` requests 30 days; extension-by-email automation is not implemented.
- Offline/mock tests validate failure handling and parsing. A real Buzzheavier →
  MailSlurp → SwissTransfer transfer remains to be verified with configured secrets.

References:
[GitHub Actions limits](https://docs.github.com/en/actions/reference/limits),
[Actions product terms](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features),
[MailSlurp OpenAPI](https://api.mailslurp.com/v2/api-docs),
[swissfer 3.0.0 source](https://github.com/Blutsh/swissfer/tree/3.0.0).
