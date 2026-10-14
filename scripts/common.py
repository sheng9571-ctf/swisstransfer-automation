import shutil
import subprocess
from pathlib import Path


class AutomationError(RuntimeError):
    """Safe, user-facing error: never include raw API responses or credentials."""


RESERVE = 1024 ** 3  # Leave room for runner logs and upload bookkeeping.


def check_space(directory: Path, needed: int = 0):
    free = shutil.disk_usage(directory).free
    if free < needed + RESERVE:
        raise AutomationError("Insufficient disk space for download (including 1 GiB reserve)")
    return free


def show_disk(directory: Path):
    subprocess.run(["df", "-h", str(directory)], check=True)


def progress(message):
    # Source filenames are untrusted; never allow GitHub workflow commands.
    print(message.replace("\r", " ").replace("\n", " "), flush=True)
