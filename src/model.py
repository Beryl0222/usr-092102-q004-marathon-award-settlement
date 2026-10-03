"""事件流归约模型与奖金计算引擎。

只依赖事件事实重放出现态；任何时刻删除并重建都得到同一结果。
榜单修订以 RESULT_LIST_AMENDED 的变更明细逐条叠加，每一次名次变动都可回看。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

# ---- 奖项表（schedule.items）规范结构 -----------------------------------
# {
#   "award_code", "award_kind": place|special|record, "division_id",
#   "rank_purses": {"1": minor, ...},        # place：按总名次
#   "max_rank": 8,                            # special：按国籍子名次上限
#   "nationality": "CHN",                     # special：国籍过滤
#   "amount_minor": 5000000,                  # record：固定额
#   "tie_rule": "split"|"full",               # 并列处理
#   "stack_group": "performance_M"            # 同组只取最高，缺省可叠加
# }


@dataclass
class Entry:
    entry_id: str
    bib: str
    name: str
    division_id: str
    nationality: str | None = None
    nationality_verified: bool = False
    eligibility_status: str = "unknown"  # unknown|eligible|pending|ineligible|disqualified
    registered_at: str | None = None


@dataclass
class ResultList:
    list_id: str
    division_id: str
    rank_basis: str  # gun_time|net_time
    wave: str | None
    revision: int
    frozen: bool = False
    frozen_at: str | None = None
    entries: dict[str, dict] = field(default_factory=dict)  # entry_id -> list_entry
    amendments: list[dict] = field(default_factory=list)


@dataclass
class Case:
    case_id: str
    kind: str  # face_mismatch|bib_dispute|doping|appeal
    entry_id: str
    status: str = "open"  # open|decided
    outcome: str | None = None
    decision: dict | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Record:
    record_id: str
    division_id: str
    entry_id: str
    time_ms: int
    status: str = "declared"  # declared|ratified|revoked
    decision: dict | None = None


@dataclass
class Calc:
    calc_id: str
    entry_id: str
    division_id: str
    basis: dict
    line_items: list[dict]
    gross_amount: dict
    status: str = "provisional"  # provisional|confirmed|superseded
    supersedes: str | None = None


@dataclass
class Settlement:
    tax: dict | None = None
    withholdings: list[dict] = field(default_factory=list)
    payments: list[dict] = field(default_factory=list)
    reversals: list[dict] = field(default_factory=list)
    makeups: list[dict] = field(default_factory=list)

    @property
    def withholding(self) -> dict | None:
        return self.withholdings[-1] if self.withholdings else None


@dataclass
class World:
    editions: dict[str, dict] = field(default_factory=dict)
    entries: dict[str, Entry] = field(default_factory=dict)
    bib_index: dict[str, str] = field(default_factory=dict)  # (division,bib)->entry_id
    timing: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    lists: dict[str, ResultList] = field(default_factory=dict)
    cases: dict[str, Case] = field(default_factory=dict)
    records: dict[str, Record] = field(default_factory=dict)
    calcs: dict[str, Calc] = field(default_factory=dict)
    calcs_by_entry: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    holds: dict[str, dict] = field(default_factory=dict)  # hold_id -> hold
    freezes: dict[str, dict] = field(default_factory=dict)  # freeze_id -> freeze
    freeze_ids_by_entry: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    voided: dict[str, dict] = field(default_factory=dict)
    settlements: dict[str, Settlement] = field(default_factory=lambda: defaultdict(Settlement))
    batches: dict[str, dict] = field(default_factory=dict)
    revisions: list[dict] = field(default_factory=list)

    # -- 便捷查询 --
    def edition_of(self, division_id: str) -> dict | None:
        for edition in self.editions.values():
            if division_id in edition["divisions"]:
                return edition
        return None

    def active_holds(self, entry_id: str) -> list[dict]:
        return [h for h in self.holds.values() if h["entry_id"] == entry_id and h["status"] == "active"]

    def open_cases(self, entry_id: str) -> list[Case]:
        return [c for c in self.cases.values() if c.entry_id == entry_id and c.status == "open"]

    def latest_calc(self, entry_id: str) -> Calc | None:
        ids = self.calcs_by_entry.get(entry_id, [])
        return self.calcs[ids[-1]] if ids else None

    def freeze_for(self, entry_id: str) -> dict | None:
        """最新一次冻结（旧冻结仍在 freezes 中保留）。"""
        ids = self.freeze_ids_by_entry.get(entry_id, [])
        return self.freezes[ids[-1]] if ids else None

    def freeze_of_calc(self, calc_id: str) -> dict | None:
        return next(
            (f for f in self.freezes.values() if f["calc_id"] == calc_id), None
        )

    def paid_freeze_ids(self, entry_id: str) -> set[str]:
        """存在未被冲正付款的冻结集合。"""
        settlement = self.settlements[entry_id]
        reversed_payments = {r["payment_id"] for r in settlement.reversals}
        return {
            p["freeze_id"]
            for p in settlement.payments
            if p["payment_id"] not in reversed_payments
        }

    def withholding_for(self, entry_id: str, freeze_id: str) -> dict | None:
        settlement = self.settlements[entry_id]
        if settlement.withholding and settlement.withholding["freeze_id"] == freeze_id:
            return settlement.withholding
        return next(
            (w for w in settlement.withholdings if w["freeze_id"] == freeze_id), None
        )

    def schedule_at(self, edition_id: str, at: str) -> dict | None:
        """返回该时刻已生效的最新奖项表。"""
        edition = self.editions.get(edition_id)
        if not edition:
            return None
        candidates = [
            s for s in edition["schedules"].values() if s["effective_from"] <= at
        ]
        return max(candidates, key=lambda s: s["effective_from"]) if candidates else None

    def rulebook_at(self, edition_id: str, at: str) -> dict | None:
        edition = self.editions.get(edition_id)
        if not edition:
            return None
        candidates = [
            r for r in edition["rulebooks"].values() if r["effective_from"] <= at
        ]
        return max(candidates, key=lambda r: r["effective_from"]) if candidates else None


def _apply_list_change(lst: ResultList, change: dict) -> None:
    op = change["op"]
    entry_id = change["entry_id"]
    if op == "withdraw":
        lst.entries.pop(entry_id, None)
    elif op == "reinstate":
        if change.get("after"):
            lst.entries[entry_id] = change["after"]
    elif op == "correct":
        if change.get("after"):
            lst.entries[entry_id] = change["after"]
    elif op == "annotate":
        current = lst.entries.get(entry_id)
        if current is not None:
            current["annotation"] = change.get("after", {}).get(
                "annotation", change.get("reason")
            )
    lst.amendments.append(change)


def apply_event(world: World, event: dict) -> None:
    et = event["event_type"]
    p = event["payload"]

    if et == "COMPETITION_REGISTERED":
        world.editions[p["edition_id"]] = {
            "edition_id": p["edition_id"],
            "name": p["name"],
            "race_date": p["race_date"],
            "divisions": {},
            "rulebooks": {},
            "schedules": {},
        }
    elif et == "EVENT_PROGRAM_DEFINED":
        edition = world.editions[p["edition_id"]]
        for div in p["divisions"]:
            edition["divisions"][div["division_id"]] = div
    elif et == "RULEBOOK_PUBLISHED":
        # 规则可挂在赛事版本下（payload 带 edition_id 时）
        edition = world.editions.get(p.get("edition_id", ""))
        if edition is not None:
            edition["rulebooks"][p["rulebook_version"]] = p
    elif et == "AWARD_SCHEDULE_PUBLISHED":
        edition = world.editions.get(p.get("edition_id", ""))
        if edition is not None:
            edition["schedules"][p["schedule_version"]] = p
    elif et == "RACE_ENTRY_REGISTERED":
        e = Entry(
            entry_id=p["entry_id"],
            bib=p["bib"],
            name=p["name"],
            division_id=p["division_id"],
            registered_at=p.get("registered_at"),
        )
        world.entries[p["entry_id"]] = e
        world.bib_index[f"{p['division_id']}:{p['bib']}"] = p["entry_id"]
    elif et == "TIMING_RECEIVED":
        world.timing[p["entry_id"]].append(p)
    elif et == "BIB_CHECKPOINT_RECORDED":
        world.timing[f"bib:{p['bib']}"].append(p)
    elif et == "ELIGIBILITY_REVIEWED":
        e = world.entries[p["entry_id"]]
        e.nationality = p.get("nationality")
        e.nationality_verified = p.get("nationality_verified", False)
        e.eligibility_status = p["eligibility_status"]
    elif et == "RESULT_LIST_PUBLISHED":
        lst = ResultList(
            list_id=p["list_id"],
            division_id=p["division_id"],
            rank_basis=p["rank_basis"],
            wave=p.get("wave"),
            revision=p["revision"],
            entries={x["entry_id"]: dict(x) for x in p["entries"]},
        )
        world.lists[p["list_id"]] = lst
    elif et == "RESULT_LIST_AMENDED":
        lst = world.lists[p["list_id"]]
        if p["from_revision"] != lst.revision:
            raise ValueError(
                f"榜单 {lst.list_id} 当前修订 {lst.revision}，无法基于 {p['from_revision']} 修订"
            )
        for change in p["changes"]:
            _apply_list_change(lst, change)
        lst.revision = p["to_revision"]
    elif et == "RESULT_LIST_FROZEN":
        lst = world.lists[p["list_id"]]
        lst.frozen = True
        lst.frozen_at = p["frozen_at"]
    elif et in ("FACE_MISMATCH_FLAGGED", "BIB_DISPUTE_OPENED", "DOPING_CASE_OPENED", "APPEAL_OPENED"):
        kind = {
            "FACE_MISMATCH_FLAGGED": "face_mismatch",
            "BIB_DISPUTE_OPENED": "bib_dispute",
            "DOPING_CASE_OPENED": "doping",
            "APPEAL_OPENED": "appeal",
        }[et]
        case_id = p.get("case_id") or p.get("appeal_id")
        world.cases[case_id] = Case(
            case_id=case_id, kind=kind, entry_id=p["entry_id"], detail=p
        )
    elif et == "DOPING_RESULT_ENTERED":
        case = world.cases[p["case_id"]]
        case.detail["result"] = p["result"]
    elif et in ("APPEAL_DECIDED", "INVESTIGATION_DECIDED", "DISQUALIFICATION_ADJUDICATED"):
        case_id = p.get("case_id") or p.get("appeal_id")
        case = world.cases.get(case_id)
        if case is None:
            case = Case(case_id=case_id, kind="adjudication", entry_id=p["entry_id"])
            world.cases[case_id] = case
        case.status = "decided"
        case.outcome = p["outcome"] if "outcome" in p else p["decision"]["outcome"]
        case.decision = p.get("decision")
        case.detail.update(p)
        if et == "DISQUALIFICATION_ADJUDICATED" or case.outcome in (
            "disqualified",
            "substitute_runner_confirmed",
            "bib_transfer_confirmed",
        ):
            world.entries[p["entry_id"]].eligibility_status = "disqualified"
    elif et == "RECORD_PERFORMANCE_DECLARED":
        world.records[p["record_id"]] = Record(
            record_id=p["record_id"],
            division_id=p["division_id"],
            entry_id=p["entry_id"],
            time_ms=p["time_ms"],
        )
    elif et == "RECORD_RATIFIED":
        world.records[p["record_id"]].status = "ratified"
    elif et == "RECORD_REVOKED":
        rec = world.records[p["record_id"]]
        rec.status = "revoked"
        rec.decision = p["decision"]
    elif et == "AWARD_CALCULATED":
        calc = Calc(
            calc_id=p["calc_id"],
            entry_id=p["entry_id"],
            division_id=p["division_id"],
            basis=p["basis"],
            line_items=p["line_items"],
            gross_amount=p["gross_amount"],
            status=p.get("calc_status", "provisional"),
            supersedes=p.get("supersedes_calc_id"),
        )
        if calc.supersedes and calc.supersedes in world.calcs:
            world.calcs[calc.supersedes].status = "superseded"
        world.calcs[p["calc_id"]] = calc
        world.calcs_by_entry[p["entry_id"]].append(p["calc_id"])
    elif et == "ENTITLEMENT_SUSPENDED":
        world.holds[p["hold_id"]] = {
            "hold_id": p["hold_id"],
            "entry_id": p["entry_id"],
            "reason_code": p["reason_code"],
            "case_ref": p.get("case_ref"),
            "status": "active",
            "suspended_at": p["suspended_at"],
        }
    elif et == "ENTITLEMENT_HOLD_RESOLVED":
        hold = world.holds[p["hold_id"]]
        hold["status"] = "resolved"
        hold["resolution"] = p["resolution"]
        hold["resolved_at"] = p["resolved_at"]
        if p.get("decision_ref"):
            hold["decision_ref"] = p["decision_ref"]
    elif et == "ENTITLEMENT_FROZEN":
        world.freezes[p["freeze_id"]] = p
        world.freeze_ids_by_entry[p["entry_id"]].append(p["freeze_id"])
    elif et == "ENTITLEMENT_VOIDED":
        world.voided[p["entry_id"]] = p
        if p["entry_id"] in world.entries:
            world.entries[p["entry_id"]].eligibility_status = "disqualified"
    elif et == "TAX_DETAILS_SUBMITTED":
        world.settlements[p["entry_id"]].tax = p
    elif et == "WITHHOLDING_CALCULATED":
        world.settlements[p["entry_id"]].withholdings.append(p)
    elif et == "PAYMENT_BATCH_OPENED":
        world.batches[p["batch_id"]] = {
            "batch_id": p["batch_id"],
            "currency": p["currency"],
            "entry_ids": set(p["entry_ids"]),
            "opened_at": p["opened_at"],
        }
    elif et == "PAYMENT_RELEASED":
        world.settlements[p["entry_id"]].payments.append(p)
    elif et == "PAYMENT_REVERSED":
        world.settlements[p["entry_id"]].reversals.append(p)
    elif et == "MAKEUP_PAYMENT_RELEASED":
        world.settlements[p["entry_id"]].makeups.append(p)
    elif et == "RESULT_REVISED":
        world.revisions.append(p)


def replay(events: list[dict]) -> World:
    world = World()
    for event in events:
        apply_event(world, event)
    return world


# ---- 奖金计算引擎 -------------------------------------------------------

def _money(minor: int, currency: str) -> dict:
    return {"amount_minor": minor, "currency": currency}


def _ranked_entries(lst: ResultList) -> list[dict]:
    return sorted(lst.entries.values(), key=lambda e: e["rank"])


def compute_line_items(world: World, *, division_id: str, lists: dict[str, str],
                       schedule: dict, at: str) -> dict[str, list[dict]]:
    """按生效奖项表为该项目每位选手计算奖项明细。

    lists 按奖项种类指定榜单：{"place": 枪声榜, "special": 净计时分枪榜, "record": ...}。
    不同种类奖项依据各自榜单，未提供对应榜单的种类不参与计算。
    """
    currency = schedule.get("currency", "CNY")
    items = schedule["items"]

    result: dict[str, list[dict]] = defaultdict(list)

    for spec in items:
        if spec.get("division_id") not in (division_id, "*"):
            continue
        kind = spec["award_kind"]
        list_id = lists.get(kind)
        if not list_id:
            continue
        lst = world.lists[list_id]
        ranked = _ranked_entries(lst)

        if kind == "place":
            purses = {int(k): v for k, v in spec["rank_purses"].items()}
            consumed: set[int] = set()
            for e in ranked:
                group = e.get("tie_group")
                if group:
                    members = [m for m in ranked if m.get("tie_group") == group]
                    first = min(m["rank"] for m in members)
                    # 并列 n 人占用 first..first+n-1 共 n 个奖金名次
                    positions = list(range(first, first + len(members)))
                else:
                    members = [e]
                    positions = [e["rank"]]
                pot_positions = [r for r in positions if r in purses and r not in consumed]
                if not pot_positions:
                    continue
                pot = sum(purses[r] for r in pot_positions)
                tie_rule = spec.get("tie_rule", "split")
                if tie_rule == "split":
                    share, rem = divmod(pot, len(members))
                    amounts = {m["entry_id"]: share for m in members}
                    # 余数（最小货币单位）按号码布顺序各分 1
                    for m in sorted(members, key=lambda m: m["bib"])[:rem]:
                        amounts[m["entry_id"]] += 1
                    consumed.update(pot_positions)
                    for m in members:
                        line = {
                            "award_code": spec["award_code"],
                            "award_kind": "place",
                            "rank": m.get("tied_rank", m["rank"]),
                            "amount": _money(amounts[m["entry_id"]], currency),
                            "rule_ref": spec.get("rule_ref"),
                        }
                        if group:
                            line["tie_group"] = group
                            line["tie_share_note"] = (
                                f"并列组 {group} 均分名次 {positions[0]}-{positions[-1]} 奖金池"
                            )
                        if spec.get("stack_group"):
                            line["stack_group"] = spec["stack_group"]
                        result[m["entry_id"]].append(line)
                else:  # full：并列者各自拿本人所占最高名次全额
                    for r in pot_positions:
                        consumed.add(r)
                    own = min(r for r in positions if r in purses)
                    line = {
                        "award_code": spec["award_code"],
                        "award_kind": "place",
                        "rank": e.get("tied_rank", own),
                        "amount": _money(purses[own], currency),
                        "rule_ref": spec.get("rule_ref"),
                    }
                    if group:
                        line["tie_group"] = group
                    if spec.get("stack_group"):
                        line["stack_group"] = spec["stack_group"]
                    result[e["entry_id"]].append(line)
            continue

        if kind == "special":
            nationality = spec.get("nationality")
            max_rank = spec.get("max_rank", 8)
            sub_rank = 0
            for e in ranked:
                entry = world.entries.get(e["entry_id"])
                if entry is None or not entry.nationality_verified:
                    continue
                if nationality and entry.nationality != nationality:
                    continue
                sub_rank += 1
                if sub_rank > max_rank:
                    break
                purse = spec["rank_purses"].get(str(sub_rank)) or spec.get("amount_minor")
                if purse is None:
                    continue
                line = {
                    "award_code": spec["award_code"],
                    "award_kind": "special",
                    "rank": sub_rank,
                    "amount": _money(purse, currency),
                    "rule_ref": spec.get("rule_ref"),
                }
                if spec.get("stack_group"):
                    line["stack_group"] = spec["stack_group"]
                result[e["entry_id"]].append(line)
            continue

        if kind == "record":
            for rec in world.records.values():
                if rec.division_id != division_id or rec.status != "ratified":
                    continue
                if rec.entry_id not in lst.entries:
                    continue
                line = {
                    "award_code": spec["award_code"],
                    "award_kind": "record",
                    "amount": _money(spec["amount_minor"], currency),
                    "rule_ref": spec.get("rule_ref"),
                    "record_id": rec.record_id,
                }
                if spec.get("stack_group"):
                    line["stack_group"] = spec["stack_group"]
                result[rec.entry_id].append(line)

    # 奖项叠加规则：同一 stack_group 只保留金额最高的一项，其余不叠加
    for entry_id, lines in result.items():
        keep: list[dict] = []
        by_group: dict[str, dict] = {}
        for line in lines:
            group = line.get("stack_group")
            if not group:
                keep.append(line)
                continue
            if group not in by_group or line["amount"]["amount_minor"] > by_group[group]["amount"]["amount_minor"]:
                by_group[group] = line
        keep.extend(by_group.values())
        # 稳定顺序：place、special、record
        order = {"place": 0, "special": 1, "record": 2}
        result[entry_id] = sorted(keep, key=lambda l: (order[l["award_kind"]], l.get("rank", 0)))

    return dict(result)
