"""Installing the gpu-screen-recorder Flatpak from inside the Vice Flatpak.

A Flatpak cannot depend on another app, and the Vice Flatpak records with the
GSR Flatpak on the host, so Vice offers to install it. It goes in system-wide:
GSR's kms-server-proxy only looks for itself under /var/lib/flatpak, so a user
install cannot capture a monitor on Wayland. Flatpak's own polkit rules let a
local wheel user install from a configured remote without a password, which
covers the Steam Deck and Bazzite.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import Awaitable, Callable, Optional

from .recorder import GSR_FLATPAK_APP
from .runtime import host_argv, host_which

log = logging.getLogger("vice.gsr_install")

FLATHUB_URL = "https://dl.flathub.org/repo/flathub.flatpakrepo"
MANUAL_COMMAND = f"flatpak install --system flathub {GSR_FLATPAK_APP}"
_TIMEOUT = 1800

# Colour and cursor codes flatpak writes when it thinks it has a terminal.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STEP = re.compile(r"(?:Installing|Updating|Downloading)\D{0,4}(\d+)\s*/\s*(\d+)", re.I)
_PERCENT = re.compile(r"(\d{1,3})\s*%")

Progress = Callable[[str, Optional[int]], Awaitable[None]]


def parse_progress(text: str, state: dict) -> Optional[int]:
    """Overall percent from a chunk of flatpak install output, or None when the
    chunk says nothing new. flatpak installs the runtime and the app as
    numbered steps ("Installing 1/2"), each with its own percent, so the two
    are folded into one number that only moves forward."""
    for match in _STEP.finditer(text):
        state["step"], state["steps"] = int(match.group(1)), max(int(match.group(2)), 1)
    percents = _PERCENT.findall(text)
    if not percents:
        return None
    pct = min(int(percents[-1]), 100)
    steps = state.get("steps", 1)
    step = min(state.get("step", 1), steps)
    overall = round(((step - 1) + pct / 100) * 100 / steps)
    overall = max(overall, state.get("last", 0))
    state["last"] = overall
    return overall


async def _run(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *host_argv(argv), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    return proc.returncode or 0, out.decode(errors="replace")


async def _ensure_flathub() -> Optional[str]:
    """None when the system has a Flathub remote (adding it if needed), else why not."""
    code, out = await _run(["flatpak", "remotes", "--system", "--columns=url"])
    if code == 0 and "flathub" in out:
        return None
    code, out = await _run(["flatpak", "remote-add", "--system", "--if-not-exists",
                            "flathub", FLATHUB_URL])
    if code != 0:
        return out.strip().splitlines()[-1] if out.strip() else "could not add the Flathub remote"
    return None


async def install_gsr_flatpak(progress: Progress) -> Optional[str]:
    """Install the GSR Flatpak. Returns None on success, else flatpak's own
    reason. *progress* gets (phase, overall percent or None)."""
    await progress("preparing", None)
    try:
        problem = await _ensure_flathub()
    except (OSError, asyncio.TimeoutError) as exc:
        problem = str(exc) or "flatpak did not answer"
    if problem:
        log.warning("Cannot install the gpu-screen-recorder Flatpak: %s", problem)
        return problem

    argv = ["flatpak", "install", "--system", "-y", "flathub", GSR_FLATPAK_APP]
    # flatpak only prints its progress to a terminal, so it gets one from
    # script(1) when the host has it. Without it the card spins instead.
    if await asyncio.to_thread(host_which, "script"):
        argv = ["script", "-qfec", shlex.join(argv), "/dev/null"]
    else:
        argv.insert(4, "--noninteractive")
    log.info("Installing the gpu-screen-recorder Flatpak: %s", " ".join(argv))
    try:
        proc = await asyncio.create_subprocess_exec(
            *host_argv(argv), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as exc:
        return str(exc)

    tail: list[str] = []
    state: dict = {}
    await progress("downloading", None)

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            text = _ANSI.sub("", chunk.decode(errors="replace"))
            for line in re.split(r"[\r\n]+", text):
                if line.strip():
                    tail.append(line.strip())
            del tail[:-12]
            percent = parse_progress(text, state)
            if percent is not None:
                await progress("installing" if percent >= 100 else "downloading", percent)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return "the install took too long and was stopped"
    if proc.returncode != 0:
        reason = next((line for line in reversed(tail) if "error" in line.lower()),
                      tail[-1] if tail else f"flatpak exited with {proc.returncode}")
        log.warning("Installing the gpu-screen-recorder Flatpak failed: %s", reason)
        return reason
    log.info("gpu-screen-recorder Flatpak installed")
    return None
