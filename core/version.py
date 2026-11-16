"""Version and provenance helpers."""

from __future__ import annotations

import functools
import os
import subprocess

__version__ = "0.1.0"

#: Wire protocol identifier.  Bump the trailing integer for incompatible
#: changes; clients refuse to talk to a server with a different major.
PROTOCOL = "samba-mini/2"
PROTOCOL_MAJOR = 2

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@functools.lru_cache(maxsize=1)
def software_revision() -> dict:
    """Return the git revision of the running source tree (best effort).

    Recorded in every data file.  ``dirty`` is True when tracked files have
    uncommitted changes, so a measurement made with modified code is visible.
    """
    info = {"version": __version__, "git_commit": None, "git_dirty": None}
    try:
        commit = subprocess.run(
            ["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if commit.returncode == 0:
            info["git_commit"] = commit.stdout.strip()
            status = subprocess.run(
                ["git", "-C", REPO_ROOT, "status", "--porcelain", "--untracked-files=no"],
                capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if status.returncode == 0:
                info["git_dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def describe() -> str:
    rev = software_revision()
    commit = (rev.get("git_commit") or "unknown")[:10]
    dirty = "+dirty" if rev.get("git_dirty") else ""
    return f"{__version__} ({commit}{dirty})"
