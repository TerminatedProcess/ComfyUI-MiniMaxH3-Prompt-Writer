"""Another app handing a prompt over: the base map, the SD tag target, locked
tokens, the handoff endpoints and the fleet gate.

The cases worth having are the silent ones: a Pony prompt without its score
string renders fine and looks wrong, a LoRA trigger word one character off
stops the LoRA firing, and a gate that loads a model over a running render
kills the render. None of those raise anything on their own.
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

from PIL import Image


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

from backend import fleet_gate, media, routes, session_store  # noqa: E402
from backend import external_routes  # noqa: E402
from backend.assembly import assemble_request  # noqa: E402
from backend.models.contract import ModelError  # noqa: E402
from backend.targets import TargetError, restore_locked_tokens, sdtags  # noqa: E402
from backend.targets.external_map import resolve  # noqa: E402


class BaseMapTests(unittest.TestCase):
    def test_invokeai_bases_map_to_targets(self):
        self.assertEqual(resolve("krea-2"), ("Krea2", None))
        self.assertEqual(resolve("krea2", variant="turbo"), ("Krea2", None))
        self.assertEqual(resolve("anima", variant="aesthetic"), ("Anima", "aesthetic"))
        self.assertEqual(resolve("anima", variant="nonsense"), ("Anima", None))
        self.assertEqual(resolve("sd-1"), ("SDTags", "sd1"))
        self.assertEqual(resolve("minimax-h3"), ("T2VA", None))
        self.assertEqual(resolve("minimax-h3", image_count=1), ("I2VA", None))

    def test_sdxl_family_picks_the_quality_vocabulary(self):
        self.assertEqual(resolve("sdxl"), ("SDTags", "sdxl"))
        self.assertEqual(resolve("sdxl", family="Illustrious"), ("SDTags", "illustrious"))
        self.assertEqual(resolve("sdxl", family="NoobAI"), ("SDTags", "illustrious"))
        self.assertEqual(resolve("sdxl", family="Pony"), ("SDTags", "pony"))

    def test_an_unmapped_base_is_refused_not_guessed(self):
        for base in ("flux3", "qwen-image", "z-image", "", "moss-image"):
            with self.assertRaises(TargetError) as caught:
                resolve(base)
            self.assertEqual(caught.exception.code, "UNSUPPORTED_TARGET")


def _assembled(variant, tokens=()):
    return {
        "input": {"mode": "SDTags", "variant": variant, "nsfw": True, "locked_tokens": list(tokens)},
        "messages": [{"role": "user", "content": "brief"}],
    }


class SDTagAuditTests(unittest.TestCase):
    def test_pony_needs_its_full_score_string(self):
        bad = "Positive: 1girl, solo, smile, red hair, beach, sunset\nNegative: lowres"
        result = sdtags.audit(bad, _assembled("pony"))
        self.assertTrue(result["repair_required"])
        good = f"Positive: {sdtags.PONY_SCORE_PREFIX}, source_anime, rating_safe, 1girl, smile, beach\nNegative: lowres"
        self.assertFalse(sdtags.audit(good, _assembled("pony"))["repair_required"])

    def test_score_tags_are_refused_off_pony(self):
        draft = "Positive: score_9, masterpiece, 1girl, solo, smile, beach\nNegative: worst quality"
        result = sdtags.audit(draft, _assembled("illustrious"))
        self.assertTrue(result["repair_required"])
        self.assertIn("score_9", " ".join(result["format_failures"]))

    def test_a_clean_illustrious_pair_passes(self):
        draft = "Positive: masterpiece, best quality, 1girl, solo, upper body, smile, (red hair:1.2)\nNegative: worst quality, lowres"
        self.assertFalse(sdtags.audit(draft, _assembled("illustrious"))["repair_required"])

    def test_nested_weights_fail(self):
        draft = "Positive: masterpiece, ((1girl)), solo, smile, beach, sunset\nNegative: lowres"
        self.assertTrue(sdtags.audit(draft, _assembled("sdxl"))["repair_required"])


class LockedTokenTests(unittest.TestCase):
    def test_a_dropped_token_goes_back_before_the_negative_line(self):
        draft = "Positive: 1girl, smile\nNegative: lowres"
        restored = restore_locked_tokens(_assembled("pony", ["<lora:abc:0.8>", "smile", "zxq_trigger"]), draft)
        self.assertEqual(restored, "Positive: 1girl, smile, <lora:abc:0.8>, zxq_trigger\nNegative: lowres")
        parsed = sdtags.parse_output(restored)
        self.assertIn("zxq_trigger", parsed["prompt"])
        self.assertNotIn("zxq_trigger", parsed["negative_prompt"])

    def test_nothing_changes_when_every_token_is_present(self):
        draft = "Positive: <lora:abc:0.8>, 1girl\nNegative: lowres"
        self.assertEqual(restore_locked_tokens(_assembled("pony", ["<lora:abc:0.8>"]), draft), draft)
        self.assertEqual(restore_locked_tokens({"input": {}}, draft), draft)

    def test_prose_without_a_negative_line_gets_them_on_the_end(self):
        self.assertEqual(restore_locked_tokens(_assembled("sdxl", ["trig"]), "A woman on a beach."), "A woman on a beach., trig")


class _Recorder:
    def __init__(self):
        self.handlers = {}

    def __getattr__(self, name):
        if name not in {"get", "post", "delete"}:
            raise AttributeError(name)

        def register(path):
            def decorator(function):
                self.handlers[(name.upper(), path)] = function
                return function
            return decorator
        return register


class _Request:
    content_type = "application/json"
    scheme = "http"
    host = "127.0.0.1:8506"

    def __init__(self, body=None, match_info=None, headers=None, host=None):
        self._body = body
        self.match_info = match_info or {}
        self.headers = headers or {}
        if host:
            self.host = host

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class HandoffRouteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for item in (
            patch.object(session_store, "STATE_ROOT", self.root / "sessions"),
            patch.object(external_routes, "CACHE_ROOT", self.root / "cache"),
            patch.object(media, "CACHE_ROOT", self.root / "cache"),
            patch.object(routes, "_claim_media_mutation", lambda: True),
            patch.object(routes, "_release_media_mutation", lambda: None),
        ):
            item.start()
            self.addCleanup(item.stop)

        async def worker(function, *args, **kwargs):
            return function(*args, **kwargs), None

        run = patch.object(routes, "_run_thread_worker", worker)
        run.start()
        self.addCleanup(run.stop)
        self.recorder = _Recorder()
        external_routes.register_external_routes(self.recorder, routes)

    def call(self, method, path, body=None, match_info=None, **request):
        handler = self.recorder.handlers[(method, f"/h3studio{path}")]
        response = asyncio.run(handler(_Request(body, match_info, **request)))
        return response.status, json.loads(response.body.decode())

    def create(self, **overrides):
        body = {"prompt": "1girl, beach, <lora:sun:0.7>", "negative": "lowres", "base": "sdxl",
                "family": "Pony", "locked_tokens": ["<lora:sun:0.7>", "sunkiss"], "source": "invokeai-generator"}
        body.update(overrides)
        return self.call("POST", "/external/session", body)

    def test_create_persists_the_handoff_without_running_a_model(self):
        status, payload = self.create()
        self.assertEqual(status, 201)
        self.assertEqual((payload["mode"], payload["variant"]), ("SDTags", "pony"))
        self.assertTrue(payload["url"].endswith(f"/?session={payload['session_id']}&autostart=1"))
        state = session_store.load(payload["session_id"])
        self.assertEqual(state["inputs"]["brief"], "1girl, beach, <lora:sun:0.7>")
        self.assertEqual(state["target"], {"mode": "SDTags", "variant": "pony"})
        self.assertEqual(state["external"]["locked_tokens"], ["<lora:sun:0.7>", "sunkiss"])
        self.assertEqual(state["external"]["negative"], "lowres")

    def test_unsupported_base_is_a_422(self):
        status, payload = self.create(base="flux3")
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "UNSUPPORTED_TARGET")

    def test_missing_prompt_and_missing_image_are_refused(self):
        self.assertEqual(self.create(prompt="")[0], 400)
        status, payload = self.create(image_path=str(self.root / "nope.png"))
        self.assertEqual((status, payload["error"]["code"]), (400, "IMAGE_NOT_FOUND"))
        text = self.root / "notes.txt"
        text.write_text("x")
        self.assertEqual(self.create(image_path=str(text))[1]["error"]["code"], "UNSUPPORTED_MEDIA")

    def test_an_image_path_is_attached_to_the_session(self):
        picture = self.root / "still.png"
        Image.new("RGB", (64, 48), "red").save(picture)
        status, payload = self.create(base="minimax-h3", image_path=str(picture))
        self.assertEqual(status, 201)
        self.assertEqual(payload["mode"], "I2VA")
        self.assertEqual(len(media.STORE.list(payload["session_id"])), 1)

    def test_status_reports_idle_then_the_best_result(self):
        _, created = self.create()
        session_id = created["session_id"]
        status, payload = self.call("GET", "/external/session/{session_id}", match_info={"session_id": session_id})
        self.assertEqual((status, payload["status"], payload["result"]), (200, "idle", None))
        state = session_store.load(session_id)
        session_store.record_output(state, "SDTags", prompt="score_9, 1girl", negative_prompt="lowres",
                                    variant="pony", audit={"repair_required": False})
        session_store.save(state)
        _, payload = self.call("GET", "/external/session/{session_id}", match_info={"session_id": session_id})
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["result"]["prompt"], "score_9, 1girl")
        self.assertEqual(payload["result"]["negative_prompt"], "lowres")
        self.assertTrue(payload["result"]["audit_ok"])
        self.assertFalse(payload["result"]["stale"])

    def test_status_of_an_ordinary_session_is_404(self):
        status, _ = self.call("GET", "/external/session/{session_id}",
                              match_info={"session_id": "11111111-2222-4333-8444-555555555555"})
        self.assertEqual(status, 404)

    def test_a_web_page_cannot_drive_a_handoff(self):
        body = {"prompt": "x", "base": "sdxl"}
        status, payload = self.call("POST", "/external/session", body, headers={"Origin": "https://evil.example"})
        self.assertEqual((status, payload["error"]["code"]), (403, "FORBIDDEN_ORIGIN"))
        status, _ = self.call("POST", "/external/session", body, host="attacker.example:8506")
        self.assertEqual(status, 403)
        self.assertEqual(self.call("POST", "/external/session", body, headers={"Origin": "http://127.0.0.1:9091"})[0], 201)
        self.assertEqual(self.call("POST", "/external/session", body, headers={"Origin": "http://127.0.0.1:8506"})[0], 201)

    def test_locked_tokens_on_a_video_target_are_refused_out_loud(self):
        status, payload = self.create(base="minimax-h3")
        self.assertEqual(status, 201)
        self.assertTrue(payload["warnings"])
        self.assertEqual(session_store.load(payload["session_id"])["external"]["locked_tokens"], [])

    def test_a_reported_failure_reads_as_error_until_a_newer_result(self):
        _, created = self.create()
        info = {"session_id": created["session_id"]}
        self.call("POST", "/external/session/{session_id}/error", {"message": "Handoff paused: stopped"}, info)
        _, payload = self.call("GET", "/external/session/{session_id}", match_info=info)
        self.assertEqual((payload["status"], payload["error"]), ("error", "Handoff paused: stopped"))
        state = session_store.load(created["session_id"])
        session_store.record_output(state, "SDTags", prompt="p", audit={})
        session_store.save(state)
        self.assertEqual(self.call("GET", "/external/session/{session_id}", match_info=info)[1]["status"], "ready")

    def test_another_sessions_compile_is_not_this_ones(self):
        _, created = self.create()
        info = {"session_id": created["session_id"]}
        with patch.dict(routes.STATE, {"active_request_id": "r", "active_session_id": "someone-else"}):
            self.assertEqual(self.call("GET", "/external/session/{session_id}", match_info=info)[1]["status"], "idle")
        with patch.dict(routes.STATE, {"active_request_id": "r", "active_session_id": created["session_id"]}):
            self.assertEqual(self.call("GET", "/external/session/{session_id}", match_info=info)[1]["status"], "running")

    def test_autostart_happens_once(self):
        _, created = self.create()
        info = {"session_id": created["session_id"]}
        self.assertTrue(self.call("POST", "/external/session/{session_id}/autostarted", {}, info)[1]["first"])
        self.assertFalse(self.call("POST", "/external/session/{session_id}/autostarted", {}, info)[1]["first"])

    def test_compile_carries_the_locked_tokens_and_source_negative(self):
        _, created = self.create()
        assembled = assemble_request({"mode": "SDTags", "session_id": created["session_id"],
                                      "creative_brief": "1girl, beach", "variant": "pony"})
        self.assertEqual(assembled["input"]["locked_tokens"], ["<lora:sun:0.7>", "sunkiss"])
        user = next(m["content"] for m in assembled["messages"] if m["role"] == "user")
        self.assertIn("sunkiss", user)
        self.assertIn("Source negative prompt", user)
        names = [m.get("name") for m in assembled["messages"] if m["role"] == "system"]
        # Only the variant's own guide: the two contradict each other on quality tags.
        self.assertIn("official_pony_prompting_rules", names)
        self.assertNotIn("official_illustrious_prompting_rules", names)
        illustrious = assemble_request({"mode": "SDTags", "session_id": created["session_id"],
                                        "creative_brief": "1girl", "variant": "illustrious"})
        names = [m.get("name") for m in illustrious["messages"] if m["role"] == "system"]
        self.assertIn("official_illustrious_prompting_rules", names)
        self.assertNotIn("official_pony_prompting_rules", names)
        plain = assemble_request({"mode": "SDTags", "session_id": created["session_id"],
                                  "creative_brief": "1girl", "variant": "sdxl"})
        names = [m.get("name") for m in plain["messages"] if m["role"] == "system"]
        self.assertEqual([n for n in names if n and n.startswith("official_")], [])


class FleetGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = fleet_gate.FleetGate()
        self.applied: list[str] = []
        for item in (
            patch.object(fleet_gate, "ENABLED", True),
            patch.object(fleet_gate, "POLL_SECONDS", 0.01),
            patch.object(fleet_gate.FleetGate, "_notify", lambda *args, **kwargs: None),
        ):
            item.start()
            self.addCleanup(item.stop)

    def run_gate(self, counts, presets, llm=True, decide=None):
        sequence = list(counts)

        async def queue_counts(_http):
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

        async def get_json(_http, _url):
            return presets

        async def llm_ready(_http):
            return llm

        async def apply(label):
            self.applied.append(label)

        async def main():
            if decide:
                async def later():
                    await asyncio.sleep(0.03)
                    self.gate.decide(decide)
                asyncio.ensure_future(later())
            await self.gate.acquire("s", lambda: False)

        class _Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def text(self):
                return ""

        test = self

        def post(_self, url, json=None, timeout=None):
            test.applied.append(json["label"])
            return _Response()

        with patch.object(fleet_gate, "queue_counts", queue_counts), \
             patch.object(fleet_gate, "_get_json", get_json), \
             patch.object(fleet_gate, "llm_ready", llm_ready), \
             patch("aiohttp.ClientSession.post", post):
            asyncio.run(main())

    PRESETS = {"selected": "b", "presets": [{"id": "a", "label": "Prompt Writer + Generator"}, {"id": "b", "label": "InvokeAI"}]}

    def test_waits_for_the_queue_then_applies_the_preset(self):
        busy, idle = {"comfyui": 0, "invokeai": 3}, {"comfyui": 0, "invokeai": 0}
        self.run_gate([busy, busy, idle], self.PRESETS)
        self.assertEqual(self.applied, ["Prompt Writer + Generator"])
        self.assertEqual(self.gate.phase, "idle")

    def test_skips_the_switch_when_already_selected_and_answering(self):
        presets = {**self.PRESETS, "selected": "a"}
        self.run_gate([{"comfyui": 0, "invokeai": 0}], presets)
        self.assertEqual(self.applied, [])

    def test_cancel_stops_waiting_and_never_switches(self):
        with self.assertRaises(ModelError) as caught:
            self.run_gate([{"comfyui": 2, "invokeai": 0}], self.PRESETS, decide="cancel")
        self.assertEqual(caught.exception.code, "GPU_WAIT_CANCELLED")
        self.assertEqual(self.applied, [])

    def test_go_ahead_switches_despite_the_queue(self):
        self.run_gate([{"comfyui": 2, "invokeai": 0}], self.PRESETS, decide="proceed")
        self.assertEqual(self.applied, ["Prompt Writer + Generator"])

    def test_a_missing_preset_is_a_clear_error(self):
        with self.assertRaises(ModelError) as caught:
            self.run_gate([{"comfyui": 0, "invokeai": 0}], {"selected": None, "presets": []})
        self.assertEqual(caught.exception.code, "FLEET_PRESET_MISSING")

    def test_disabled_gate_is_a_no_op(self):
        with patch.object(fleet_gate, "ENABLED", False):
            asyncio.run(self.gate.acquire("s", lambda: False))
        self.assertEqual(self.gate.phase, "idle")

    def test_describe_counts(self):
        self.assertEqual(fleet_gate.describe({"invokeai": 3, "comfyui": 1}), "InvokeAI has 3 jobs, ComfyUI has 1 job")


if __name__ == "__main__":
    unittest.main()
