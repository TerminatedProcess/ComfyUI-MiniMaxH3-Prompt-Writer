"""Durable studio state: it survives a restart, and only Reset clears it.

The corrupt-file cases are not hypothetical -- this was written after a power cut
ate a desktop mid-session. A half-written state file must cost at most the field
document, never the whole session.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import generic, goals, session_store

SESSION = "11111111-2222-4333-8444-555555555555"


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(session_store, "STATE_ROOT", Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_new_session_starts_with_both_flags_on(self):
        state = session_store.load(SESSION)
        self.assertTrue(state["inputs"]["nsfw"])
        self.assertTrue(state["inputs"]["story"])
        self.assertTrue(generic.is_empty(state["generic"]))

    def test_state_survives_a_reload(self):
        state = session_store.load(SESSION)
        state["generic"] = generic.set_field(state["generic"], "subject", "Bob", generic.ORIGIN_USER)
        state["goals"] = [goals.new_goal("keep it one shot")]
        session_store.record_turn(state, "user", "make the room 70s")
        session_store.save(state)

        restored = session_store.load(SESSION)
        self.assertEqual(generic.record(restored["generic"], "subject")["value"], "Bob")
        self.assertEqual(restored["goals"][0]["text"], "keep it one shot")
        self.assertEqual(restored["conversation"][-1]["text"], "make the room 70s")

    def test_only_reset_clears_the_session(self):
        state = session_store.load(SESSION)
        state["generic"] = generic.set_field(state["generic"], "subject", "Bob", generic.ORIGIN_USER)
        session_store.save(state)
        cleared = session_store.reset(SESSION)
        self.assertTrue(generic.is_empty(cleared["generic"]))
        self.assertTrue(generic.is_empty(session_store.load(SESSION)["generic"]))

    def test_a_session_id_cannot_escape_the_state_directory(self):
        for bad in ("../../etc/passwd", "not-a-uuid", "", None):
            with self.assertRaises(session_store.SessionStoreError):
                session_store.load(bad)

    def test_a_truncated_state_file_does_not_lose_the_session(self):
        session_store.save(session_store.load(SESSION))
        path = Path(self.temp.name) / f"{SESSION}.json"
        path.write_text('{"schema": "session/1", "conversa', encoding="utf-8")
        state = session_store.load(SESSION)
        self.assertTrue(generic.is_empty(state["generic"]))
        self.assertEqual(state["session_id"], SESSION)

    def test_a_corrupt_document_keeps_the_conversation_and_goals(self):
        state = session_store.load(SESSION)
        state["goals"] = [goals.new_goal("keep it one shot")]
        session_store.record_turn(state, "user", "hello")
        session_store.save(state)
        path = Path(self.temp.name) / f"{SESSION}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["generic"] = {"fields": {"subject": "not a record"}}
        path.write_text(json.dumps(raw), encoding="utf-8")

        state = session_store.load(SESSION)
        self.assertTrue(generic.is_empty(state["generic"]))
        self.assertEqual(state["goals"][0]["text"], "keep it one shot")
        self.assertEqual(state["conversation"][-1]["text"], "hello")

    def test_the_write_is_atomic(self):
        """A save leaves no temp file behind to be mistaken for state."""
        state = session_store.load(SESSION)
        state["generic"] = generic.set_field(state["generic"], "subject", "Bob", generic.ORIGIN_USER)
        session_store.save(state)
        names = sorted(path.name for path in Path(self.temp.name).iterdir())
        self.assertEqual(names, [f"{SESSION}.json"])

    def test_an_empty_session_writes_no_file(self):
        """A page load mints an id; it must not leave a file behind per visit."""
        session_store.save(session_store.load(SESSION))
        self.assertEqual(sorted(path.name for path in Path(self.temp.name).iterdir()), [])

    def test_a_session_emptied_by_reset_removes_its_file(self):
        state = session_store.load(SESSION)
        state["generic"] = generic.set_field(state["generic"], "subject", "Bob", generic.ORIGIN_USER)
        session_store.save(state)
        session_store.save(session_store.reset(SESSION))
        self.assertEqual(sorted(path.name for path in Path(self.temp.name).iterdir()), [])

    def test_one_output_is_kept_per_target(self):
        state = session_store.load(SESSION)
        session_store.record_output(state, "Krea2", prompt="a prose prompt")
        session_store.record_output(state, "Anima", prompt="1girl, smile", negative_prompt="worst quality", variant="turbo")
        session_store.save(state)

        restored = session_store.load(SESSION)
        self.assertEqual(restored["outputs"]["Krea2"]["prompt"], "a prose prompt")
        self.assertEqual(restored["outputs"]["Anima"]["negative_prompt"], "worst quality")
        self.assertEqual(restored["outputs"]["Anima"]["variant"], "turbo")

    def test_an_output_records_the_document_version_it_came_from(self):
        state = session_store.load(SESSION)
        state["generic"] = generic.set_field(state["generic"], "subject", "Bob", generic.ORIGIN_USER)
        session_store.record_output(state, "Krea2", prompt="a prose prompt")
        self.assertEqual(
            state["outputs"]["Krea2"]["generic_updated_at"],
            state["generic"]["updated_at"],
        )

    def test_conversation_is_capped_without_losing_the_latest(self):
        state = session_store.load(SESSION)
        for index in range(session_store.MAX_CONVERSATION_TURNS + 20):
            session_store.record_turn(state, "user", f"turn {index}")
        self.assertEqual(len(state["conversation"]), session_store.MAX_CONVERSATION_TURNS)
        self.assertEqual(state["conversation"][-1]["text"], f"turn {session_store.MAX_CONVERSATION_TURNS + 19}")

    def test_public_reports_media_that_is_no_longer_loaded(self):
        state = session_store.load(SESSION)
        state["media_snapshot"] = [
            {"id": "a1", "filename": "hero.png", "reference": "<Picture 1>", "type": "image"},
            {"id": "a2", "filename": "clip.mp4", "reference": "<Video 1>", "type": "video"},
        ]
        public = session_store.public(state, attached=[{"id": "a1"}])
        self.assertEqual([item["filename"] for item in public["media_missing"]], ["clip.mp4"])

    def test_public_exposes_the_field_layout_the_studio_renders(self):
        public = session_store.public(session_store.load(SESSION), attached=[])
        self.assertEqual(public["field_order"], list(generic.FIELDS))
        self.assertEqual(len(public["groups"]), len(generic.GROUPS))
        self.assertEqual(set(public["fields"]), set(generic.FIELDS))
        self.assertEqual(public["unspecified"], list(generic.FIELDS))

    def test_media_snapshot_is_derived_from_a_manifest(self):
        snapshot = session_store.media_snapshot({
            "assets": [{"id": "a1", "filename": "hero.png", "reference": "<Picture 1>", "type": "image", "extra": 1}]
        })
        self.assertEqual(snapshot, [{"id": "a1", "filename": "hero.png", "reference": "<Picture 1>", "type": "image"}])


if __name__ == "__main__":
    unittest.main()
