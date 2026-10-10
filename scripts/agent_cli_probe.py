"""Probe how Claude Code and Antigravity (agy) accept long prompts and images.

Run from a normal terminal with network access (not from a sandbox):

    python scripts/agent_cli_probe.py            # all probes
    python scripts/agent_cli_probe.py claude     # only Claude Code
    python scripts/agent_cli_probe.py agy        # only Antigravity

Each probe runs in a fresh temporary folder and prints PASS/FAIL with the
first lines of output. The long probes send ~60 KB, which is over the cmd.exe
(8191) and CreateProcess (32767) command-line limits, so they only pass when
the prompt really travels through stdin or a file. See
docs/CREATE-KREA2-PLAN.md (section 7.4).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "app" / "ai" / "assets" / "cmv-vision-test-garden.jpg"
TIMEOUT = 240
LONG_MARKER = "CMV_LONG_OK"


def long_prompt() -> str:
    filler = "\n".join(
        f"Reference line {index:04d}: neutral filler text used to test long input delivery."
        for index in range(720)
    )
    return (
        "The text below is filler that tests long input delivery. Ignore its content.\n\n"
        f"{filler}\n\n"
        f"Final task: reply with exactly {LONG_MARKER} and no other text. Do not use tools."
    )


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        if name == "CLAUDECODE" or name.startswith("CLAUDE_CODE_"):
            env.pop(name)
    env.setdefault("NO_COLOR", "1")
    return env


def run(label: str, args: list[str], *, cwd: Path, stdin_text: str | None, expect: str | None):
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            env=clean_env(),
            input=stdin_text if stdin_text is not None else None,
            stdin=None if stdin_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"[FAIL] {label}: timeout after {TIMEOUT}s")
        return
    except OSError as exc:
        print(f"[FAIL] {label}: cannot start: {exc}")
        return
    elapsed = time.monotonic() - started
    out = completed.stdout.strip()
    ok = completed.returncode == 0 and bool(out) and (expect is None or expect in out)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: rc={completed.returncode} {elapsed:.1f}s")
    print("    stdout:", out[:600].replace("\n", "\n            "))
    if completed.stderr.strip():
        print("    stderr:", completed.stderr.strip()[:600].replace("\n", "\n            "))


def probe_claude(exe: str) -> None:
    with tempfile.TemporaryDirectory(prefix="cmv-probe-claude-") as temp:
        work = Path(temp)
        base = [exe, "--print", "--output-format", "json", "--no-session-persistence"]
        run(
            "claude long prompt via stdin, no tools",
            [*base, "--tools", ""],
            cwd=work,
            stdin_text=long_prompt(),
            expect=LONG_MARKER,
        )
        shutil.copyfile(IMAGE, work / "input.jpg")
        run(
            "claude image via Read tool in the run folder",
            [*base, "--tools", "Read", "--allowedTools", "Read"],
            cwd=work,
            stdin_text=(
                "Use the Read tool to open the image file input.jpg in the current working "
                "directory. Then describe its main subject in at most eight words."
            ),
            expect=None,
        )


def probe_agy(exe: str) -> None:
    common = ["--dangerously-skip-permissions", "--sandbox"]
    with tempfile.TemporaryDirectory(prefix="cmv-probe-agy-") as temp:
        work = Path(temp)
        run(
            "agy long prompt via stdin with --print and no value",
            [exe, "--print", *common],
            cwd=work,
            stdin_text=long_prompt(),
            expect=LONG_MARKER,
        )
        run(
            "agy long prompt via stdin with a short --print prompt",
            [exe, "--print", "Follow the task given on standard input.", *common],
            cwd=work,
            stdin_text=long_prompt(),
            expect=LONG_MARKER,
        )
        (work / "cmv-task.md").write_text(long_prompt(), encoding="utf-8")
        run(
            "agy long prompt via task file in the run folder",
            [
                exe,
                "--print",
                "Read the file cmv-task.md in the current working directory in full and "
                "follow its final task exactly.",
                *common,
            ],
            cwd=work,
            stdin_text=None,
            expect=LONG_MARKER,
        )
        shutil.copyfile(IMAGE, work / "input.jpg")
        run(
            "agy image file in the run folder",
            [
                exe,
                "--print",
                "Open the image file input.jpg in the current working directory and describe "
                "its main subject in at most eight words.",
                *common,
            ],
            cwd=work,
            stdin_text=None,
            expect=None,
        )


def main() -> int:
    wanted = set(sys.argv[1:]) or {"claude", "agy"}
    if "claude" in wanted:
        exe = shutil.which("claude")
        print(f"== Claude Code: {exe}")
        if exe:
            probe_claude(exe)
    if "agy" in wanted:
        exe = shutil.which("agy") or shutil.which("antigravity")
        print(f"== Antigravity: {exe}")
        if exe:
            probe_agy(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
