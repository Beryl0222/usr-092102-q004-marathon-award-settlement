"""太原马拉松奖金核算端到端样例。

运行：python3 -m src.scenario
导出：data/taiyuan_marathon_events.jsonl（只增不改的完整事件流）

情节覆盖：
1. 三份"最终版"榜单并存：枪声总榜（前100）、净计时分枪榜、09-24 新加入的
   中国籍前八特别奖（奖项表 s2 生效期）——不同奖项种类必须采用对应榜单；
2. 人脸检录异常（何强）只进入替跑调查并暂缓权益，调查无违规后解除、照常放款；
3. 兴奋剂（李伟）立案即暂缓，禁止"先付款等结果"；签署裁决取消资格后榜单出修订、
   名次递补，未付款所以无旧账可改，只留更正记录；
4. 号码布争议（孙浩）赛后由仲裁签署确认转让，冲正已付款（原付款事实保留），
   后续选手按新修订差额补付；
5. 并列（王军/赵磊同成绩）按规则均分奖金池，递补后变为并列第1；
6. 名次奖与中国籍特别奖同组不叠加，破纪录奖可叠加；纪录先 ratify 后撤销，
   撤销走"全额收回原付款 + 按新冻结重发"的连续账目；
7. 申诉（ALI）立案即暂缓，裁决驳回后解除；
8. 重复事件、乱序版本在存储边界被拒绝。
"""
from __future__ import annotations

from pathlib import Path

from .model import replay
from .query import athlete_view, trace_amount
from .services import AwardService, PaymentService, DomainRuleError
from .services import (
    adjudicate_disqualification,
    amend_list_for_disqualification,
    build_event,
    decide_investigation,
    flag_face_mismatch,
)
from .store import EventStore

EDITION = "TY2026"
DIV = "M42"
GUN = "list-gun-M42"
NET = "list-net-waveA-M42"

# (entry_id, bib, name, nationality, gun_rank, net_rank)
ATHLETES = [
    ("E-LI", "001", "李伟", "CHN", 1, 1),
    ("E-WANG", "002", "王军", "CHN", 2, 2),
    ("E-ZHAO", "003", "赵磊", "CHN", 3, 4),
    ("E-ALI", "004", "ALI KIP", "KEN", 4, 11),
    ("E-SUN", "005", "孙浩", "CHN", 5, 3),
    ("E-ZHOU", "006", "周琦", "CHN", 6, 5),
    ("E-WU", "007", "吴迪", "CHN", 7, 6),
    ("E-HE", "008", "何强", "CHN", 8, 7),
    ("E-MA", "009", "马奔", "CHN", 9, 8),
    ("E-GAO", "010", "高翔", "CHN", 10, 9),
    ("E-LIN", "011", "林峰", "CHN", 11, 10),
]

PLACE_PURSES = {1: 8_000_000, 2: 6_000_000, 3: 4_500_000, 4: 3_000_000, 5: 2_000_000,
                6: 1_500_000, 7: 1_200_000, 8: 1_000_000, 9: 800_000, 10: 600_000}
CN_PURSES = {1: 3_000_000, 2: 2_500_000, 3: 2_000_000, 4: 1_500_000, 5: 1_200_000,
             6: 1_000_000, 7: 800_000, 8: 600_000}
RECORD_BONUS = 10_000_000


def build_scenario() -> tuple[EventStore, dict]:
    store = EventStore()
    awards = AwardService(store)
    payments = PaymentService(store)
    ctx: dict = {"calcs": {}, "freezes": {}}

    def put(ev: dict) -> dict:
        store.append(ev)
        return ev

    def prov(source_type, doc, at, *, issued_by=None, signed=False):
        p = {"source_type": source_type, "document_ref": doc, "captured_at": at, "signed": signed}
        if issued_by:
            p["issued_by"] = issued_by
        return p

    def jury_decision(decision_id, body, signed_by, doc, outcome, at, reason=""):
        return {"decision_id": decision_id, "deciding_body": body, "decided_at": at,
                "signed_by": signed_by, "document_ref": doc, "outcome": outcome, "reason": reason}

    def edition_event(event_type, payload, summary, at, source="race_organization",
                      doc="TY2026-bulletin", signed=False):
        return put(build_event(
            store, event_type=event_type, aggregate_id=f"edition-{EDITION}",
            payload=payload, provenance=prov(source, doc, at, signed=signed),
            summary=summary, occurred_at=at,
        ))

    # ---- 赛事、项目与规则生效期 ----
    edition_event("COMPETITION_REGISTERED",
                  {"edition_id": EDITION, "name": "2026 太原马拉松", "race_date": "2026-09-20"},
                  "2026 太原马拉松建档", "2026-08-01T09:00:00+08:00")
    edition_event("EVENT_PROGRAM_DEFINED",
                  {"edition_id": EDITION,
                   "divisions": [{"division_id": DIV, "name": "男子全程马拉松", "distance_m": 42195},
                                 {"division_id": "W42", "name": "女子全程马拉松", "distance_m": 42195}]},
                  "公布比赛项目", "2026-08-01T09:10:00+08:00")
    edition_event("RULEBOOK_PUBLISHED",
                  {"edition_id": EDITION, "rulebook_version": "RB-2026-1",
                   "effective_from": "2026-01-01T00:00:00+08:00",
                   "highlights": ["名次奖按枪声计时", "中国籍特别奖按净计时分枪成绩",
                                  "同组奖项不叠加取最高", "并列均分", "破纪录奖须经 ratify"]},
                  "2026 竞赛规则与奖励办法发布", "2026-08-01T09:20:00+08:00")
    edition_event("AWARD_SCHEDULE_PUBLISHED",
                  {"edition_id": EDITION, "schedule_version": "SCH-2026-S1",
                   "rulebook_version": "RB-2026-1", "effective_from": "2026-08-01T00:00:00+08:00",
                   "currency": "CNY",
                   "basis_rules": {"place": "gun_time", "record": "gun_time"},
                   "items": [
                       {"award_code": "PLACE_M", "award_kind": "place", "division_id": DIV,
                        "rank_purses": PLACE_PURSES, "tie_rule": "split",
                        "stack_group": "performance_M", "rule_ref": "RB-2026-1#3.1"},
                       {"award_code": "RECORD_M", "award_kind": "record", "division_id": DIV,
                        "amount_minor": RECORD_BONUS, "rule_ref": "RB-2026-1#3.4"},
                   ]},
                  "奖项表 S1（尚无中国籍特别奖）", "2026-08-01T09:30:00+08:00")
    edition_event("AWARD_SCHEDULE_PUBLISHED",
                  {"edition_id": EDITION, "schedule_version": "SCH-2026-S2",
                   "rulebook_version": "RB-2026-1", "effective_from": "2026-09-24T08:00:00+08:00",
                   "currency": "CNY",
                   "basis_rules": {"place": "gun_time", "special": "net_time", "record": "gun_time"},
                   "items": [
                       {"award_code": "PLACE_M", "award_kind": "place", "division_id": DIV,
                        "rank_purses": PLACE_PURSES, "tie_rule": "split",
                        "stack_group": "performance_M", "rule_ref": "RB-2026-1#3.1"},
                       {"award_code": "CN_TOP8_M", "award_kind": "special", "division_id": DIV,
                        "nationality": "CHN", "max_rank": 8, "rank_purses": CN_PURSES,
                        "stack_group": "performance_M", "rule_ref": "RB-2026-1#3.2"},
                       {"award_code": "RECORD_M", "award_kind": "record", "division_id": DIV,
                        "amount_minor": RECORD_BONUS, "rule_ref": "RB-2026-1#3.4"},
                   ]},
                  "奖项表 S2：加入中国籍运动员前八名特别奖（09-24 生效）",
                  "2026-09-24T08:00:00+08:00", doc="TY2026-schedule-S2")

    # ---- 报名、计时证据、检录 ----
    elig_ids: dict[str, str] = {}
    for entry_id, bib, name, nat, gun_rank, net_rank in ATHLETES:
        put(build_event(
            store, event_type="RACE_ENTRY_REGISTERED", aggregate_id=f"entry-{entry_id}",
            payload={"entry_id": entry_id, "bib": bib, "name": name, "division_id": DIV,
                     "registered_at": "2026-09-10T10:00:00+08:00"},
            provenance=prov("race_organization", f"reg-{bib}", "2026-09-10T10:00:00+08:00"),
            summary=f"{name}（{bib}）报名", occurred_at="2026-09-10T10:00:00+08:00",
        ))
        put(build_event(
            store, event_type="TIMING_RECEIVED", aggregate_id=f"timing-{entry_id}",
            payload={"entry_id": entry_id, "bib": bib, "timing_source": "chip_and_gun",
                     "evidence_ref": f"TIMEOFFICE/{bib}/2026",
                     "gun_rank": gun_rank, "net_rank": net_rank,
                     "gun_time_ms": 7_740_000 + gun_rank * 30_000,
                     "net_time_ms": 7_740_000 + net_rank * 25_000},
            provenance=prov("official_timing", f"TIMEOFFICE/{bib}/2026", "2026-09-20T11:30:00+08:00",
                            issued_by="太原马拉松计时中心"),
            summary=f"{bib} 官方计时证据入库", occurred_at="2026-09-20T11:30:00+08:00",
        ))
        ev = put(build_event(
            store, event_type="ELIGIBILITY_REVIEWED", aggregate_id=f"entry-{entry_id}",
            payload={"entry_id": entry_id, "nationality": nat, "nationality_verified": True,
                     "eligibility_status": "eligible",
                     "reviewed_at": "2026-09-22T10:00:00+08:00"},
            provenance=prov("race_organization", f"elig-{bib}", "2026-09-22T10:00:00+08:00"),
            summary=f"{bib} 国籍与参赛资格核验通过（{nat}）",
            occurred_at="2026-09-22T10:00:00+08:00",
        ))
        elig_ids[entry_id] = ev["event_id"]

    # ---- 两份榜单：枪声总榜（前100节选）与净计时分枪榜 ----
    gun_entries, net_entries = [], []
    for entry_id, bib, name, nat, gun_rank, net_rank in ATHLETES:
        row = {"entry_id": entry_id, "bib": bib, "rank": gun_rank,
               "time_ms": 7_740_000 + gun_rank * 30_000, "nationality": nat}
        if entry_id in ("E-WANG", "E-ZHAO"):  # 同成绩：并列第2，占用 2、3 名
            row["rank"] = 2 if entry_id == "E-WANG" else 3
            row["tied_rank"] = 2
            row["tie_group"] = "T1-2026"
            row["time_ms"] = 7_800_000
        gun_entries.append(row)
        net_entries.append({"entry_id": entry_id, "bib": bib, "rank": net_rank,
                            "time_ms": 7_740_000 + net_rank * 25_000, "nationality": nat})

    for list_id, basis, wave, entries, doc in (
        (GUN, "gun_time", None, gun_entries, "RESULT/gun/M42/r1"),
        (NET, "net_time", "wave-A", net_entries, "RESULT/net-waveA/M42/r1"),
    ):
        put(build_event(
            store, event_type="RESULT_LIST_PUBLISHED", aggregate_id=list_id,
            payload={"list_id": list_id, "division_id": DIV, "rank_basis": basis,
                     "wave": wave, "revision": 1,
                     "note": "前100名榜单节选" if basis == "gun_time" else "分枪净计时成绩（特别奖依据）",
                     "entries": entries},
            provenance=prov("official_timing", doc, "2026-09-21T12:00:00+08:00",
                            issued_by="太原马拉松计时中心"),
            summary=f"发布{('枪声总榜' if basis == 'gun_time' else '净计时分枪榜')} r1",
            occurred_at="2026-09-21T12:00:00+08:00",
        ))
        put(build_event(
            store, event_type="RESULT_LIST_FROZEN", aggregate_id=list_id,
            payload={"list_id": list_id, "revision": 1,
                     "frozen_at": "2026-09-24T09:00:00+08:00", "frozen_by": "竞赛秘书"},
            provenance=prov("race_organization", f"{doc}#freeze", "2026-09-24T09:00:00+08:00",
                            signed=True),
            summary=f"榜单 {list_id} r1 封存（后续只出修订版）",
            occurred_at="2026-09-24T09:00:00+08:00",
        ))

    # ---- 纪录申报（待 ratify）、人脸异常、申诉 ----
    put(build_event(
        store, event_type="RECORD_PERFORMANCE_DECLARED", aggregate_id="record-M-2026",
        payload={"record_id": "REC-M-2026", "division_id": DIV, "entry_id": "E-WANG",
                 "time_ms": 7_800_000, "list_id": GUN, "list_revision": 1,
                 "declared_at": "2026-09-22T15:00:00+08:00"},
        provenance=prov("race_organization", "RECORD/M-2026/declare", "2026-09-22T15:00:00+08:00"),
        summary="王军成绩申报赛会纪录（待田径管理机构 ratify，暂不计奖）",
        occurred_at="2026-09-22T15:00:00+08:00",
    ))

    put(flag_face_mismatch(
        store, case_id="CASE-FACE-008", entry_id="E-HE", bib="008",
        evidence_ref="FACE-GATE/008/2026", document_ref="FACE/008/flag",
        flagged_at="2026-09-21T09:05:00+08:00",
        note="终点人脸检录与报名照片相似度低，中立线索"))
    put(awards.suspend(
        hold_id="HOLD-FACE-008", entry_id="E-HE", reason_code="substitute_runner_investigation",
        case_ref="CASE-FACE-008", suspended_at="2026-09-22T09:00:00+08:00"))

    put(build_event(
        store, event_type="APPEAL_OPENED", aggregate_id="investigation-appeal-004",
        payload={"appeal_id": "APPEAL-004", "entry_id": "E-ALI",
                 "grounds": "对中途 checkpoint 计时标注提出异议",
                 "filed_at": "2026-09-23T14:00:00+08:00"},
        provenance=prov("appeal_tribunal", "APPEAL/004/file", "2026-09-23T14:00:00+08:00"),
        summary="ALI 就计时标注提出申诉", occurred_at="2026-09-23T14:00:00+08:00",
    ))
    put(awards.suspend(
        hold_id="HOLD-APPEAL-004", entry_id="E-ALI", reason_code="appeal",
        case_ref="APPEAL-004", suspended_at="2026-09-23T15:00:00+08:00"))

    # ---- 09-25 试算（provisional，纪录未 ratify 不计入） ----
    winners = [a[0] for a in ATHLETES if a[0] != "E-LIN"]
    for ev in awards.calculate(
        division_id=DIV, lists={"place": GUN, "special": NET, "record": GUN},
        edition_id=EDITION, calc_at="2026-09-25T10:00:00+08:00",
        status="provisional", only_entries=winners,
        eligibility_refs={e: [elig_ids[e]] for e in winners},
    ):
        put(ev)
        ctx["calcs"].setdefault(ev["payload"]["entry_id"], []).append(ev["payload"]["calc_id"])

    # ---- 09-26 兴奋剂立案：李伟（第1名）权益暂缓，付款日不得放款 ----
    put(build_event(
        store, event_type="DOPING_CASE_OPENED", aggregate_id="investigation-dope-001",
        payload={"case_id": "DOPE-2026-001", "entry_id": "E-LI",
                 "sample_ref": "SAMPLE/001/A", "authority": "中国反兴奋剂中心",
                 "opened_at": "2026-09-26T10:00:00+08:00"},
        provenance=prov("doping_authority", "DOPE/2026-001/open", "2026-09-26T10:00:00+08:00"),
        summary="李伟兴奋剂检查立案，权益暂缓", occurred_at="2026-09-26T10:00:00+08:00",
    ))
    put(awards.suspend(
        hold_id="HOLD-DOPE-001", entry_id="E-LI", reason_code="doping",
        case_ref="DOPE-2026-001", suspended_at="2026-09-26T11:00:00+08:00"))

    # ---- 09-28 纪录 ratify；人脸调查无违规；随后正式核算 ----
    put(build_event(
        store, event_type="RECORD_RATIFIED", aggregate_id="record-M-2026",
        payload={"record_id": "REC-M-2026", "ratified_at": "2026-09-28T11:00:00+08:00",
                 "ratified_by": "田径管理机构纪录委员会", "document_ref": "RECORD/M-2026/ratify"},
        provenance=prov("jury_decision", "RECORD/M-2026/ratify", "2026-09-28T11:00:00+08:00",
                        issued_by="田径管理机构纪录委员会", signed=True),
        summary="赛会纪录 ratify，破纪录奖进入核算", occurred_at="2026-09-28T11:00:00+08:00",
    ))
    face_decision = jury_decision(
        "DEC-FACE-008", "赛事仲裁委员会", "仲裁长 陈曦", "JURY/FACE-008/decide",
        "no_violation", "2026-09-29T10:00:00+08:00",
        reason="多机位比对为同一选手，系雨天面部遮挡导致相似度低，无替跑")
    put(decide_investigation(
        store, case_id="CASE-FACE-008", entry_id="E-HE",
        investigation_kind="substitute_runner", outcome="no_violation",
        decision=face_decision, decided_at="2026-09-29T10:00:00+08:00"))
    put(awards.resolve_hold(
        hold_id="HOLD-FACE-008", entry_id="E-HE", resolution="lifted",
        resolved_at="2026-09-29T10:30:00+08:00", decision_ref="JURY/FACE-008/decide"))

    confirmed_entries = [a[0] for a in ATHLETES if a[0] not in ("E-LI", "E-ALI", "E-LIN")]
    # E-HE 调查已于 10:30 解除，12:00 的正式核算包含之（放款另走 10-02 批次）
    supersedes_of = {e: ctx["calcs"][e][-1] for e in confirmed_entries if ctx["calcs"].get(e)}
    for ev in awards.calculate(
        division_id=DIV, lists={"place": GUN, "special": NET, "record": GUN},
        edition_id=EDITION, calc_at="2026-09-29T12:00:00+08:00",
        status="confirmed", only_entries=confirmed_entries, supersedes_of=supersedes_of,
        eligibility_refs={e: [elig_ids[e]] for e in confirmed_entries},
    ):
        put(ev)
        ctx["calcs"].setdefault(ev["payload"]["entry_id"], []).append(ev["payload"]["calc_id"])

    # 冻结、税务资料、计税（李伟因暂缓被挡在冻结之外）
    batch1_entries = [e for e in confirmed_entries if e != "E-HE"]
    for entry_id in batch1_entries:
        fid = f"FRZ-{entry_id}-R1"
        put(awards.freeze(entry_id=entry_id, freeze_id=fid,
                          frozen_at="2026-09-29T15:00:00+08:00", frozen_by="奖金核算员"))
        ctx["freezes"].setdefault(entry_id, []).append(fid)
    for entry_id, bib, *_ in ATHLETES:
        if entry_id == "E-LI":
            continue
        put(payments.submit_tax(
            entry_id=entry_id, tax_residency="CN" if entry_id != "E-ALI" else "KE",
            id_document_ref=f"TAX/{bib}/id", submitted_at="2026-09-24T16:00:00+08:00"))
    for entry_id in batch1_entries:
        rate = 1000 if entry_id == "E-ALI" else 2000
        put(payments.calculate_withholding(
            entry_id=entry_id, rate_bps=rate,
            calculated_at="2026-09-29T16:00:00+08:00",
            freeze_id=ctx["freezes"][entry_id][-1]))

    put(payments.open_batch(
        batch_id="BATCH-2026-0930", currency="CNY", entry_ids=batch1_entries,
        opened_at="2026-09-30T09:00:00+08:00"))
    payment_no = 0
    for entry_id in batch1_entries:
        payment_no += 1
        put(payments.release(
            payment_id=f"PAY-2026-{payment_no:03d}", batch_id="BATCH-2026-0930",
            entry_id=entry_id, paid_at="2026-09-30T10:00:00+08:00",
            freeze_id=ctx["freezes"][entry_id][-1]))

    # 何强：调查解除后冻结，10-02 随第二批放款
    fid = "FRZ-E-HE-R1"
    put(awards.freeze(entry_id="E-HE", freeze_id=fid,
                      frozen_at="2026-09-30T09:30:00+08:00", frozen_by="奖金核算员"))
    ctx["freezes"].setdefault("E-HE", []).append(fid)
    put(payments.calculate_withholding(
        entry_id="E-HE", rate_bps=2000, calculated_at="2026-09-30T10:30:00+08:00",
        freeze_id=fid))

    # ---- 10-01 ALI 申诉被驳回，解除暂缓 ----
    appeal_decision = jury_decision(
        "DEC-APPEAL-004", "赛事申诉仲裁庭", "仲裁员 李楠", "TRIBUNAL/APPEAL-004/decide",
        "dismissed", "2026-10-01T11:00:00+08:00", reason="checkpoint 标注不影响有效成绩")
    put(build_event(
        store, event_type="APPEAL_DECIDED", aggregate_id="investigation-appeal-004",
        payload={"appeal_id": "APPEAL-004", "entry_id": "E-ALI", "outcome": "dismissed",
                 "decision": appeal_decision},
        provenance=prov("appeal_tribunal", "TRIBUNAL/APPEAL-004/decide",
                        "2026-10-01T11:00:00+08:00", signed=True),
        summary="ALI 申诉驳回，成绩维持", occurred_at="2026-10-01T11:00:00+08:00",
    ))
    put(awards.resolve_hold(
        hold_id="HOLD-APPEAL-004", entry_id="E-ALI", resolution="lifted",
        resolved_at="2026-10-01T12:00:00+08:00", decision_ref="TRIBUNAL/APPEAL-004/decide"))
    # 申诉驳回后才出具正式核算（暂缓期间只允许 provisional）
    ali_calc_id = None
    for ev in awards.calculate(
        division_id=DIV, lists={"place": GUN, "special": NET, "record": GUN},
        edition_id=EDITION, calc_at="2026-10-01T13:00:00+08:00",
        status="confirmed", only_entries=["E-ALI"],
        supersedes_of={"E-ALI": ctx["calcs"]["E-ALI"][-1]},
        eligibility_refs={"E-ALI": [elig_ids["E-ALI"]]},
    ):
        put(ev)
        ali_calc_id = ev["payload"]["calc_id"]
        ctx["calcs"]["E-ALI"].append(ali_calc_id)
    fid = "FRZ-E-ALI-R1"
    put(awards.freeze(entry_id="E-ALI", freeze_id=fid, calc_id=ali_calc_id,
                      frozen_at="2026-10-01T14:00:00+08:00", frozen_by="奖金核算员"))
    ctx["freezes"].setdefault("E-ALI", []).append(fid)
    put(payments.calculate_withholding(
        entry_id="E-ALI", rate_bps=1000, calculated_at="2026-10-01T15:00:00+08:00",
        freeze_id=fid))
    put(payments.open_batch(
        batch_id="BATCH-2026-1002", currency="CNY", entry_ids=["E-ALI", "E-HE"],
        opened_at="2026-10-02T09:00:00+08:00"))
    put(payments.release(payment_id="PAY-2026-011", batch_id="BATCH-2026-1002",
                         entry_id="E-ALI", paid_at="2026-10-02T10:00:00+08:00",
                         freeze_id=fid))
    put(payments.release(payment_id="PAY-2026-012", batch_id="BATCH-2026-1002",
                         entry_id="E-HE", paid_at="2026-10-02T10:00:00+08:00",
                         freeze_id="FRZ-E-HE-R1"))

    # ---- 10-08 李伟兴奋剂阳性，签署裁决取消资格 ----
    put(build_event(
        store, event_type="DOPING_RESULT_ENTERED", aggregate_id="investigation-dope-001",
        payload={"case_id": "DOPE-2026-001", "entry_id": "E-LI", "result": "AAF_positive",
                 "document_ref": "DOPE/2026-001/result",
                 "entered_at": "2026-10-08T09:00:00+08:00"},
        provenance=prov("doping_authority", "DOPE/2026-001/result",
                        "2026-10-08T09:00:00+00:00", signed=True),
        summary="李伟兴奋剂检测 AAF 阳性", occurred_at="2026-10-08T09:00:00+08:00",
    ))
    dq_li = jury_decision(
        "DEC-DOPE-001", "赛事仲裁委员会", "仲裁长 陈曦", "JURY/DOPE-001/dq",
        "disqualified", "2026-10-08T10:00:00+08:00",
        reason="兴奋剂阳性，取消 2026 太原马拉松成绩与奖金资格")
    dope_result_id = next(e["event_id"] for e in store.events
                          if e["event_type"] == "DOPING_RESULT_ENTERED")
    dq_event = put(adjudicate_disqualification(
        store, case_id="DOPE-2026-001", entry_id="E-LI", decision=dq_li,
        decided_at="2026-10-08T10:00:00+08:00", trigger_event_id=dope_result_id))
    # 两份榜单都出修订（名次奖与特别奖依据各自榜单递补）
    for list_id in (GUN, NET):
        put(amend_list_for_disqualification(
            store, replay(store.events),
            list_id=list_id, withdrawn_entry_id="E-LI",
            reason="兴奋剂阳性取消资格", decision=dq_li, at="2026-10-08T11:00:00+08:00"))
    put(awards.resolve_hold(
        hold_id="HOLD-DOPE-001", entry_id="E-LI", resolution="disqualified",
        resolved_at="2026-10-08T11:30:00+08:00", decision_ref="JURY/DOPE-001/dq"))
    put(awards.void_entitlement(
        entry_id="E-LI", calc_id=ctx["calcs"]["E-LI"][0], decision=dq_li,
        voided_at="2026-10-08T12:00:00+08:00"))
    put(awards.revise_after_change(
        entry_id="E-LI", trigger_event_id=dq_event["event_id"],
        revised_at="2026-10-08T12:30:00+08:00", ledger_effect="none", after_calc_id=None))

    # 按 r2 重新核算受影响选手；旧核算标 superseded，旧冻结/付款原样保留
    r2_entries = [a[0] for a in ATHLETES if a[0] != "E-LI"]
    old_map = {e: ctx["calcs"][e][-1] for e in r2_entries if ctx["calcs"].get(e)}
    new_calcs = {}
    for ev in awards.calculate(
        division_id=DIV, lists={"place": GUN, "special": NET, "record": GUN},
        edition_id=EDITION, calc_at="2026-10-08T13:00:00+08:00",
        status="confirmed", only_entries=r2_entries, supersedes_of=old_map,
        eligibility_refs={e: [elig_ids[e], dq_event["event_id"]] for e in r2_entries},
    ):
        put(ev)
        new_calcs[ev["payload"]["entry_id"]] = ev["payload"]["calc_id"]
        ctx["calcs"].setdefault(ev["payload"]["entry_id"], []).append(ev["payload"]["calc_id"])
    promoted = list(new_calcs)

    # 冻结 r2 权益并计税；仅对金额增加者发起差额补付；林峰首次进入奖金区，全额补付
    world_r2 = replay(store.events)
    for entry_id in promoted:
        new_gross = world_r2.calcs[new_calcs[entry_id]].gross_amount["amount_minor"]
        prior_freeze_ids = ctx["freezes"].get(entry_id, [])
        prior_gross = (world_r2.freezes[prior_freeze_ids[-1]]["gross_amount"]["amount_minor"]
                       if prior_freeze_ids else 0)
        if new_gross <= prior_gross:
            continue
        fid = f"FRZ-{entry_id}-R2"
        put(awards.freeze(entry_id=entry_id, freeze_id=fid, calc_id=new_calcs[entry_id],
                          frozen_at="2026-10-08T14:00:00+08:00", frozen_by="奖金核算员"))
        ctx["freezes"].setdefault(entry_id, []).append(fid)
        put(payments.calculate_withholding(
            entry_id=entry_id, rate_bps=1000 if entry_id == "E-ALI" else 2000,
            calculated_at="2026-10-08T14:30:00+08:00", freeze_id=fid))
    seq = 20
    for entry_id in promoted:
        prior_freezes = ctx["freezes"].get(entry_id, [])
        if not any(f.startswith(f"FRZ-{entry_id}-R2") for f in prior_freezes):
            continue
        related = [p["payment_id"] for p in replay(store.events).settlements[entry_id].payments]
        seq += 1
        if len(prior_freezes) > 1:
            put(payments.makeup(
                makeup_id=f"MK-2026-{seq:03d}", entry_id=entry_id, reason_code="promotion",
                related_payment_ids=related, paid_at="2026-10-09T10:00:00+08:00",
                freeze_id=prior_freezes[-1], delta_against_freeze_id=prior_freezes[-2]))
        else:
            put(payments.makeup(
                makeup_id=f"MK-2026-{seq:03d}", entry_id=entry_id, reason_code="promotion",
                related_payment_ids=[], paid_at="2026-10-09T10:00:00+08:00",
                freeze_id=prior_freezes[-1]))
        put(awards.revise_after_change(
            entry_id=entry_id, trigger_event_id=dq_event["event_id"],
            revised_at="2026-10-09T11:00:00+08:00", ledger_effect="makeup",
            after_calc_id=new_calcs[entry_id]))

    # ---- 11-05 孙浩号码布争议：签署确认转让，冲正已付款，再次递补 ----
    put(build_event(
        store, event_type="BIB_DISPUTE_OPENED", aggregate_id="investigation-bib-005",
        payload={"case_id": "BIB-2026-005", "entry_id": "E-SUN", "bib": "005",
                 "opened_at": "2026-10-10T09:00:00+08:00",
                 "note": "影像复核发现号码布佩戴人与报名信息不符"},
        provenance=prov("race_organization", "BIB/2026-005/open", "2026-10-10T09:00:00+08:00"),
        summary="孙浩号码布争议立案", occurred_at="2026-10-10T09:00:00+08:00",
    ))
    dq_sun = jury_decision(
        "DEC-BIB-005", "赛事仲裁委员会", "仲裁长 陈曦", "JURY/BIB-005/dq",
        "bib_transfer_confirmed", "2026-11-05T10:00:00+08:00",
        reason="查实号码布转让替跑，取消成绩与资格")
    put(decide_investigation(
        store, case_id="BIB-2026-005", entry_id="E-SUN",
        investigation_kind="bib_dispute", outcome="bib_transfer_confirmed",
        decision=dq_sun, decided_at="2026-11-05T10:00:00+08:00"))
    dq_sun_event = put(adjudicate_disqualification(
        store, case_id="BIB-2026-005", entry_id="E-SUN", decision=dq_sun,
        decided_at="2026-11-05T10:30:00+08:00",
        trigger_event_id=next(e["event_id"] for e in store.events
                              if e["event_type"] == "INVESTIGATION_DECIDED"
                              and e["payload"]["case_id"] == "BIB-2026-005")))
    sun_settle = replay(store.events).settlements["E-SUN"]
    for p in sun_settle.payments:
        put(payments.reverse(
            reversal_id=f"REV-{p['payment_id']}", payment_id=p["payment_id"],
            entry_id="E-SUN", reason_code="disqualification",
            reversed_at="2026-11-05T11:00:00+08:00", decision_ref="JURY/BIB-005/dq"))
    for m in sun_settle.makeups:
        put(payments.reverse(
            reversal_id=f"REV-{m['makeup_id']}", payment_id=m["makeup_id"],
            entry_id="E-SUN", reason_code="disqualification",
            reversed_at="2026-11-05T11:05:00+08:00", decision_ref="JURY/BIB-005/dq",
            against="makeup"))
    for list_id in (GUN, NET):
        put(amend_list_for_disqualification(
            store, replay(store.events), list_id=list_id,
            withdrawn_entry_id="E-SUN", reason="号码布转让取消资格",
            decision=dq_sun, at="2026-11-05T11:30:00+08:00"))
    put(awards.void_entitlement(
        entry_id="E-SUN", calc_id=ctx["calcs"]["E-SUN"][-1], decision=dq_sun,
        voided_at="2026-11-05T12:00:00+08:00"))
    put(awards.revise_after_change(
        entry_id="E-SUN", trigger_event_id=dq_sun_event["event_id"],
        revised_at="2026-11-05T12:30:00+08:00", ledger_effect="clawback",
        after_calc_id=None))

    r3_entries = [a[0] for a in ATHLETES if a[0] not in ("E-LI", "E-SUN")]
    old_map3 = {e: ctx["calcs"][e][-1] for e in r3_entries if ctx["calcs"].get(e)}
    new_calcs3 = {}
    for ev in awards.calculate(
        division_id=DIV, lists={"place": GUN, "special": NET, "record": GUN},
        edition_id=EDITION, calc_at="2026-11-06T10:00:00+08:00",
        status="confirmed", only_entries=r3_entries, supersedes_of=old_map3,
        eligibility_refs={e: [elig_ids[e], dq_sun_event["event_id"]] for e in r3_entries},
    ):
        put(ev)
        new_calcs3[ev["payload"]["entry_id"]] = ev["payload"]["calc_id"]
        ctx["calcs"].setdefault(ev["payload"]["entry_id"], []).append(ev["payload"]["calc_id"])
    seq = 40
    world_r3 = replay(store.events)
    for entry_id, calc_id in new_calcs3.items():
        new_gross = world_r3.calcs[calc_id].gross_amount["amount_minor"]
        prior_freeze_ids = ctx["freezes"].get(entry_id, [])
        previous_freeze_id = prior_freeze_ids[-1] if prior_freeze_ids else None
        prior_gross = (world_r3.freezes[previous_freeze_id]["gross_amount"]["amount_minor"]
                       if previous_freeze_id else 0)
        if new_gross <= prior_gross:
            continue
        fid = f"FRZ-{entry_id}-R3"
        put(awards.freeze(entry_id=entry_id, freeze_id=fid, calc_id=calc_id,
                          frozen_at="2026-11-06T14:00:00+08:00", frozen_by="奖金核算员"))
        ctx["freezes"].setdefault(entry_id, []).append(fid)
        put(payments.calculate_withholding(
            entry_id=entry_id, rate_bps=2000,
            calculated_at="2026-11-06T14:30:00+08:00", freeze_id=fid))
        seq += 1
        put(payments.makeup(
            makeup_id=f"MK-2026-{seq:03d}", entry_id=entry_id, reason_code="promotion",
            related_payment_ids=[p["payment_id"] for p in
                                 replay(store.events).settlements[entry_id].payments],
            paid_at="2026-11-07T10:00:00+08:00",
            freeze_id=fid, delta_against_freeze_id=previous_freeze_id))
        put(awards.revise_after_change(
            entry_id=entry_id, trigger_event_id=dq_sun_event["event_id"],
            revised_at="2026-11-07T11:00:00+08:00", ledger_effect="makeup",
            after_calc_id=calc_id))

    # ---- 12-10 赛会纪录撤销：王军破纪录奖被收回，原付款不抹除 ----
    revoke = jury_decision(
        "DEC-REC-2026", "田径管理机构纪录委员会", "委员 郑海", "RECORD/M-2026/revoke",
        "record_revoked", "2026-12-10T10:00:00+08:00",
        reason="赛道复测发现起终点高差超标，成绩不构成赛会纪录")
    put(build_event(
        store, event_type="RECORD_REVOKED", aggregate_id="record-M-2026",
        payload={"record_id": "REC-M-2026", "revoked_at": "2026-12-10T10:00:00+08:00",
                 "decision": revoke},
        provenance=prov("jury_decision", "RECORD/M-2026/revoke",
                        "2026-12-10T10:00:00+08:00", signed=True),
        summary="赛会纪录经签署裁决撤销", occurred_at="2026-12-10T10:00:00+08:00",
    ))
    revoke_event_id = store.events[-1]["event_id"]
    new_calc_w = None
    for ev in awards.calculate(
        division_id=DIV, lists={"place": GUN, "special": NET, "record": GUN},
        edition_id=EDITION, calc_at="2026-12-10T11:00:00+08:00",
        status="confirmed", only_entries=["E-WANG"],
        supersedes_of={"E-WANG": ctx["calcs"]["E-WANG"][-1]},
        eligibility_refs={"E-WANG": [elig_ids["E-WANG"], revoke_event_id]},
    ):
        put(ev)
        new_calc_w = ev["payload"]["calc_id"]
        ctx["calcs"]["E-WANG"].append(new_calc_w)
    fid = "FRZ-E-WANG-R4"
    put(awards.freeze(entry_id="E-WANG", freeze_id=fid, calc_id=new_calc_w,
                      frozen_at="2026-12-10T12:00:00+08:00", frozen_by="财务主管"))
    ctx["freezes"]["E-WANG"].append(fid)
    put(payments.calculate_withholding(
        entry_id="E-WANG", rate_bps=2000,
        calculated_at="2026-12-10T13:00:00+08:00", freeze_id=fid))
    wang_snap = replay(store.events).settlements["E-WANG"]
    reversed_pay_ids = {r["payment_id"] for r in wang_snap.reversals}
    for p in wang_snap.payments:
        if p["payment_id"] not in reversed_pay_ids:
            put(payments.reverse(
                reversal_id=f"REV-{p['payment_id']}", payment_id=p["payment_id"],
                entry_id="E-WANG", reason_code="overpayment",
                reversed_at="2026-12-10T14:00:00+08:00", decision_ref="RECORD/M-2026/revoke"))
    for m in wang_snap.makeups:
        if m["makeup_id"] not in reversed_pay_ids:
            put(payments.reverse(
                reversal_id=f"REV-{m['makeup_id']}", payment_id=m["makeup_id"],
                entry_id="E-WANG", reason_code="overpayment",
                reversed_at="2026-12-10T14:05:00+08:00",
                decision_ref="RECORD/M-2026/revoke", against="makeup"))
    put(payments.makeup(
        makeup_id="MK-2026-099", entry_id="E-WANG", reason_code="administrative",
        related_payment_ids=[p["payment_id"] for p in wang_snap.payments]
                            + [m["makeup_id"] for m in wang_snap.makeups],
        paid_at="2026-12-10T15:00:00+08:00", freeze_id=fid))
    put(awards.revise_after_change(
        entry_id="E-WANG", trigger_event_id=revoke_event_id,
        revised_at="2026-12-10T15:30:00+08:00", ledger_effect="clawback",
        after_calc_id=new_calc_w))

    return store, ctx


def narrative(store: EventStore) -> str:
    world = replay(store.events)
    lines = ["=" * 72, "太原马拉松奖金核算台 — 端到端事件流", "=" * 72,
             f"事件总数：{len(store.events)}（只增不改）", ""]
    for entry_id in ("E-WANG", "E-ZHAO", "E-LI", "E-HE", "E-SUN", "E-LIN"):
        view = athlete_view(world, entry_id)
        name = world.entries[entry_id].name
        lines.append(f"【{name} {entry_id}】资格={view['eligibility_status']} "
                     f"净付累计={view['ledger']['net_paid_minor']/100:.2f} 元")
        r = view["results"][0] if view["results"] else None
        if r:
            lines.append(f"  当前名次：{r['list_id']} r{r['list_revision']} 第 {r['rank']} 名"
                         + (f"（并列 {r['tied_rank']}）" if r.get("tied_rank") else ""))
        else:
            lines.append("  当前榜单：已因裁决撤出（原名次与付款事实仍保留在事件流）")
        for item in view["applicable_awards"]["line_items"]:
            lines.append(
                f"  奖项 {item['award_code']}（{item['award_kind']}）"
                f"{item['amount']['amount_minor']/100:.2f} 元"
                + (f"  {item['tie_share_note']}" if item.get("tie_share_note") else ""))
        if view["pending"]["active_holds"] or view["pending"]["open_cases"]:
            lines.append(f"  未决：{view['pending']}")
        lines.append("")

    trace = trace_amount(store.events, entry_id="E-WANG",
                         payment_id=next(iter(
                             replay(store.events).settlements["E-WANG"].payments
                         ))["payment_id"])
    lines.append("审计追踪（王军首笔付款）")
    lines.append(f"  采用榜单：" + ", ".join(
        f"{kind}={v['list_id']}#r{v['list_revision']}({v['rank_basis']})"
        for kind, v in trace["adopted_basis"]["list_refs"].items()))
    lines.append(f"  规则版本：{trace['adopted_basis']['rulebook_version']} / "
                 f"奖项表：{trace['adopted_basis']['schedule_version']}")
    lines.append(f"  按冻结时点重算金额：{trace['recomputed']['gross_minor']/100:.2f} 元 "
                 f"（与冻结值一致={trace['recomputed']['matches_frozen']}）")
    lines.append(f"  裁决链事件数：{len(trace['decision_chain'])}")
    return "\n".join(lines)


def main() -> None:
    store, _ctx = build_scenario()
    out = Path(__file__).resolve().parents[1] / "data" / "taiyuan_marathon_events.jsonl"
    store.write_jsonl(out)
    print(narrative(store))
    print(f"\n事件流已导出：{out}")


if __name__ == "__main__":
    main()
