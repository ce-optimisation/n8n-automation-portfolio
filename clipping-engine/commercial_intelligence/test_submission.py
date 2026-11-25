import hashlib
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from commercial_intelligence.submission import prepare
from commercial_intelligence.test_select_candidates import NOW, campaign, clip


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.media = Path(temporary.name) / "fixture.mp4"
        self.content = b"synthetic file identity fixture"
        self.media.write_bytes(self.content)

    def packet(self, candidate=None, **changes):
        arguments = dict(clip_id=1, destination="Instagram:brand", account_id="123",
                         media=self.media, as_of=NOW)
        arguments.update(changes)
        return prepare(campaign(), [candidate or clip()], **arguments)

    def test_draft_contains_file_identity_and_manual_gates(self):
        packet = self.packet()
        self.assertEqual(packet["packet_status"], "draft_for_manual_review")
        self.assertEqual(packet["destination"]["account_id"], "123")
        self.assertEqual(packet["media"]["sha256"], hashlib.sha256(self.content).hexdigest())
        self.assertIn("Obtain specific publication approval", packet["remaining_review"])

    def test_rejected_or_expired_clip_cannot_make_packet(self):
        with self.assertRaisesRegex(ValueError, "pass preflight"):
            self.packet(clip(status="rejected"))
        with self.assertRaisesRegex(ValueError, "pass preflight"):
            self.packet(as_of=NOW + timedelta(days=7))

    def test_invalid_destination_or_empty_media_cannot_make_packet(self):
        with self.assertRaisesRegex(ValueError, "destination"):
            self.packet(destination="Instagram:other")
        self.media.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.packet()
