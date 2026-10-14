"""Run bash scripts inside WSL from Windows.

Google's Colab CLI runs only on Linux and macOS, so on Windows MediaStudio drives it
through the default WSL distribution. A script travels in an environment variable
(``WSLENV``) rather than on the command line: wsl.exe re-parses its arguments, and
quotes, spaces or non-Latin letters in paths would not survive that.
Every script runs in a login shell with ``~/.local/bin`` first on PATH, where
``uv`` and the tools it installs live.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 30.0
SCRIPT_VARIABLE = "MEDIASTUDIO_WSL_SCRIPT"
# A fixed command line: the script itself arrives through SCRIPT_VARIABLE.
_RUNNER = f'export PATH="$HOME/.local/bin:$PATH"; eval "${SCRIPT_VARIABLE}"'


class WslError(RuntimeError):
    """WSL is missing or a command inside it could not run."""


@dataclass(frozen=True)
class WslStatus:
    available: bool
    distro: str | None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"available": self.available, "distro": self.distro, "detail": self.detail}


def wsl_executable() -> str | None:
    if sys.platform != "win32":
        return None
    found = shutil.which("wsl.exe")
    if found:
        return found
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"
    return str(system) if system.is_file() else None


def decode_output(raw: bytes) -> str:
    """Linux programs print UTF-8; wsl.exe's own messages may still come as UTF-16LE."""
    if raw and raw.count(b"\x00") > len(raw) // 4:
        try:
            return raw.decode("utf-16-le").replace("\x00", "")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def _environment(script: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    passed = {
        SCRIPT_VARIABLE: script,
        # Plain, wide output from rich/typer: no colors, no wrapping of long lines.
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "400",
        **(extra or {}),
    }
    env.update(passed)
    shared = [item for item in env.get("WSLENV", "").split(":") if item and item.split("/")[0] not in passed]
    env["WSLENV"] = ":".join([*shared, *(f"{name}/u" for name in passed)])
    env["WSL_UTF8"] = "1"
    return env


def _command(executable: str) -> list[str]:
    return [executable, "-e", "bash", "-lc", _RUNNER]


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(
    script: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a bash script in WSL and return its decoded output."""
    executable = wsl_executable()
    if executable is None:
        raise WslError("WSL is not installed.")
    try:
        completed = subprocess.run(
            _command(executable),
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=_environment(script, env),
            creationflags=_creation_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise WslError(f"WSL command did not finish in {timeout:.0f} s.") from exc
    except OSError as exc:
        raise WslError(f"Cannot start WSL: {exc}") from exc
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        decode_output(completed.stdout or b""),
        decode_output(completed.stderr or b""),
    )


def popen(script: str, *, env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    """Start a bash script in WSL with piped stdin, stdout and stderr (streaming callers)."""
    executable = wsl_executable()
    if executable is None:
        raise WslError("WSL is not installed.")
    try:
        return subprocess.Popen(
            _command(executable),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(script, env),
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise WslError(f"Cannot start WSL: {exc}") from exc


def status() -> WslStatus:
    """Whether WSL and a default Linux distribution are there."""
    if wsl_executable() is None:
        return WslStatus(False, None, "WSL is not installed." if sys.platform == "win32" else "Windows only.")
    try:
        result = run('printf "%s" "$WSL_DISTRO_NAME"', timeout=60.0)
    except WslError as exc:
        return WslStatus(False, None, str(exc))
    distro = result.stdout.strip()
    if result.returncode != 0 or not distro:
        detail = (result.stderr or result.stdout).strip() or "No Linux distribution is installed."
        return WslStatus(False, None, detail.splitlines()[-1][:300])
    return WslStatus(True, distro)


def to_wsl_path(path: str | Path) -> str:
    """A Windows path as WSL sees it, through WSL's own wslpath (honours custom mounts)."""
    result = run(f"wslpath -a -u {shlex.quote(str(path))}")
    converted = result.stdout.strip()
    if result.returncode != 0 or not converted.startswith("/"):
        raise WslError(f"WSL cannot reach {path}: {(result.stderr or result.stdout).strip()}")
    return converted


def install_wsl() -> None:
    """Start ``wsl --install`` with elevation; Windows asks for consent and later a restart."""
    import ctypes

    executable = wsl_executable()
    if executable is None:
        raise WslError("wsl.exe is not available on this Windows.")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    code = shell32.ShellExecuteW(None, "runas", executable, "--install", None, 1)
    if int(code) <= 32:
        raise WslError("WSL setup was not started (consent declined or not allowed).")


__all__ = [
    "WslError",
    "WslStatus",
    "decode_output",
    "install_wsl",
    "popen",
    "run",
    "status",
    "to_wsl_path",
    "wsl_executable",
]
