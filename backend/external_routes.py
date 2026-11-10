"""Let another local app hand a prompt to the writer and read the result back.

The caller (the Media Generator on :9091) creates a session, opens the studio
on it, and polls for the compiled prompt. Creating a session runs no model --
it only writes the brief, the target and the caller's locked tokens to disk --
so it can never touch the GPU. The studio tab does the work, through the same
build and compile the user would click, with the fleet gate in front of it.

    POST /h3studio/external/session          -> 201 {session_id, mode, variant, url}
    GET  /h3studio/external/session/{id}     -> {status, wait, result}
    POST /h3studio/external/session/{id}/autostarted
    GET  /h3studio/fleet-gate                -> gate snapshot (the banner reads this)
    POST /h3studio/fleet-gate/acquire        -> wait for the GPU, switch preset
    POST /h3studio/fleet-gate/{cancel|proceed}
"""
from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

from . import fleet_gate, session_store
from .fleet_gate import GATE
from .media import CACHE_ROOT, IMAGE_EXTENSIONS, SESSION_MEDIA_MODE, MediaError, STORE
from .models.contract import ModelError
from .targets import TargetError, target_for_mode
from .targets.external_map import resolve

MAX_TOKENS = 64
# A handed-over still, not a video: well under the studio's 1 GB media ceiling,
# and spooled to disk as it arrives rather than held in memory.
MAX_EXTERNAL_IMAGE_BYTES = 100 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
# Browser origins besides the studio's own that may call these routes. The
# Media Generator's page lives on :9091.
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get(
        "H3_EXTERNAL_ORIGINS", "http://127.0.0.1:9091,http://localhost:9091"
    ).split(",")
    if origin.strip()
}


def origin_refusal(request: web.Request) -> str | None:
    """Why this request may not drive a handoff or the GPU gate, or None.

    These routes read a local file by path and can stop another app's render,
    so a web page must not be able to reach them. A loopback Host defeats DNS
    rebinding; an Origin, which every browser sends on a cross-origin POST, must
    be the studio itself or an allowed caller. Server-to-server calls send no
    Origin and pass.
    """
    host = (request.host or "").lower()
    hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if hostname not in LOOPBACK_HOSTS:
        return "Handoff routes answer only on a loopback host."
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin and origin != f"{request.scheme}://{request.host}" and origin not in ALLOWED_ORIGINS:
        return f"Origin {origin} may not use the handoff routes."
    return None


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _tokens(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        token = str(item or "").strip()[:200]
        if token and token not in seen:
            seen.append(token)
    return seen[:MAX_TOKENS]


def result_for(state: dict[str, Any]) -> dict[str, Any] | None:
    mode = (state.get("target") or {}).get("mode")
    record = (state.get("outputs") or {}).get(mode) if mode else None
    if not isinstance(record, dict):
        return None
    best = record.get("best") if isinstance(record.get("best"), dict) else record
    audit = best.get("audit") or {}
    generic_updated_at = (state.get("generic") or {}).get("updated_at")
    return {
        "prompt": best.get("prompt") or "",
        "negative_prompt": best.get("negative_prompt") or "",
        "mode": mode,
        "variant": best.get("variant") or (state.get("target") or {}).get("variant"),
        "audit_ok": not audit.get("repair_required", False),
        "stale": best.get("generic_updated_at") != generic_updated_at,
        "at": best.get("at"),
    }


def register_external_routes(routes, services) -> None:
    prefix = services.ROUTE_PREFIX

    def error(code: str, message: str, status: int, details: Any = None) -> web.Response:
        return services._error(code, message, status=status, details=details)

    def guarded(handler):
        async def wrapper(request: web.Request) -> web.Response:
            refusal = origin_refusal(request)
            if refusal:
                return error("FORBIDDEN_ORIGIN", refusal, 403)
            return await handler(request)
        wrapper.__name__ = handler.__name__
        return wrapper

    async def read_body(request: web.Request) -> tuple[dict[str, Any], tuple[str, Path] | None]:
        if request.content_type.startswith("multipart/"):
            body: dict[str, Any] = {}
            upload: tuple[str, Path] | None = None
            reader = await request.multipart()
            while field := await reader.next():
                if field.name == "image" and field.filename:
                    spool = Path(tempfile.mkstemp(prefix="h3-handoff-")[1])
                    size = 0
                    try:
                        with spool.open("wb") as output:
                            while chunk := await field.read_chunk(1024 * 1024):
                                size += len(chunk)
                                if size > MAX_EXTERNAL_IMAGE_BYTES:
                                    raise MediaError("MEDIA_TOO_LARGE", "A handed-over image cannot exceed 100 MB.")
                                output.write(chunk)
                    except BaseException:
                        spool.unlink(missing_ok=True)
                        raise
                    upload = (field.filename, spool)
                elif field.name == "locked_tokens":
                    body.setdefault("locked_tokens", []).append(await field.text())
                elif field.name:
                    body[field.name] = await field.text()
            return body, upload
        try:
            body = await request.json()
        except Exception as exc:
            raise MediaError("INVALID_REQUEST", "Expected a JSON object or multipart form data.") from exc
        if not isinstance(body, dict):
            raise MediaError("INVALID_REQUEST", "Expected a JSON object.")
        return body, None

    def image_source(body: dict[str, Any], upload: tuple[str, Path] | None) -> tuple[str, Path] | None:
        if upload is not None:
            return upload
        raw = _text(body.get("image_path"), 4096)
        if not raw:
            return None
        # Resolved, so the suffix checked is the real file's and not a symlink's.
        path = Path(raw).resolve()
        if not path.is_file():
            raise MediaError("IMAGE_NOT_FOUND", "image_path is not a readable file.")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise MediaError("UNSUPPORTED_MEDIA", "image_path must be an image (jpg, png, webp, bmp, tif).")
        if path.stat().st_size > MAX_EXTERNAL_IMAGE_BYTES:
            raise MediaError("MEDIA_TOO_LARGE", "A handed-over image cannot exceed 100 MB.")
        return path.name, path

    async def attach(session_id: str, source: tuple[str, Path]) -> None:
        filename, content = source
        if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
            raise MediaError("UNSUPPORTED_MEDIA", "The image must be jpg, png, webp, bmp or tif.")
        if not services._claim_media_mutation():
            raise MediaError("GENERATION_BUSY", "The writer is busy; try the handoff again in a moment.")
        asset_dir = CACHE_ROOT / session_id / str(uuid4())
        try:
            asset_dir.mkdir(parents=True, exist_ok=False)
            stored = asset_dir / f"original{Path(filename).suffix.lower()}"
            shutil.copyfile(content, stored)
            prepared, cancellation = await services._run_thread_worker(
                STORE.prepare_add, session_id, SESSION_MEDIA_MODE, filename,
                mimetypes.guess_type(filename)[0], stored,
            )
            if cancellation is not None:
                raise cancellation
            STORE.commit_add(session_id, SESSION_MEDIA_MODE, prepared)
        except BaseException:
            shutil.rmtree(asset_dir, ignore_errors=True)
            raise
        finally:
            services._release_media_mutation()

    @routes.post(f"{prefix}/external/session")
    @guarded
    async def create_session(request: web.Request) -> web.Response:
        body: dict[str, Any] = {}
        upload: tuple[str, Path] | None = None
        try:
            try:
                body, upload = await read_body(request)
                prompt = _text(body.get("prompt"), 8000)
                if not prompt:
                    return error("INVALID_REQUEST", "prompt is required.", 400, {"field": "prompt"})
                source = image_source(body, upload)
                mode, variant = resolve(
                    _text(body.get("base"), 64),
                    variant=_text(body.get("variant"), 64) or None,
                    family=_text(body.get("family"), 64) or None,
                    image_count=1 if source else 0,
                )
                target = target_for_mode(mode)
                if variant is not None and variant not in target.variants:
                    raise TargetError("INVALID_VARIANT", f"{target.label} has no variant {variant!r}.")
            except TargetError as exc:
                return error(exc.code, exc.message, 422, {"base": body.get("base")})
            except MediaError as exc:
                return error(exc.code, str(exc), 400)
            warnings: list[str] = []
            tokens = _tokens(body.get("locked_tokens"))
            if tokens and target.workspace != "image":
                # H3's sectioned prompt has no tag list to append a trigger word
                # to; say so rather than accept a promise nothing keeps.
                warnings.append(f"{target.label} prompts do not carry LoRA tokens; locked_tokens were ignored.")
                tokens = []
            session_id = str(uuid4())
            if source is not None:
                try:
                    await attach(session_id, source)
                except MediaError as exc:
                    return error(exc.code, str(exc), 409 if exc.code == "GENERATION_BUSY" else 400)
        finally:
            if upload is not None:
                upload[1].unlink(missing_ok=True)
        state = session_store.new_state(session_id)
        state["inputs"]["brief"] = prompt
        state["target"] = {"mode": mode, "variant": variant}
        state["external"] = {
            "source": _text(body.get("source"), 64) or "external",
            "base": _text(body.get("base"), 64),
            "family": _text(body.get("family"), 64),
            "negative": _text(body.get("negative"), 8000),
            "locked_tokens": tokens,
            "autostarted": False,
            "created_at": time.time(),
        }
        session_store.save(state)
        url = f"{request.scheme}://{request.host}/?session={session_id}&autostart=1"
        return web.json_response(
            {"session_id": session_id, "mode": mode, "variant": variant, "url": url, "warnings": warnings},
            status=201,
        )

    @routes.get(f"{prefix}/external/session/{{session_id}}")
    @guarded
    async def session_status(request: web.Request) -> web.Response:
        try:
            state = session_store.load(request.match_info["session_id"])
        except session_store.SessionStoreError as exc:
            return error(exc.code, exc.message, 400)
        if not state.get("external"):
            return error("SESSION_NOT_FOUND", "No handed-over session with that id.", 404)
        session_id = state["session_id"]
        gate = GATE.snapshot()
        mine = gate["session_id"] == session_id
        with services.STATE_LOCK:
            running = (
                services.STATE.get("active_request_id") is not None
                and services.STATE.get("active_session_id") == session_id
            )
        result = result_for(state)
        external = state["external"]
        # The newest failure wins over an older result, and a newer result over it.
        failure, failed_at = external.get("error"), external.get("error_at") or 0
        if mine and gate["error"] and not failure:
            failure, failed_at = gate["error"], time.time()
        if mine and gate["phase"] == "waiting":
            status = "waiting_gpu"
        elif mine and gate["phase"] == "switching":
            status = "switching"
        elif running:
            status = "running"
        elif failure and failed_at >= ((result or {}).get("at") or 0):
            status = "error"
        elif result:
            status = "ready"
        else:
            status = "idle"
        return web.json_response({
            "session_id": session_id,
            "status": status,
            "wait": gate["counts"] if status == "waiting_gpu" else None,
            "error": failure if status == "error" else None,
            "result": result,
        })

    @routes.post(f"{prefix}/external/session/{{session_id}}/error")
    @guarded
    async def record_error(request: web.Request) -> web.Response:
        """The studio reports a handoff that stopped (cancelled wait, failed build)."""
        try:
            state = session_store.load(request.match_info["session_id"])
            body = await request.json()
        except session_store.SessionStoreError as exc:
            return error(exc.code, exc.message, 400)
        except Exception:
            body = {}
        if not state.get("external"):
            return error("SESSION_NOT_FOUND", "No handed-over session with that id.", 404)
        state["external"]["error"] = _text((body or {}).get("message"), 500) or "The handoff stopped."
        state["external"]["error_at"] = time.time()
        session_store.save(state)
        return web.json_response({"ok": True})

    @routes.post(f"{prefix}/external/session/{{session_id}}/autostarted")
    @guarded
    async def mark_autostarted(request: web.Request) -> web.Response:
        """Autostart runs once per handoff; a reload of the tab must not rebuild."""
        try:
            state = session_store.load(request.match_info["session_id"])
        except session_store.SessionStoreError as exc:
            return error(exc.code, exc.message, 400)
        external = state.get("external")
        if not external:
            return error("SESSION_NOT_FOUND", "No handed-over session with that id.", 404)
        first = not external.get("autostarted")
        external["autostarted"] = True
        session_store.save(state)
        return web.json_response({"first": first})

    if not fleet_gate.ENABLED:
        # The ComfyUI extension and a plain Standalone have no fleet to wait on;
        # the studio hides the banner when these 404.
        return

    @routes.get(f"{prefix}/fleet-gate")
    @guarded
    async def gate_status(_request: web.Request) -> web.Response:
        return web.json_response(GATE.snapshot())

    @routes.post(f"{prefix}/fleet-gate/acquire")
    @guarded
    async def gate_acquire(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            await GATE.acquire(str((body or {}).get("session_id") or "") or None, lambda: False)
        except ModelError as exc:
            status = 499 if exc.code == "GPU_WAIT_CANCELLED" else 503
            return error(exc.code, exc.message, status, exc.details)
        return web.json_response(GATE.snapshot())

    @routes.post(f"{prefix}/fleet-gate/{{decision}}")
    @guarded
    async def gate_decide(request: web.Request) -> web.Response:
        decision = request.match_info["decision"]
        if decision not in {"cancel", "proceed"}:
            return error("INVALID_REQUEST", "Decision must be cancel or proceed.", 400)
        return web.json_response({"applied": GATE.decide(decision), **GATE.snapshot()})
