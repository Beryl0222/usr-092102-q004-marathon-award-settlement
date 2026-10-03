"""测试夹具：构造最小但完整的赛事世界（枪声榜+净计时榜+奖项表 S2）。"""
from __future__ import annotations

from src.services import AwardService, PaymentService, build_event
from src.store import EventStore

EDITION = "TY"
DIV = "M42"
GUN = "list-gun"
NET = "list-net"
T0 = "2026-09-01T00:00:00+08:00"

PURSES = {1: 80000, 2: 60000, 3: 45000}
CN = {1: 30000, 2: 25000}


def prov(source="race_organization", doc="doc", at=T0, signed=False):
    return {"source_type": source, "document_ref": doc, "captured_at": at, "signed": signed}


def build_world(ranks=None, *, tie: bool = False):
    """ranks: [(entry_id, bib, name, nat, gun_rank, net_rank), ...]。"""
    store = EventStore()
    awards = AwardService(store)
    payments = PaymentService(store)
    if ranks is None:
        ranks = [
            ("E1", "001", "甲", "CHN", 1, 1),
            ("E2", "002", "乙", "CHN", 2, 2),
            ("E3", "003", "丙", "KEN", 3, 3),
            ("E4", "004", "丁", "CHN", 4, 4),
        ]

    def put(ev):
        store.append(ev)
        return ev

    put(build_event(store, event_type="COMPETITION_REGISTERED", aggregate_id=f"edition-{EDITION}",
                    payload={"edition_id": EDITION, "name": "测试赛", "race_date": "2026-09-20"},
                    provenance=prov(), summary="建档", occurred_at="2026-08-01T09:00:00+08:00"))
    put(build_event(store, event_type="EVENT_PROGRAM_DEFINED", aggregate_id=f"edition-{EDITION}",
                    payload={"edition_id": EDITION,
                             "divisions": [{"division_id": DIV, "name": "男子", "distance_m": 42195}]},
                    provenance=prov(), summary="项目", occurred_at="2026-08-01T09:10:00+08:00"))
    put(build_event(store, event_type="RULEBOOK_PUBLISHED", aggregate_id=f"edition-{EDITION}",
                    payload={"edition_id": EDITION, "rulebook_version": "RB1",
                             "effective_from": "2026-01-01T00:00:00+08:00"},
                    provenance=prov(), summary="规则", occurred_at="2026-08-01T09:20:00+08:00"))
    put(build_event(store, event_type="AWARD_SCHEDULE_PUBLISHED", aggregate_id=f"edition-{EDITION}",
                    payload={"edition_id": EDITION, "schedule_version": "S2", "rulebook_version": "RB1",
                             "effective_from": "2026-09-01T00:00:00+08:00", "currency": "CNY",
                             "basis_rules": {"place": "gun_time", "special": "net_time"},
                             "items": [
                                 {"award_code": "PLACE", "award_kind": "place", "division_id": DIV,
                                  "rank_purses": PURSES, "tie_rule": "split",
                                  "stack_group": "perf", "rule_ref": "r1"},
                                 {"award_code": "CN", "award_kind": "special", "division_id": DIV,
                                  "nationality": "CHN", "max_rank": 2, "rank_purses": CN,
                                  "stack_group": "perf", "rule_ref": "r2"},
                             ]},
                    provenance=prov(doc="S2"), summary="奖项表", occurred_at="2026-09-01T00:00:00+08:00"))

    elig = {}
    gun_rows, net_rows = [], []
    for entry_id, bib, name, nat, gr, nr in ranks:
        put(build_event(store, event_type="RACE_ENTRY_REGISTERED", aggregate_id=f"entry-{entry_id}",
                        payload={"entry_id": entry_id, "bib": bib, "name": name, "division_id": DIV,
                                 "registered_at": "2026-09-10T10:00:00+08:00"},
                        provenance=prov(doc=f"reg-{bib}"), summary=f"{name}报名",
                        occurred_at="2026-09-10T10:00:00+08:00"))
        ev = put(build_event(store, event_type="ELIGIBILITY_REVIEWED", aggregate_id=f"entry-{entry_id}",
                             payload={"entry_id": entry_id, "nationality": nat,
                                      "nationality_verified": True, "eligibility_status": "eligible",
                                      "reviewed_at": "2026-09-22T10:00:00+08:00"},
                             provenance=prov(doc=f"elig-{bib}"), summary="资格通过",
                             occurred_at="2026-09-22T10:00:00+08:00"))
        elig[entry_id] = ev["event_id"]
        grow = {"entry_id": entry_id, "bib": bib, "rank": gr, "time_ms": 7_800_000 + gr * 1000,
                "nationality": nat}
        gun_rows.append(grow)
        net_rows.append({"entry_id": entry_id, "bib": bib, "rank": nr,
                         "time_ms": 7_800_000 + nr * 1000, "nationality": nat})

    if tie:
        # E1、E2 同成绩并列第 1（占用 1、2 名）
        for row, eid in ((gun_rows[0], "E1"), (gun_rows[1], "E2")):
            row["rank"] = 1 if eid == "E1" else 2
            row["tied_rank"] = 1
            row["tie_group"] = "TG"
            row["time_ms"] = 7_801_000

    put(build_event(store, event_type="RESULT_LIST_PUBLISHED", aggregate_id=GUN,
                    payload={"list_id": GUN, "division_id": DIV, "rank_basis": "gun_time",
                             "wave": None, "revision": 1, "entries": gun_rows},
                    provenance=prov("official_timing", "gun", "2026-09-21T12:00:00+08:00"),
                    summary="枪声榜", occurred_at="2026-09-21T12:00:00+08:00"))
    put(build_event(store, event_type="RESULT_LIST_PUBLISHED", aggregate_id=NET,
                    payload={"list_id": NET, "division_id": DIV, "rank_basis": "net_time",
                             "wave": "waveA", "revision": 1, "entries": net_rows},
                    provenance=prov("official_timing", "net", "2026-09-21T12:05:00+08:00"),
                    summary="净计时榜", occurred_at="2026-09-21T12:05:00+08:00"))
    return store, awards, payments, elig
