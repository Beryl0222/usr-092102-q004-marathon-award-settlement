import unittest

from src.model import replay
from tests.fixtures import DIV, EDITION, GUN, NET, build_world
from src.services import build_event

LISTS = {"place": GUN, "special": NET, "record": GUN}


def add_record_award(store, *, amount=100000):
    """在奖项表中加入破纪录奖（重发 S3）。"""
    store.append(build_event(
        store, event_type="AWARD_SCHEDULE_PUBLISHED", aggregate_id=f"edition-{EDITION}",
        payload={"edition_id": EDITION, "schedule_version": "S3", "rulebook_version": "RB1",
                 "effective_from": "2026-09-22T00:00:00+08:00", "currency": "CNY",
                 "basis_rules": {"place": "gun_time", "special": "net_time", "record": "gun_time"},
                 "items": [
                     {"award_code": "PLACE", "award_kind": "place", "division_id": DIV,
                      "rank_purses": {1: 80000, 2: 60000, 3: 45000},
                      "tie_rule": "split", "rule_ref": "r1"},
                     {"award_code": "REC", "award_kind": "record", "division_id": DIV,
                      "amount_minor": amount, "rule_ref": "r3"},
                 ]},
        provenance={"source_type": "race_organization", "document_ref": "S3",
                    "captured_at": "2026-09-22T00:00:00+08:00", "signed": False},
        summary="S3 含破纪录奖", occurred_at="2026-09-22T00:00:00+08:00"))


def gross(store, awards, entry_id, at):
    evs = awards.calculate(division_id=DIV, lists=LISTS, edition_id=EDITION,
                           calc_at=at, only_entries=[entry_id])
    for ev in evs:
        store.append(ev)
    payload = evs[0]["payload"] if evs else None
    return (payload["gross_amount"]["amount_minor"] if payload else 0,
            {li["award_code"] for li in payload["line_items"]} if payload else set())


class RecordTest(unittest.TestCase):
    def test_declared_record_not_paid_until_ratified(self):
        store, awards, *_ = build_world()
        add_record_award(store)
        store.append(build_event(
            store, event_type="RECORD_PERFORMANCE_DECLARED", aggregate_id="rec-1",
            payload={"record_id": "R1", "division_id": DIV, "entry_id": "E1", "time_ms": 7_801_000,
                     "list_id": GUN, "list_revision": 1, "declared_at": "2026-09-22T10:00:00+08:00"},
            provenance={"source_type": "race_organization", "document_ref": "rd",
                        "captured_at": "2026-09-22T10:00:00+08:00"},
            summary="申报纪录", occurred_at="2026-09-22T10:00:00+08:00"))
        amount, codes = gross(store, awards, "E1", "2026-09-23T10:00:00+08:00")
        self.assertNotIn("REC", codes)  # 未 ratify 不计破纪录奖

        store.append(build_event(
            store, event_type="RECORD_RATIFIED", aggregate_id="rec-1",
            payload={"record_id": "R1", "ratified_at": "2026-09-28T11:00:00+08:00",
                     "ratified_by": "纪录委员会", "document_ref": "rr"},
            provenance={"source_type": "jury_decision", "document_ref": "rr",
                        "captured_at": "2026-09-28T11:00:00+08:00", "signed": True},
            summary="ratify", occurred_at="2026-09-28T11:00:00+08:00"))
        amount, codes = gross(store, awards, "E1", "2026-09-29T10:00:00+08:00")
        self.assertIn("REC", codes)
        self.assertEqual(amount, 80000 + 100000)  # 名次奖 + 破纪录奖可叠加

        # 撤销后破纪录奖移除（名次奖保留）
        decision = {"decision_id": "DR", "deciding_body": "纪录委员会",
                    "decided_at": "2026-12-10T10:00:00+08:00", "signed_by": "委员",
                    "document_ref": "rv", "outcome": "record_revoked", "reason": "高差超标"}
        store.append(build_event(
            store, event_type="RECORD_REVOKED", aggregate_id="rec-1",
            payload={"record_id": "R1", "revoked_at": "2026-12-10T10:00:00+08:00",
                     "decision": decision},
            provenance={"source_type": "jury_decision", "document_ref": "rv",
                        "captured_at": "2026-12-10T10:00:00+08:00", "signed": True},
            summary="撤销纪录", occurred_at="2026-12-10T10:00:00+08:00"))
        amount, codes = gross(store, awards, "E1", "2026-12-10T11:00:00+08:00")
        self.assertNotIn("REC", codes)
        self.assertEqual(amount, 80000)


class RuleEffectivePeriodTest(unittest.TestCase):
    def test_schedule_at_picks_effective_version(self):
        store, *_ = build_world()
        world = replay(store.events)
        # 夹具中 S2 自 2026-09-01 生效；S1 不存在，构造一个更早的 S0
        before = world.schedule_at(EDITION, "2026-09-01T00:00:00+08:00")
        self.assertEqual(before["schedule_version"], "S2")
        self.assertIsNone(world.schedule_at(EDITION, "2026-08-31T23:59:59+08:00"))

    def test_special_award_absent_under_earlier_schedule(self):
        # S2 之前的核算不应凭空得到中国籍特别奖
        store, awards, *_ = build_world()
        world = replay(store.events)
        schedule = world.schedule_at(EDITION, "2026-08-15T00:00:00+08:00")
        self.assertIsNone(schedule)


if __name__ == "__main__":
    unittest.main()
