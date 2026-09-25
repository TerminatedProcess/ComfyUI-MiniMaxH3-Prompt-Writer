"""Routes for the generic prompt stage: build it, talk to it, keep it.

These sit in front of the same runtime as `/generate` -- one backend lease, one
model, one request at a time -- but they run with the single_call policy, because
what comes back is a JSON document, not a prompt to audit.

Everything they touch persists until Reset (see `session_store`).
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from . import conversation, generic, goals as goal_ledger, session_store
from .assembly import _media_inputs
from .media import SESSION_MEDIA_MODE, STORE, MediaError, parse_session_id
from .models.contract import ModelError
from .targets import DEFAULT_MODE, TargetError, WORKSPACES, catalog, target_for_mode


def _session_id(body: dict[str, Any]) -> str:
    """The session this request belongs to. Missing is an error, not a new one.

    `parse_session_id` mints a fresh id for an empty value, which is right when
    media is being uploaded for the first time and wrong here: silently writing a
    document into a session the caller has no handle on loses the work.
    """
    if not body.get("session_id"):
        raise session_store.SessionStoreError("INVALID_SESSION", "A session ID is required.")
    try:
        return parse_session_id(body["session_id"])
    except ValueError as error:
        raise session_store.SessionStoreError("INVALID_SESSION", "The session ID is invalid.") from error


def _manifest(session_id: str) -> dict[str, Any]:
    try:
        return STORE.manifest(session_id, SESSION_MEDIA_MODE)
    except MediaError:
        return {"session_id": session_id, "mode": SESSION_MEDIA_MODE, "assets": [], "valid": True}


def _flags(state: dict[str, Any], body: dict[str, Any]) -> tuple[bool, bool]:
    inputs = state.get("inputs", {})
    nsfw = body.get("nsfw", inputs.get("nsfw", True))
    story = body.get("story", inputs.get("story", True))
    return bool(nsfw), bool(story)


def register_generic_routes(routes, services) -> None:
    prefix = services.ROUTE_PREFIX

    def error(err: Any, status: int = 400) -> web.Response:
        return services._error(
            getattr(err, "code", "INVALID_REQUEST"),
            getattr(err, "message", str(err)),
            status=status,
            details=getattr(err, "details", None),
        )

    async def _single_call(body: dict[str, Any], assembled: dict[str, Any]) -> str:
        """Run one non-audited completion on the selected prompt model."""
        request_id = services._claim_generation_request()
        if request_id is None:
            raise ModelError("GENERATION_BUSY", "Another H3 Prompt Writer request is already running.")
        try:
            model, backend, runtime_plan = await services._prepare_generation_runtime(body, assembled, request_id)
            result, cancellation = await services._run_thread_worker(
                backend.generate,
                model,
                assembled,
                body["session_id"],
                on_cancel=backend.cancel,
                thinking=False,
                seed=body.get("seed"),
                unload_after=body.get("unload_after", True),
                context_profile=body.get("context_profile", "auto"),
                kv_cache=body.get("kv_cache", "auto"),
                runtime_plan=runtime_plan,
                on_phase=lambda phase: services._set_request_phase(request_id, phase),
            )
            services._propagate_worker_cancellation(cancellation)
            return result.get("prompt") if isinstance(result, dict) else ""
        finally:
            services._release_generation_request(request_id)

    def _state_response(state: dict[str, Any], session_id: str, **extra: Any) -> web.Response:
        manifest = _manifest(session_id)
        state["media_snapshot"] = session_store.media_snapshot(manifest)
        persisted = True
        try:
            session_store.save(state)
        except (OSError, session_store.SessionStoreError):
            # A read-only install (a packaged ComfyUI custom_nodes directory) must
            # not break the writer: the work is still returned and usable for this
            # session, and the response says plainly that it will not survive.
            persisted = False
        return web.json_response({
            "session": session_store.public(state, attached=manifest.get("assets", [])),
            "persisted": persisted,
            **extra,
        })

    @routes.get(f"{prefix}/targets")
    async def get_targets(_request: web.Request) -> web.Response:
        """The registry, as JSON. The studio builds its whole UI from this."""
        return web.json_response({
            "targets": catalog(),
            "workspaces": list(WORKSPACES),
            "default_mode": DEFAULT_MODE,
            "session_media_mode": SESSION_MEDIA_MODE,
            "generic": {
                "fields": list(generic.FIELDS),
                "labels": dict(generic.LABELS),
                "groups": [{"title": title, "fields": list(keys)} for title, keys in generic.GROUPS],
                "origins": list(generic.ORIGINS),
            },
            "goal_kinds": list(goal_ledger.KINDS),
        })

    @routes.get(f"{prefix}/session")
    async def get_session(request: web.Request) -> web.Response:
        try:
            session_id = parse_session_id(request.query.get("session_id"))
        except ValueError:
            return services._error("INVALID_SESSION", "The session ID is invalid.", status=400)
        state = session_store.load(session_id)
        manifest = _manifest(session_id)
        return web.json_response({
            "session": session_store.public(state, attached=manifest.get("assets", [])),
        })

    @routes.post(f"{prefix}/session/inputs")
    async def put_inputs(request: web.Request) -> web.Response:
        body = await services._json_body(request)
        if body is None:
            return services._error("INVALID_REQUEST", "Expected a JSON object.", status=400)
        try:
            session_id = _session_id(body)
        except session_store.SessionStoreError as err:
            return error(err)
        state = session_store.load(session_id)
        inputs = dict(state["inputs"])
        if "brief" in body:
            inputs["brief"] = str(body.get("brief") or "")[:8000]
        if "duration_seconds" in body:
            duration = body["duration_seconds"]
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 0 < duration <= 20:
                return services._error("INVALID_DURATION", "Duration must be between 1 and 20 seconds.", status=400)
            inputs["duration_seconds"] = duration
        if "aspect_ratio" in body:
            # Validated on the way in as well as at compile time: this value is
            # persisted and interpolated into the writer's own user message.
            from .assembly import ASPECT_RATIOS

            if body["aspect_ratio"] not in ASPECT_RATIOS:
                return services._error("INVALID_ASPECT_RATIO", "The selected aspect ratio is not supported.", status=400)
            inputs["aspect_ratio"] = body["aspect_ratio"]
        for flag in ("nsfw", "story"):
            if flag in body:
                if not isinstance(body[flag], bool):
                    return services._error("INVALID_REQUEST", f"{flag} must be a boolean.", status=400)
                inputs[flag] = body[flag]
        state["inputs"] = inputs
        if "mode" in body or "variant" in body:
            mode = body.get("mode") or state["target"].get("mode")
            if mode is not None:
                try:
                    target = target_for_mode(mode)
                except TargetError as err:
                    return error(err)
                variant = body.get("variant", state["target"].get("variant"))
                if target.variants and variant not in target.variants:
                    variant = target.default_variant
                state["target"] = {"mode": mode, "variant": variant if target.variants else None}
        return _state_response(state, session_id)

    @routes.post(f"{prefix}/session/reset")
    async def reset_session(request: web.Request) -> web.Response:
        body = await services._json_body(request) or {}
        try:
            session_id = _session_id(body)
        except session_store.SessionStoreError as err:
            return error(err)
        busy = services._generation_busy_error()
        if busy is not None:
            return busy
        state = session_store.reset(session_id)
        if body.get("clear_media", True):
            try:
                STORE.clear_mode(session_id, SESSION_MEDIA_MODE)
            except MediaError:
                pass
        return _state_response(state, session_id, reset=True)

    @routes.post(f"{prefix}/generic/build")
    async def build_generic(request: web.Request) -> web.Response:
        body = await services._json_body(request)
        if body is None:
            return services._error("INVALID_REQUEST", "Expected a JSON object.", status=400)
        if not body.get("model_id") and not (body.get("api_provider") or body.get("ollama_model") or body.get("external_server")):
            return services._error("INVALID_REQUEST", "Required fields are missing.", status=400, details={"fields": ["model_id"]})
        try:
            session_id = _session_id(body)
        except session_store.SessionStoreError as err:
            return error(err)
        body["session_id"] = session_id
        state = session_store.load(session_id)
        nsfw, story = _flags(state, body)
        brief = str(body.get("brief") if body.get("brief") is not None else state["inputs"].get("brief") or "").strip()
        manifest = _manifest(session_id)
        if not brief and not manifest.get("assets"):
            return services._error(
                "INVALID_REQUEST",
                "Add a brief or a reference image before building the generic prompt.",
                status=400,
                details={"field": "brief"},
            )
        state["inputs"] = {**state["inputs"], "brief": brief[:8000], "nsfw": nsfw, "story": story}
        # A rebuild keeps what the user has already fixed by hand or in
        # conversation; only invented and unspecified fields are rewritten.
        doc = state["generic"] if body.get("keep_established", True) else generic.new_doc()
        assembled = conversation.assemble_build(
            session_id=session_id,
            brief=brief,
            manifest=manifest,
            media_inputs=_media_inputs(manifest.get("assets", [])),
            doc=doc,
            goals=state["goals"],
            nsfw=nsfw,
            story=story,
            duration_seconds=state["inputs"].get("duration_seconds"),
            aspect_ratio=state["inputs"].get("aspect_ratio"),
        )
        retried = False
        try:
            text = await _single_call(body, assembled)
            try:
                updated, changed, protected = conversation.apply_build(
                    doc, text, brief=brief, has_media=bool(manifest.get("assets")),
                )
            except conversation.ConversationError:
                # One retry: a document is minutes of the user's attention and
                # an unusable answer is usually a sampling flake, not a refusal.
                # The second failure is reported with the model's own words.
                retried = True
                text = await _single_call(body, assembled)
                updated, changed, protected = conversation.apply_build(
                    doc, text, brief=brief, has_media=bool(manifest.get("assets")),
                )
        except ModelError as err:
            status = 499 if err.code == "GENERATION_CANCELLED" else services._model_error_status(err)
            return services._error(err.code, err.message, status=status, details=err.details)
        except (conversation.ConversationError, generic.GenericError) as err:
            return error(err, status=502)
        # Anything the user changed while the model was working stays changed.
        state = session_store.merge_after_await(session_id, state, "generic", "goals", "conversation")
        state["generic"] = updated
        session_store.record_turn(
            state,
            "assistant",
            "Built the generic prompt from your brief"
            + (" and reference media." if manifest.get("assets") else ".")
            + (" The first answer was unusable, so it was written again." if retried else ""),
            changed=changed,
            protected=protected,
        )
        return _state_response(
            state, session_id, changed=list(changed), protected=list(protected), retried=retried,
        )

    @routes.post(f"{prefix}/generic/turn")
    async def generic_turn(request: web.Request) -> web.Response:
        body = await services._json_body(request)
        if body is None:
            return services._error("INVALID_REQUEST", "Expected a JSON object.", status=400)
        message = str(body.get("message") or "").strip()
        if not message:
            return services._error("INVALID_REQUEST", "Say what should change.", status=400, details={"field": "message"})
        try:
            session_id = _session_id(body)
        except session_store.SessionStoreError as err:
            return error(err)
        body["session_id"] = session_id
        state = session_store.load(session_id)
        if generic.is_empty(state["generic"]):
            return services._error(
                "GENERIC_PROMPT_EMPTY",
                "Generate the generic prompt first, then steer it here.",
                status=409,
            )
        nsfw, story = _flags(state, body)
        manifest = _manifest(session_id)
        session_store.record_turn(state, "user", message)
        assembled = conversation.assemble_turn(
            session_id=session_id,
            message=message,
            doc=state["generic"],
            goals=state["goals"],
            conversation=state["conversation"],
            manifest=manifest,
            media_inputs=_media_inputs(manifest.get("assets", [])),
            nsfw=nsfw,
            story=story,
        )
        try:
            text = await _single_call(body, assembled)
            outcome = conversation.apply_turn(
                state["generic"],
                state["goals"],
                text,
                has_media=bool(manifest.get("assets")),
                message=message,
            )
        except ModelError as err:
            session_store.save(state)
            status = 499 if err.code == "GENERATION_CANCELLED" else services._model_error_status(err)
            return services._error(err.code, err.message, status=status, details=err.details)
        except (conversation.ConversationError, generic.GenericError, goal_ledger.GoalError) as err:
            session_store.save(state)
            return error(err, status=502)
        state = session_store.merge_after_await(session_id, state, "conversation")
        state["generic"] = outcome["generic"]
        state["goals"] = outcome["goals"]
        session_store.record_turn(
            state,
            "assistant",
            outcome["reply"],
            changed=outcome["changed"],
            protected=outcome["protected"],
            goals_added=outcome["goals_added"],
        )
        return _state_response(
            state,
            session_id,
            reply=outcome["reply"],
            changed=list(outcome["changed"]),
            protected=list(outcome["protected"]),
            goals_added=list(outcome["goals_added"]),
        )

    @routes.post(f"{prefix}/generic/field")
    async def edit_field(request: web.Request) -> web.Response:
        """Inline edit: the escape hatch beside the conversation, no model call."""
        body = await services._json_body(request)
        if body is None:
            return services._error("INVALID_REQUEST", "Expected a JSON object.", status=400)
        try:
            session_id = _session_id(body)
        except session_store.SessionStoreError as err:
            return error(err)
        field = body.get("field")
        if field not in generic.FIELDS:
            return services._error("UNKNOWN_FIELD", "That field is not part of the generic prompt.", status=400)
        state = session_store.load(session_id)
        value = body.get("value")
        try:
            if value is None or not str(value).strip():
                state["generic"] = generic.clear_field(state["generic"], field)
                changed: tuple[str, ...] = ()
            else:
                state["generic"], changed, _protected = generic.apply_patch(
                    state["generic"], {field: str(value)}, source=generic.SOURCE_USER
                )
        except generic.GenericError as err:
            return error(err)
        return _state_response(state, session_id, changed=list(changed))

    @routes.post(f"{prefix}/goals")
    async def add_goal(request: web.Request) -> web.Response:
        body = await services._json_body(request)
        if body is None:
            return services._error("INVALID_REQUEST", "Expected a JSON object.", status=400)
        try:
            session_id = _session_id(body)
        except session_store.SessionStoreError as err:
            return error(err)
        state = session_store.load(session_id)
        action = str(body.get("action") or "add")
        try:
            if action == "add":
                # Coerced rather than trusted: a number in `fields` used to reach
                # the ledger as a TypeError and come back as an HTTP 500.
                fields = body.get("fields")
                includes = body.get("must_include")
                goal = goal_ledger.new_goal(
                    str(body.get("text") or ""),
                    str(body.get("kind") or goal_ledger.KIND_JUDGED),
                    fields=tuple(str(item) for item in fields) if isinstance(fields, list) else (),
                    must_include=tuple(str(item) for item in includes) if isinstance(includes, list) else (),
                )
                state["goals"], added = goal_ledger.merge(state["goals"], [goal])
                if not added:
                    # merge() skips a repeat and stops at the cap. The studio
                    # clears the box before the request, so saying nothing loses
                    # the user's text and shows an unchanged list.
                    reason = (
                        "That goal is already in the ledger."
                        if len(state["goals"]) < goal_ledger.MAX_GOALS
                        else f"The ledger is full at {goal_ledger.MAX_GOALS} goals. Delete one first."
                    )
                    return services._error("GOAL_NOT_ADDED", reason, status=409, details={"text": goal["text"]})
            elif action == "toggle":
                state["goals"] = goal_ledger.toggle(state["goals"], str(body.get("id")), bool(body.get("enabled", True)))
            elif action == "delete":
                state["goals"] = goal_ledger.remove(state["goals"], str(body.get("id")))
            else:
                return services._error("INVALID_REQUEST", "Unknown goal action.", status=400)
        except goal_ledger.GoalError as err:
            return error(err)
        return _state_response(state, session_id)
