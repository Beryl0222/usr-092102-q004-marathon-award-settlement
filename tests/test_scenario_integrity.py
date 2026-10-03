import json
import unittest
from pathlib import Path

from src.model import replay
from src.query import trace_amount
from src.store import EventStore
from src.validator import validate_event

ROOT = Path(__file__).parents[1]
JSONL = ROOT / "data" / "taiyuan_marathon_events.jsonl"


def load_events():
    return [json.loads(line) for line in JSONL.read_text(encoding="utf-8").splitlines()
            if line.strip()]


class ScenarioIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = load_events()

    def test_all_events_valid(self):
        for i, event in enumerate(self.events, 1):
            self.assertEqual(validate_event(event), [], f"行 {i}: {event['event_id']}")

    def test_event_ids_unique_and_versions_contiguous(self):
        seen = set()
        versions = {}
        for event in self.events:
            self.assertNotIn(event["event_id"], seen)
            seen.add(event["event_id"])
            key = event["aggregate_id"]
            self.assertEqual(event["version"], versions.get(key, 0) + 1)
            versions[key] = event["version"]

    def test_roundtrip_jsonl_replay_is_deterministic(self):
        store = EventStore()
        store.load_jsonl(JSONL)
        self.assertEqual(len(store.events), len(self.events))
        w1 = replay(store.events)
        w2 = replay([dict(e) for e in store.events])
        self.assertEqual(
            {f: w1.freezes[f]["gross_amount"]["amount_minor"] for f in w1.freezes},
            {f: w2.freezes[f]["gross_amount"]["amount_minor"] for f in w2.freezes},
        )

    def test_every_freeze_amount_recomputes_exactly(self):
        """每个冻结权益都能用其榜单修订/规则版本独立重算出完全相同的总额。"""
        world = replay(self.events)
        for freeze_id, freeze in world.freezes.items():
            trace = trace_amount(self.events, entry_id=freeze["entry_id"], freeze_id=freeze_id)
            self.assertTrue(
                trace["recomputed"]["matches_frozen"],
                f"冻结 {freeze_id} 重算不符：{trace['recomputed']['difference_minor']}",
            )

    def test_every_paid_payment_has_freeze_and_withholding(self):
        world = replay(self.events)
        for entry_id, settlement in world.settlements.items():
            for payment in settlement.payments:
                self.assertIn(payment["freeze_id"], world.freezes)
                self.assertIsNotNone(
                    world.withholding_for(entry_id, payment["freeze_id"]))

    def test_reversed_payments_keep_original_fact(self):
        """冲正不删除原付款：原 PAYMENT_RELEASED 仍在流中，且冲正金额=原净额。"""
        world = replay(self.events)
        for entry_id, settlement in world.settlements.items():
            by_id = {p["payment_id"]: p for p in settlement.payments}
            by_id.update({m["makeup_id"]: m for m in settlement.makeups})
            for reversal in settlement.reversals:
                original = by_id[reversal["payment_id"]]
                self.assertEqual(reversal["amount"]["amount_minor"],
                                 original["net_amount"]["amount_minor"])

    def test_reingest_is_idempotent(self):
        store = EventStore()
        store.load_jsonl(JSONL)
        again = store.ingest([json.loads(json.dumps(e)) for e in self.events])
        self.assertEqual(again, [])
        self.assertEqual(len(store.events), len(self.events))

    def test_disqualified_entries_never_paid_while_held(self):
        """李伟在暂缓期间不存在任何付款（禁止先付款等结果）。"""
        world = replay(self.events)
        self.assertEqual(world.settlements["E-LI"].payments, [])


if __name__ == "__main__":
    unittest.main()
