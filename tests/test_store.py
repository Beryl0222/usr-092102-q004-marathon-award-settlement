import copy
import unittest

from src.store import DuplicateEventError, EventStore, UnexpectedVersionError, DomainError
from tests.fixtures import build_world


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store, *_ = build_world()

    def test_duplicate_event_id_rejected(self):
        events = self.store.events
        dup = copy.deepcopy(events[0])
        with self.assertRaises(DuplicateEventError):
            self.store.append(dup)

    def test_version_must_be_contiguous(self):
        last = self.store.events_for("list-gun")[-1]
        bad = copy.deepcopy(last)
        bad["event_id"] = "brand-new"
        bad["version"] = 99
        with self.assertRaises(UnexpectedVersionError):
            self.store.append(bad)

    def test_out_of_order_timestamps_rejected(self):
        last = self.store.events_for("list-gun")[-1]
        bad = copy.deepcopy(last)
        bad["event_id"] = "brand-new-2"
        bad["version"] = last["version"] + 1
        bad["occurred_at"] = "2000-01-01T00:00:00+08:00"
        bad["payload"] = {"list_id": "list-gun", "revision": 2,
                          "from_revision": 1, "to_revision": 2, "changes": [],
                          "amended_at": bad["occurred_at"]}
        bad["event_type"] = "RESULT_LIST_AMENDED"
        with self.assertRaises(DomainError):
            self.store.append(bad)

    def test_ingest_skips_duplicates_and_keeps_count_once(self):
        before = len(self.store.events)
        # 重投整个流（全部为重复）：一条都不得再计数
        stored = self.store.ingest(copy.deepcopy(self.store.events))
        self.assertEqual(stored, [])
        self.assertEqual(len(self.store.events), before)

    def test_dangling_causal_reference_rejected(self):
        last = self.store.events_for("list-gun")[-1]
        bad = copy.deepcopy(last)
        bad["event_id"] = "brand-new-3"
        bad["version"] = last["version"] + 1
        bad["occurred_at"] = "2026-10-01T00:00:00+08:00"
        bad["payload"] = {"list_id": "list-gun", "revision": 2,
                          "from_revision": 1, "to_revision": 2, "changes": [],
                          "amended_at": bad["occurred_at"]}
        bad["event_type"] = "RESULT_LIST_AMENDED"
        bad["caused_by"] = ["missing-event-id"]
        with self.assertRaises(DomainError):
            self.store.append(bad)


if __name__ == "__main__":
    unittest.main()
