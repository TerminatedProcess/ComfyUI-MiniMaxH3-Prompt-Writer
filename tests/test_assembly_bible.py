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


if __name__ == "__main__":
    unittest.main()
