"""The generic-stage endpoints, driven the way the studio drives them.

One fake model answer per call, so what is under test is the wiring: what gets
persisted, what the studio is told, and what happens when the model's answer is
unusable.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeRoutes:
    def get(self, _path):
        return lambda function: function

    post = get
    delete = get


sys.modules.setdefault(
    "server",
    types.SimpleNamespace(
        PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=_FakeRoutes()))
    ),
)

from backend import generic, goals, routes, session_store  # noqa: E402
from backend.generic_routes import register_generic_routes  # noqa: E402
from backend.models.contract import ModelError  # noqa: E402

SESSION = "11111111-2222-4333-8444-555555555555"


class _Recorder:
    """Captures the closures `register_generic_routes` installs."""

    def __init__(self):
        self.handlers = {}

    def _record(self, method):
        def register(path):
            def decorator(function):
                self.handlers[(method, path)] = function
                return function
            return decorator
        return register

    def __getattr__(self, name):
        if name in {"get", "post", "delete"}:
            return self._record(name.upper())
        raise AttributeError(name)


class _Request:
    def __init__(self, body=None, query=None):
        self._body = body
        self.query = query or {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class GenericRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state_root = patch.object(session_store, "STATE_ROOT", Path(self.temp.name))
        state_root.start()
        self.addCleanup(state_root.stop)

        self.recorder = _Recorder()
        register_generic_routes(self.recorder, routes)
        self.answers: list[str] = []
        self.model_error: ModelError | None = None

        async def fake_prepare(_body, _assembled, _request_id):
            if self.model_error is not None:
                raise self.model_error
            return {"id": "m", "family": "gguf", "name": "m"}, _Backend(), {"max_output_tokens": 2048}

        async def fake_worker(function, *args, on_cancel=None, **kwargs):
            return function(*args, **kwargs), None

        patches = [
            patch.object(routes, "_prepare_generation_runtime", fake_prepare),
            patch.object(routes, "_run_thread_worker", fake_worker),
            patch.object(routes, "_claim_generation_request", lambda: "request-1"),
            patch.object(routes, "_release_generation_request", lambda _request_id: None),
            patch.object(routes, "_set_request_phase", lambda _request_id, _phase: None),
            patch.object(routes, "_propagate_worker_cancellation", lambda _cancellation: None),
            patch.object(routes, "_generation_busy_error", lambda: None),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        test = self

        class _Backend:
            def generate(self, _model, _assembled, _session_id, **_kwargs):
                return {"prompt": test.answers.pop(0) if test.answers else ""}

            def cancel(self):
                return True

        self._backend_class = _Backend

    def call(self, method, path, body=None, query=None):
        handler = self.recorder.handlers[(method, f"/h3studio{path}")]
        response = asyncio.run(handler(_Request(body=body, query=query)))
        return response.status, json.loads(response.body.decode())

    # -- read-only surface ------------------------------------------------

    def test_the_target_catalog_is_served_for_the_studio(self):
        status, payload = self.call("GET", "/targets")
        self.assertEqual(status, 200)
        self.assertEqual({item["id"] for item in payload["targets"]}, {"h3", "krea2", "anima", "music3"})
        self.assertEqual(payload["workspaces"], ["image", "video", "music"])
        self.assertEqual(payload["generic"]["fields"], list(generic.FIELDS))
        self.assertIn("judged", payload["goal_kinds"])

    def test_a_fresh_session_reports_an_empty_document(self):
        status, payload = self.call("GET", "/session", query={"session_id": SESSION})
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"]["unspecified"], list(generic.FIELDS))
        self.assertTrue(payload["session"]["inputs"]["story"])

    def test_a_read_only_state_directory_does_not_break_the_writer(self):
        """A packaged ComfyUI install can be read-only; the work still comes back."""
        with patch.object(session_store, "STATE_ROOT", Path(self.temp.name) / "missing" / "nested"):
            with patch("backend.session_store.os.replace", side_effect=OSError("read-only file system")):
                status, payload = self.call("POST", "/session/inputs", {"session_id": SESSION, "brief": "a brief"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["persisted"])
        self.assertEqual(payload["session"]["inputs"]["brief"], "a brief")

    def test_a_missing_session_id_is_refused_rather_than_invented(self):
        """A generated id would write the document where the caller cannot find it."""
        for path in ("/session/inputs", "/generic/build", "/generic/turn", "/generic/field", "/goals", "/session/reset"):
            status, payload = self.call("POST", path, {"model_id": "m", "message": "x", "field": "mood", "text": "x"})
            self.assertEqual(status, 400, path)
            self.assertEqual(payload["error"]["code"], "INVALID_SESSION", path)

    def test_a_bad_session_id_is_refused(self):
        status, payload = self.call("GET", "/session", query={"session_id": "../etc"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_SESSION")

    # -- inputs and reset ------------------------------------------------

    def test_inputs_persist_including_the_selected_target(self):
        status, payload = self.call("POST", "/session/inputs", {
            "session_id": SESSION,
            "brief": "a tram stop at blue hour",
            "duration_seconds": 8,
            "story": False,
            "mode": "Anima",
            "variant": "aesthetic",
        })
        self.assertEqual(status, 200)
        inputs = payload["session"]["inputs"]
        self.assertEqual(inputs["brief"], "a tram stop at blue hour")
        self.assertEqual(inputs["duration_seconds"], 8)
        self.assertFalse(inputs["story"])
        self.assertEqual(payload["session"]["target"], {"mode": "Anima", "variant": "aesthetic"})

    def test_an_invalid_variant_falls_back_to_the_targets_default(self):
        _status, payload = self.call("POST", "/session/inputs", {
            "session_id": SESSION, "mode": "Anima", "variant": "ultra",
        })
        self.assertEqual(payload["session"]["target"]["variant"], "turbo")

    def test_a_duration_outside_the_range_is_refused(self):
        status, payload = self.call("POST", "/session/inputs", {"session_id": SESSION, "duration_seconds": 45})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_DURATION")

    def test_reset_clears_the_document_the_goals_and_the_conversation(self):
        self.answers.append(json.dumps({"scene": {"subject": "Bob", "mood": "wistful"}}))
        self.call("POST", "/generic/build", {"session_id": SESSION, "model_id": "m", "brief": "Bob waits"})
        status, payload = self.call("POST", "/session/reset", {"session_id": SESSION})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reset"])
        self.assertEqual(payload["session"]["unspecified"], list(generic.FIELDS))
        self.assertEqual(payload["session"]["conversation"], [])

    # -- build -----------------------------------------------------------

    def test_build_stores_the_document_and_logs_the_turn(self):
        self.answers.append(json.dumps({"scene": {
            "subject": "Bob, a man in his late 30s",
            "location": "his living room",
            "lighting": "dusty afternoon light",
        }}))
        status, payload = self.call("POST", "/generic/build", {
            "session_id": SESSION,
            "model_id": "m",
            "brief": "Bob, a man in his late 30s, is sitting in his living room",
        })
        self.assertEqual(status, 200)
        fields = payload["session"]["fields"]
        self.assertEqual(fields["subject"]["origin"], generic.ORIGIN_USER)
        self.assertEqual(fields["lighting"]["origin"], generic.ORIGIN_INVENTED)
        self.assertEqual(payload["session"]["conversation"][-1]["role"], "assistant")

    def test_build_refuses_with_neither_a_brief_nor_media(self):
        status, payload = self.call("POST", "/generic/build", {"session_id": SESSION, "model_id": "m", "brief": ""})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    def test_an_unusable_build_answer_is_retried_once(self):
        self.answers.append("Sure! Here is a lovely scene for you.")
        self.answers.append(json.dumps({"scene": {"subject": "Bob", "mood": "wistful"}}))
        status, payload = self.call("POST", "/generic/build", {
            "session_id": SESSION, "model_id": "m", "brief": "Bob waits",
        })
        self.assertEqual(status, 200)
        self.assertTrue(payload["retried"])
        self.assertEqual(payload["session"]["fields"]["subject"]["value"], "Bob")
        self.assertIn("written again", payload["session"]["conversation"][-1]["text"])

    def test_two_unusable_build_answers_are_reported_with_the_models_words(self):
        self.answers.append("Sure! Here is a lovely scene for you.")
        self.answers.append("I am afraid I cannot do that.")
        status, payload = self.call("POST", "/generic/build", {
            "session_id": SESSION, "model_id": "m", "brief": "Bob waits",
        })
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "INVALID_GENERIC_BUILD")
        self.assertIn("cannot do that", payload["error"]["details"]["model_said"])
        _status, session = self.call("GET", "/session", query={"session_id": SESSION})
        self.assertEqual(session["session"]["unspecified"], list(generic.FIELDS))

    def test_a_model_failure_during_build_surfaces_its_own_code(self):
        self.model_error = ModelError("MODEL_NOT_FOUND", "gone")
        status, payload = self.call("POST", "/generic/build", {
            "session_id": SESSION, "model_id": "m", "brief": "Bob waits",
        })
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "MODEL_NOT_FOUND")

    def test_a_rebuild_keeps_what_the_user_settled(self):
        self.answers.append(json.dumps({"scene": {"subject": "Bob", "mood": "wistful"}}))
        self.call("POST", "/generic/build", {"session_id": SESSION, "model_id": "m", "brief": "Bob waits"})
        self.call("POST", "/generic/field", {"session_id": SESSION, "field": "mood", "value": "furious"})
        self.answers.append(json.dumps({"scene": {"subject": "Bob", "mood": "wistful"}}))
        _status, payload = self.call("POST", "/generic/build", {
            "session_id": SESSION, "model_id": "m", "brief": "Bob waits",
        })
        self.assertEqual(payload["session"]["fields"]["mood"]["value"], "furious")
        self.assertIn("mood", payload["protected"])

    # -- conversation ----------------------------------------------------

    def build_once(self):
        self.answers.append(json.dumps({"scene": {"subject": "a woman on a bridge", "wardrobe": "a blue skirt"}}))
        self.call("POST", "/generic/build", {
            "session_id": SESSION, "model_id": "m", "brief": "a woman on a bridge in a blue skirt",
        })

    # -- clearing -------------------------------------------------------

    def test_clearing_prompts_clears_the_generic_prompt_but_keeps_your_work(self):
        """The reported bug: Clear prompts left the actual prompt standing."""
        self.answers.append(json.dumps({"scene": {"subject": "Bob", "mood": "wistful"}}))
        self.call("POST", "/generic/build", {"session_id": SESSION, "model_id": "m", "brief": "Bob waits"})
        self.call("POST", "/goals", {"session_id": SESSION, "text": "never mention a brand"})
        state = session_store.load(SESSION)
        session_store.record_output(state, "Krea2", prompt="a compiled prose prompt")
        session_store.save(state)

        status, payload = self.call("POST", "/session/reset", {"session_id": SESSION, "scope": "prompts"})
        self.assertEqual(status, 200)
        session = payload["session"]
        self.assertEqual(session["unspecified"], list(generic.FIELDS), "the document is gone")
        self.assertEqual(session["outputs"], {}, "the compiled prompts are gone")
        self.assertEqual(session["inputs"]["brief"], "Bob waits", "the brief is yours and stays")
        self.assertEqual(len(session["goals"]), 1, "goals are yours and stay")
        self.assertTrue(session["conversation"], "the conversation stays")

    def test_clearing_everything_takes_the_goals_and_the_conversation_too(self):
        self.answers.append(json.dumps({"scene": {"subject": "Bob"}}))
        self.call("POST", "/generic/build", {"session_id": SESSION, "model_id": "m", "brief": "Bob waits"})
        self.call("POST", "/goals", {"session_id": SESSION, "text": "never mention a brand"})
        _status, payload = self.call("POST", "/session/reset", {"session_id": SESSION, "scope": "all"})
        self.assertEqual(payload["session"]["goals"], [])
        self.assertEqual(payload["session"]["conversation"], [])
        self.assertEqual(payload["session"]["inputs"]["brief"], "")

    def test_an_unknown_clear_scope_is_refused(self):
        status, payload = self.call("POST", "/session/reset", {"session_id": SESSION, "scope": "half"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    # -- expanding the brief ---------------------------------------------

    def test_expanding_rewrites_the_brief_and_hands_back_the_previous_wording(self):
        self.answers.append("A lone bicycle courier crosses a rain-dark rooftop at blue hour, sets a glowing "
                            "parcel on a bench and watches the city lights come on below.")
        status, payload = self.call("POST", "/generic/expand", {
            "session_id": SESSION, "model_id": "m", "brief": "a courier on a rooftop",
        })
        self.assertEqual(status, 200)
        self.assertIn("blue hour", payload["brief"])
        self.assertEqual(payload["previous_brief"], "a courier on a rooftop")
        self.assertEqual(payload["session"]["inputs"]["brief"], payload["brief"])

    def test_expanding_refuses_an_answer_that_is_not_a_brief(self):
        for answer in ("## Scene\nA courier.", "detailed_description: [Shot 1] a courier", "- a courier"):
            self.answers.append(answer)
            status, payload = self.call("POST", "/generic/expand", {
                "session_id": SESSION, "model_id": "m", "brief": "a courier on a rooftop",
            })
            self.assertEqual(status, 502, answer)
            self.assertEqual(payload["error"]["code"], "INVALID_BRIEF_EXPANSION", answer)

    def test_expanding_an_empty_brief_says_to_write_one(self):
        status, payload = self.call("POST", "/generic/expand", {"session_id": SESSION, "model_id": "m", "brief": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["details"]["field"], "brief")

    def test_a_refused_expansion_leaves_the_brief_alone(self):
        self.call("POST", "/session/inputs", {"session_id": SESSION, "brief": "a courier on a rooftop"})
        self.answers.append("## Scene")
        self.call("POST", "/generic/expand", {"session_id": SESSION, "model_id": "m"})
        _status, session = self.call("GET", "/session", query={"session_id": SESSION})
        self.assertEqual(session["session"]["inputs"]["brief"], "a courier on a rooftop")

    def test_a_turn_before_a_build_is_refused_with_a_reason(self):
        status, payload = self.call("POST", "/generic/turn", {
            "session_id": SESSION, "model_id": "m", "message": "make the skirt red",
        })
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "GENERIC_PROMPT_EMPTY")

    def test_a_turn_patches_the_document_and_records_both_sides(self):
        self.build_once()
        self.answers.append(json.dumps({"reply": "Red it is.", "patch": {"wardrobe": "a red skirt"}}))
        status, payload = self.call("POST", "/generic/turn", {
            "session_id": SESSION, "model_id": "m", "message": "her skirt is red, not blue",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["reply"], "Red it is.")
        self.assertEqual(payload["session"]["fields"]["wardrobe"]["value"], "a red skirt")
        roles = [turn["role"] for turn in payload["session"]["conversation"][-2:]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_a_turn_can_add_a_standing_goal(self):
        self.build_once()
        self.answers.append(json.dumps({
            "reply": "I will keep the colours from the picture.",
            "goals": [{"text": "follow the clothing colours in the image", "kind": "field", "fields": ["wardrobe"]}],
        }))
        _status, payload = self.call("POST", "/generic/turn", {
            "session_id": SESSION, "model_id": "m", "message": "you are not following the clothing colours",
        })
        self.assertEqual(len(payload["session"]["goals"]), 1)
        self.assertEqual(payload["goals_added"], ["follow the clothing colours in the image"])

    def test_an_unusable_turn_keeps_the_users_message_in_the_log(self):
        self.build_once()
        self.answers.append("I have made the skirt red for you.")
        status, payload = self.call("POST", "/generic/turn", {
            "session_id": SESSION, "model_id": "m", "message": "make it red",
        })
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "INVALID_GENERIC_TURN")
        _status, session = self.call("GET", "/session", query={"session_id": SESSION})
        self.assertEqual(session["session"]["conversation"][-1]["text"], "make it red")

    def test_an_empty_message_is_refused(self):
        status, payload = self.call("POST", "/generic/turn", {"session_id": SESSION, "model_id": "m", "message": "  "})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")

    # -- inline edits and goals -----------------------------------------

    def test_an_inline_edit_marks_the_field_as_the_users(self):
        self.build_once()
        _status, payload = self.call("POST", "/generic/field", {
            "session_id": SESSION, "field": "wardrobe", "value": "a red skirt",
        })
        record = payload["session"]["fields"]["wardrobe"]
        self.assertEqual(record["value"], "a red skirt")
        self.assertEqual(record["origin"], generic.ORIGIN_USER)

    def test_an_inline_edit_against_an_observed_fact_becomes_an_override(self):
        state = session_store.load(SESSION)
        state["generic"] = generic.set_field(
            state["generic"], "wardrobe", "a blue skirt", generic.ORIGIN_ASSET, observed="a blue skirt"
        )
        session_store.save(state)
        _status, payload = self.call("POST", "/generic/field", {
            "session_id": SESSION, "field": "wardrobe", "value": "a red skirt",
        })
        record = payload["session"]["fields"]["wardrobe"]
        self.assertEqual(record["origin"], generic.ORIGIN_OVERRIDE)
        self.assertEqual(record["observed"], "a blue skirt")

    def test_an_inline_edit_can_clear_a_field_back_to_unspecified(self):
        self.build_once()
        _status, payload = self.call("POST", "/generic/field", {
            "session_id": SESSION, "field": "wardrobe", "value": "",
        })
        self.assertIn("wardrobe", payload["session"]["unspecified"])

    def test_an_unknown_field_is_refused(self):
        status, payload = self.call("POST", "/generic/field", {"session_id": SESSION, "field": "vibes", "value": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "UNKNOWN_FIELD")

    def test_goals_can_be_added_paused_and_deleted(self):
        _status, payload = self.call("POST", "/goals", {
            "session_id": SESSION, "text": "never mention a brand name",
        })
        goal_id = payload["session"]["goals"][0]["id"]
        self.assertEqual(payload["session"]["goals"][0]["kind"], goals.KIND_JUDGED)

        _status, payload = self.call("POST", "/goals", {
            "session_id": SESSION, "action": "toggle", "id": goal_id, "enabled": False,
        })
        self.assertFalse(payload["session"]["goals"][0]["enabled"])

        _status, payload = self.call("POST", "/goals", {"session_id": SESSION, "action": "delete", "id": goal_id})
        self.assertEqual(payload["session"]["goals"], [])

    def test_a_non_string_goal_payload_is_coerced_rather_than_crashing(self):
        """`must_include: [123]` is a fine requirement; it used to be an HTTP 500."""
        status, payload = self.call("POST", "/goals", {
            "session_id": SESSION, "text": "mention the year", "kind": "presence", "must_include": [123],
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["session"]["goals"][0]["must_include"], ["123"])

    def test_an_oversized_or_overlong_goal_payload_is_refused(self):
        for payload, code in (
            ({"text": "x", "kind": "field", "fields": 5}, "INVALID_GOAL"),
            ({"text": "x", "kind": "presence", "must_include": ["a" * 5000]}, "GOAL_TOO_LONG"),
            ({"text": "x", "kind": "presence", "must_include": ["a"] * 50}, "TOO_MANY_INCLUDES"),
        ):
            status, body = self.call("POST", "/goals", {"session_id": SESSION, **payload})
            self.assertEqual(status, 400, payload)
            self.assertEqual(body["error"]["code"], code, payload)

    def test_an_unsupported_aspect_ratio_is_refused(self):
        status, payload = self.call("POST", "/session/inputs", {"session_id": SESSION, "aspect_ratio": "7:11"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "INVALID_ASPECT_RATIO")

    def test_work_done_while_the_model_ran_is_not_overwritten(self):
        """The brief kept reverting when a turn landed; the handler owns less now."""
        self.build_once()
        self.answers.append(json.dumps({"reply": "Done.", "patch": {"wardrobe": "a red skirt"}}))
        handler = self.recorder.handlers[("POST", "/h3studio/generic/turn")]

        original_generate = self._backend_class.generate

        def generate_and_edit_meanwhile(backend_self, model, assembled, session_id, **kwargs):
            # Stands in for the studio's debounced save landing mid-generation.
            state = session_store.load(SESSION)
            state["inputs"] = {**state["inputs"], "brief": "typed while the model was working"}
            session_store.save(state)
            return original_generate(backend_self, model, assembled, session_id, **kwargs)

        self._backend_class.generate = generate_and_edit_meanwhile
        try:
            response = asyncio.run(handler(_Request(body={
                "session_id": SESSION, "model_id": "m", "message": "make it red",
            })))
        finally:
            self._backend_class.generate = original_generate
        payload = json.loads(response.body.decode())
        self.assertEqual(payload["session"]["inputs"]["brief"], "typed while the model was working")
        self.assertEqual(payload["session"]["fields"]["wardrobe"]["value"], "a red skirt")

    def test_a_duplicate_goal_is_refused_out_loud(self):
        """Silence here loses the user's text: the studio clears the box first."""
        self.call("POST", "/goals", {"session_id": SESSION, "text": "never mention a brand name"})
        status, payload = self.call("POST", "/goals", {"session_id": SESSION, "text": "Never mention a brand name "})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "GOAL_NOT_ADDED")
        self.assertIn("already", payload["error"]["message"])

    def test_a_full_ledger_says_it_is_full(self):
        for index in range(goals.MAX_GOALS):
            self.call("POST", "/goals", {"session_id": SESSION, "text": f"goal number {index}"})
        status, payload = self.call("POST", "/goals", {"session_id": SESSION, "text": "one goal too many"})
        self.assertEqual(status, 409)
        self.assertIn("full", payload["error"]["message"])

    def test_deleting_a_goal_that_is_gone_says_so(self):
        status, payload = self.call("POST", "/goals", {"session_id": SESSION, "action": "delete", "id": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "GOAL_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
