"""Wait for the GPU before using it, then switch the local service fleet.

Off unless `H3_FLEET_GATE=1`. It exists for one machine's layout: the writer
runs beside ComfyUI and InvokeAI under a Service Dashboard (:8090) that swaps
whole service sets by *preset*. Loading a local LLM while either image server
is rendering would either OOM the render or stall both, and applying the
writer's preset stops those servers -- so a job in flight would simply die.

So before any local-model call:

1. Poll ComfyUI's `/queue` and InvokeAI's queue status. While either has work,
   wait, say so with a silent desktop notification, and expose the counts so
   the studio can show a banner with Cancel / Go ahead. Never cancel a job --
   the user decides. A server that does not answer counts as idle: if it is
   down, there is nothing on it to interrupt.
2. Apply the writer's preset by label, unless it is already selected and the
   LLM endpoint answers. Never start engines directly; the preset owns that.

Inside ComfyUI (the extension build) this must stay off: it would wait on the
very queue it is running inside.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from typing import Any, Callable

import aiohttp

from .models.contract import ModelError

ENABLED = os.environ.get("H3_FLEET_GATE") == "1"
PRESET = os.environ.get("H3_FLEET_PRESET", "Prompt Writer + Generator")
COMFY_QUEUE = os.environ.get("H3_FLEET_COMFY_QUEUE", "http://127.0.0.1:8188/queue")
INVOKE_QUEUE = os.environ.get("H3_FLEET_INVOKE_QUEUE", "http://127.0.0.1:9090/api/v1/queue/default/status")
DASHBOARD = os.environ.get("H3_FLEET_DASHBOARD", "http://127.0.0.1:8090")
# What the studio should connect to once the preset is up: LLM-Redirect, which
# fronts whichever local engine the preset started. A handed-over session's tab
# was opened before that engine existed, so it cannot have connected on its own.
LLM_URL = os.environ.get("H3_FLEET_LLM_URL", "http://127.0.0.1:8892")
# Readiness is checked THROUGH the redirect, not on the engine's own port.
# Measured: the gate saw llama.cpp :8890 healthy, the studio's next call to
# :8892 still got 503 engine_not_set -- Caddy's `fail_duration 10s` remembers
# that the engine was down when the tab probed it at load, for ten more seconds.
LLM_HEALTH = os.environ.get("H3_FLEET_LLM_HEALTH", f"{LLM_URL}/health")
POLL_SECONDS = 5.0
LLM_READY_TIMEOUT = 180.0
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=3)


async def _get_json(session: aiohttp.ClientSession, url: str) -> Any:
    try:
        async with session.get(url, timeout=PROBE_TIMEOUT) as response:
            if response.status != 200:
                return None
            return await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def queue_counts(session: aiohttp.ClientSession) -> dict[str, int]:
    comfy, invoke = await asyncio.gather(_get_json(session, COMFY_QUEUE), _get_json(session, INVOKE_QUEUE))
    comfy_jobs = 0
    if isinstance(comfy, dict):
        comfy_jobs = len(comfy.get("queue_running") or []) + len(comfy.get("queue_pending") or [])
    invoke_jobs = 0
    if isinstance(invoke, dict):
        queue = invoke.get("queue") or {}
        invoke_jobs = int(queue.get("pending") or 0) + int(queue.get("in_progress") or 0)
    return {"comfyui": comfy_jobs, "invokeai": invoke_jobs}


async def llm_ready(session: aiohttp.ClientSession) -> bool:
    try:
        async with session.get(LLM_HEALTH, timeout=PROBE_TIMEOUT) as response:
            return response.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


def describe(counts: dict[str, int]) -> str:
    parts = []
    if counts.get("invokeai"):
        parts.append(f"InvokeAI has {counts['invokeai']} job{'s' if counts['invokeai'] != 1 else ''}")
    if counts.get("comfyui"):
        parts.append(f"ComfyUI has {counts['comfyui']} job{'s' if counts['comfyui'] != 1 else ''}")
    return ", ".join(parts)


class FleetGate:
    def __init__(self) -> None:
        self.phase = "idle"          # idle | waiting | switching
        self.counts: dict[str, int] | None = None
        self.session_id: str | None = None
        self.since: float | None = None
        self.error: str | None = None
        self._decision: str | None = None   # cancel | proceed
        self._wake = asyncio.Event()
        self._notification_id: str | None = None
        # One waiter at a time: the studio's autostart and a generation it
        # triggers both pass through here, and must not overwrite each other's
        # counts or answer each other's Cancel.
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": ENABLED,
            "phase": self.phase,
            "counts": self.counts,
            "message": describe(self.counts or {}),
            "session_id": self.session_id,
            "since": self.since,
            "preset": PRESET,
            "llm_url": LLM_URL if ENABLED else None,
            "error": self.error,
        }

    def decide(self, decision: str) -> bool:
        if self.phase != "waiting" or decision not in {"cancel", "proceed"}:
            return False
        self._decision = decision
        self._wake.set()
        return True

    def _notify(self, summary: str, body: str, *, final: bool = False) -> None:
        """Visual only: the user is audio-sensitive, so the sound hint is suppressed."""
        if not shutil.which("notify-send"):
            return
        command = [
            "notify-send", "--app-name=Prompt Writer", "--print-id",
            "--hint=boolean:suppress-sound:true",
            f"--expire-time={4000 if final else 0}",
        ]
        if self._notification_id:
            command.append(f"--replace-id={self._notification_id}")
        try:
            result = subprocess.run([*command, summary, body], capture_output=True, text=True, timeout=5)
            self._notification_id = (result.stdout or "").strip() or self._notification_id
        except (OSError, subprocess.SubprocessError):
            pass

    async def acquire(self, session_id: str | None, is_cancelled: Callable[[], bool]) -> None:
        if not ENABLED:
            return
        async with self._lock:
            await self._acquire(session_id, is_cancelled)

    async def _acquire(self, session_id: str | None, is_cancelled: Callable[[], bool]) -> None:
        self.session_id = session_id
        self.error = None
        self._decision = None
        self._notification_id = None
        try:
            async with aiohttp.ClientSession() as http:
                await self._wait_for_queues(http, is_cancelled)
                await self._ensure_preset(http, is_cancelled)
        except ModelError as exc:
            # Kept after the wait ends, so a caller polling the handoff sees
            # why it stopped instead of an idle session forever.
            self.error = exc.message
            # The waiting notification never expires on its own; an error after
            # it was shown must replace it, or it claims to wait forever.
            if self._notification_id and exc.code != "GPU_WAIT_CANCELLED":
                self._notify("Prompt Writer could not continue", exc.message, final=True)
            raise
        finally:
            self.phase = "idle"
            self.counts = None
            self.since = None

    async def _wait_for_queues(self, http: aiohttp.ClientSession, is_cancelled: Callable[[], bool]) -> None:
        announced = None
        while True:
            counts = await queue_counts(http)
            if not any(counts.values()) or self._decision == "proceed":
                if announced is not None:
                    self._notify("Prompt Writer continuing", "The GPU is free." if not any(counts.values())
                                 else "Going ahead at your request.", final=True)
                return
            if self._decision == "cancel" or is_cancelled():
                if announced is not None:
                    self._notify("Prompt Writer stopped waiting", "Cancelled.", final=True)
                raise ModelError("GPU_WAIT_CANCELLED", "Stopped waiting for the GPU.", {"counts": counts})
            self.phase = "waiting"
            self.counts = counts
            self.since = self.since or time.time()
            if counts != announced:
                self._notify("Prompt Writer waiting", describe(counts))
                announced = counts
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _ensure_preset(self, http: aiohttp.ClientSession, is_cancelled: Callable[[], bool]) -> None:
        listing = await _get_json(http, f"{DASHBOARD}/api/presets")
        if not isinstance(listing, dict):
            # No dashboard: nothing to switch. If the LLM is down the backend
            # reports that itself, in its own words.
            return
        wanted = " ".join(PRESET.split()).lower()
        preset = next(
            (item for item in listing.get("presets") or [] if " ".join(str(item.get("label", "")).split()).lower() == wanted),
            None,
        )
        if preset is None:
            self.error = f'Service Dashboard has no preset named "{PRESET}".'
            raise ModelError("FLEET_PRESET_MISSING", self.error, {"preset": PRESET})
        if listing.get("selected") == preset.get("id") and await llm_ready(http):
            return
        self.phase = "switching"
        try:
            async with http.post(
                f"{DASHBOARD}/api/presets/apply", json={"label": preset["label"]},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status != 200:
                    detail = (await response.text())[:300]
                    raise ModelError("FLEET_SWITCH_FAILED", f'Applying "{PRESET}" failed: {detail}')
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise ModelError("FLEET_SWITCH_FAILED", f'Applying "{PRESET}" failed: {error}') from error
        deadline = time.monotonic() + LLM_READY_TIMEOUT
        while not await llm_ready(http):
            if is_cancelled():
                raise ModelError("GENERATION_CANCELLED", "Generation was cancelled.")
            if time.monotonic() > deadline:
                raise ModelError("FLEET_LLM_NOT_READY", f"The local LLM did not answer at {LLM_HEALTH} after switching.")
            await asyncio.sleep(2)


GATE = FleetGate()
LOCAL_FAMILIES = frozenset({"gguf", "external", "ollama"})
