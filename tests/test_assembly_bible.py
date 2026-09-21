import unittest

from backend.assembly import AssemblyError, _validated_bible
from backend.scene_bible import ORIGIN_ASSET, ORIGIN_INVENTED, new_bible, set_field


class ValidatedBibleTests(unittest.TestCase):
    def test_absent_bible_is_none(self):
        self.assertIsNone(_validated_bible({}))

    def test_bible_with_an_asset_lock_is_kept(self):
        b = set_field(new_bible(), "subject", "Bob in a denim jacket", ORIGIN_ASSET)
        self.assertEqual(_validated_bible({"bible": b}), b)

    def test_bible_without_locks_is_dropped(self):
        # Nothing to enforce, so carrying it into the audit is pure overhead.
        b = set_field(new_bible(), "era", "1970s", ORIGIN_INVENTED)
        self.assertIsNone(_validated_bible({"bible": b}))

    def test_malformed_bible_is_rejected_not_ignored(self):
        # Silently dropping it would disable asset locks without telling anyone,
        # which is the exact silent-drift failure locks exist to prevent.
        with self.assertRaises(AssemblyError) as ctx:
            _validated_bible({"bible": {"fields": {"vibe": "moody"}, "origins": {}}})
        self.assertEqual(ctx.exception.code, "UNKNOWN_FIELD")

    def test_non_object_bible_is_rejected(self):
        with self.assertRaises(AssemblyError) as ctx:
            _validated_bible({"bible": "Bob"})
        self.assertEqual(ctx.exception.code, "INVALID_BIBLE")

    def test_orphan_origin_is_rejected(self):
        with self.assertRaises(AssemblyError) as ctx:
            _validated_bible({"bible": {"fields": {}, "origins": {"subject": ORIGIN_ASSET}}})
        self.assertEqual(ctx.exception.code, "ORPHAN_ORIGIN")


class EstablishedFactsInRequestTests(unittest.TestCase):
    """Locks must constrain generation, not only grade it afterwards.

    Measured live: with the facts withheld from the request the model wrote
    whatever the brief implied and a single repair turn could not overturn a
    draft already built on the wrong subject (0/3 runs kept the locked
    subject). Feeding them in raised it to 2/3, with the repair recovering one
    of the remaining failures.
    """

    def _assemble(self, body_extra):
        import uuid
        from backend.assembly import assemble_request
        body = {
            "session_id": str(uuid.uuid4()),
            "mode": "T2VA",
            "duration_seconds": 8,
            "aspect_ratio": "16:9",
            "creative_brief": "Bob climbs the lighthouse stairs at dawn.",
        }
        body.update(body_extra)
        return assemble_request(body)

    def _user_message(self, assembled):
        return next(m["content"] for m in assembled["messages"] if m["role"] == "user")

    def test_locked_facts_reach_the_user_message(self):
        b = set_field(new_bible(), "subject", "Bob in a faded denim jacket", ORIGIN_ASSET)
        text = self._user_message(self._assemble({"bible": b}))
        self.assertIn("Established facts", text)
        self.assertIn("Bob in a faded denim jacket", text)

    def test_no_block_without_a_bible(self):
        text = self._user_message(self._assemble({}))
        self.assertNotIn("Established facts", text)

    def test_no_block_when_nothing_is_locked(self):
        b = set_field(new_bible(), "era", "1970s", ORIGIN_INVENTED)
        text = self._user_message(self._assemble({"bible": b}))
        self.assertNotIn("Established facts", text)


if __name__ == "__main__":
    unittest.main()
