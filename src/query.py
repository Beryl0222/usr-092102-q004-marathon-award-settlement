"""查询面与审计追踪。

- athlete_view：选手看到的当前成绩状态、适用奖项、未决事项与账目；
- trace_amount：对任意已付或待付金额，复原冻结当时的榜单修订、规则版本、资格
  依据与签署裁决链，并独立重算金额核对，证明每一笔钱都可重新算出。
"""
from __future__ import annotations

from .model import World, compute_line_items, replay


def athlete_view(world: World, entry_id: str) -> dict:
    entry = world.entries.get(entry_id)
    if entry is None:
        raise KeyError(f"未知选手 {entry_id}")

    # 当前成绩状态：该选手出现在哪些榜单、当前修订下的名次
    results = []
    for lst in world.lists.values():
        row = lst.entries.get(entry_id)
        if row is None:
            continue
        results.append({
            "list_id": lst.list_id,
            "division_id": lst.division_id,
            "rank_basis": lst.rank_basis,
            "wave": lst.wave,
            "list_revision": lst.revision,
            "frozen": lst.frozen,
            "rank": row["rank"],
            "tied_rank": row.get("tied_rank"),
            "tie_group": row.get("tie_group"),
            "time_ms": row.get("time_ms"),
            "annotation": row.get("annotation"),
        })

    calc = world.latest_calc(entry_id)
    freeze_ids = world.freeze_ids_by_entry.get(entry_id, [])
    settlement = world.settlements[entry_id]

    pending_cases = [
        {
            "case_id": c.case_id,
            "kind": c.kind,
            "status": c.status,
            "detail_ref": c.detail.get("evidence_ref") or c.detail.get("sample_ref"),
        }
        for c in world.open_cases(entry_id)
    ]
    pending_holds = [
        {"hold_id": h["hold_id"], "reason_code": h["reason_code"], "case_ref": h["case_ref"]}
        for h in world.active_holds(entry_id)
    ]
    pending_records = [
        {"record_id": r.record_id, "status": r.status}
        for r in world.records.values()
        if r.entry_id == entry_id and r.status == "declared"
    ]

    reversed_ids = {r["payment_id"] for r in settlement.reversals}
    gross_paid = sum(
        p["net_amount"]["amount_minor"]
        for p in settlement.payments if p["payment_id"] not in reversed_ids
    )
    gross_reversed = sum(r["amount"]["amount_minor"] for r in settlement.reversals)
    gross_makeup = sum(
        m["net_amount"]["amount_minor"]
        for m in settlement.makeups if m["makeup_id"] not in reversed_ids
    )
    # 净付 = 未冲正付款 + 未冲正补付；冲正抵消对应原流入，不重复扣减。
    net_paid = gross_paid + gross_makeup
    return {
        "entry_id": entry_id,
        "bib": entry.bib,
        "name": entry.name,
        "division_id": entry.division_id,
        "nationality": entry.nationality,
        "nationality_verified": entry.nationality_verified,
        "eligibility_status": entry.eligibility_status,
        "results": results,
        "applicable_awards": {
            "calc_id": calc.calc_id if calc else None,
            "calc_status": calc.status if calc else None,
            "line_items": calc.line_items if calc else [],
            "gross_amount": calc.gross_amount if calc else None,
            "freeze_ids": freeze_ids,
        },
        "pending": {
            "open_cases": pending_cases,
            "active_holds": pending_holds,
            "records_awaiting_ratification": pending_records,
        },
        "ledger": {
            "payments": [
                {
                    "payment_id": p["payment_id"],
                    "batch_id": p["batch_id"],
                    "freeze_id": p["freeze_id"],
                    "net_amount": p["net_amount"],
                    "paid_at": p["paid_at"],
                    "reversed": any(
                        r["payment_id"] == p["payment_id"] for r in settlement.reversals
                    ),
                }
                for p in settlement.payments
            ],
            "reversals": [
                {"reversal_id": r["reversal_id"], "payment_id": r["payment_id"],
                 "amount": r["amount"], "reason_code": r["reason_code"]}
                for r in settlement.reversals
            ],
            "makeup_payments": [
                {"makeup_id": m["makeup_id"], "freeze_id": m["freeze_id"],
                 "net_amount": m["net_amount"], "reason_code": m["reason_code"],
                 "reversed": m["makeup_id"] in reversed_ids}
                for m in settlement.makeups
            ],
            "reversed_total_minor": gross_reversed,
            "net_paid_minor": net_paid,
        },
    }


# ---- 金额重算审计追踪 ----------------------------------------------------

def _walk_causal(world_events: list[dict], start_ids: list[str]) -> list[dict]:
    """沿 caused_by 向上收集裁决/证据链（按时间排序，去重）。"""
    by_id = {e["event_id"]: e for e in world_events}
    found: dict[str, dict] = {}

    def visit(eid: str) -> None:
        event = by_id.get(eid)
        if event is None or eid in found:
            return
        found[eid] = event
        for ref in event.get("caused_by", []):
            visit(ref)

    for eid in start_ids:
        visit(eid)
    return sorted(found.values(), key=lambda e: e["occurred_at"])


def trace_amount(events: list[dict], *, entry_id: str,
                 payment_id: str | None = None,
                 freeze_id: str | None = None) -> dict:
    """复原一笔已付（payment_id）或待付（freeze_id）金额的完整依据并重算。"""
    world = replay(events)
    settlement = world.settlements[entry_id]

    payment = None
    makeup = None
    if payment_id:
        payment = next(
            (p for p in settlement.payments if p["payment_id"] == payment_id), None
        )
        makeup = next(
            (m for m in settlement.makeups if payment_id in m.get("related_payment_ids", [])),
            None,
        )
        if payment is None and makeup is None:
            raise KeyError(f"找不到付款 {payment_id}")
        fid = (payment or makeup)["freeze_id"]
    elif freeze_id:
        fid = freeze_id
    else:
        current = world.freeze_for(entry_id)
        if current is None:
            raise KeyError(f"{entry_id} 没有冻结权益")
        fid = current["freeze_id"]

    freeze = world.freezes.get(fid)
    if freeze is None:
        raise KeyError(f"找不到冻结 {fid}")
    calc = world.calcs[freeze["calc_id"]]
    basis = freeze["basis"]

    # 截至冻结时刻重放：精确复原当时采用的榜单修订与规则生效期
    as_of = replay([e for e in events if e["occurred_at"] <= freeze["frozen_at"]])
    list_refs = basis.get("list_refs") or {
        "place": {"list_id": basis["list_id"], "list_revision": basis["list_revision"]}
    }
    adopted_lists = {}
    ranking_at_freeze = {}
    lists_for_compute: dict[str, str] = {}
    for kind, ref in list_refs.items():
        lst_then = as_of.lists[ref["list_id"]]
        adopted_lists[kind] = lst_then
        ranking_at_freeze[kind] = {
            "list_id": lst_then.list_id,
            "list_revision": lst_then.revision,
            "rank_basis": lst_then.rank_basis,
            "wave": lst_then.wave,
            "entries": [
                {"rank": e["rank"], "entry_id": e["entry_id"], "bib": e["bib"],
                 "tie_group": e.get("tie_group")}
                for e in sorted(lst_then.entries.values(), key=lambda e: e["rank"])
            ],
        }
        lists_for_compute[kind] = ref["list_id"]

    primary_kind = "place" if "place" in list_refs else next(iter(list_refs))
    edition = as_of.edition_of(calc.division_id)
    schedule = as_of.schedule_at(edition["edition_id"], freeze["frozen_at"]) if edition else None
    rulebook = as_of.rulebook_at(edition["edition_id"], freeze["frozen_at"]) if edition else None

    # 独立重算，不采信冻结时存下的金额
    recomputed = compute_line_items(
        as_of,
        division_id=calc.division_id,
        lists=lists_for_compute,
        schedule=schedule,
        at=freeze["frozen_at"],
    ).get(entry_id, [])
    recomputed_gross = sum(l["amount"]["amount_minor"] for l in recomputed)
    frozen_gross = freeze["gross_amount"]["amount_minor"]

    # 资格依据与裁决链
    eligibility_basis = [
        {
            "event_id": ref,
            "summary": (next((e for e in events if e["event_id"] == ref), None) or {}).get("summary"),
        }
        for ref in basis.get("eligibility_refs", [])
    ]
    freeze_event = next(e for e in events if e["event_type"] == "ENTITLEMENT_FROZEN"
                        and e["payload"]["freeze_id"] == fid)
    chain = _walk_causal(events, [freeze_event["event_id"]])

    return {
        "entry_id": entry_id,
        "payment_id": payment_id,
        "makeup_id": makeup["makeup_id"] if makeup else None,
        "freeze": {
            "freeze_id": fid,
            "frozen_at": freeze["frozen_at"],
            "frozen_by": freeze["frozen_by"],
            "calc_id": calc.calc_id,
            "gross_amount": freeze["gross_amount"],
        },
        "adopted_basis": {
            "list_refs": basis.get("list_refs"),
            "ranking_at_freeze": ranking_at_freeze,
            "schedule_version": basis["schedule_version"],
            "schedule_effective_from": schedule["effective_from"] if schedule else None,
            "rulebook_version": basis["rulebook_version"],
            "rulebook_effective_from": rulebook["effective_from"] if rulebook else None,
            "eligibility_refs": eligibility_basis,
        },
        "line_items_at_freeze": calc.line_items,
        "recomputed": {
            "line_items": recomputed,
            "gross_minor": recomputed_gross,
            "matches_frozen": recomputed_gross == frozen_gross,
            "difference_minor": recomputed_gross - frozen_gross,
        },
        "decision_chain": [
            {
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "summary": e["summary"],
                "provenance": e["provenance"],
                "caused_by": e.get("caused_by", []),
            }
            for e in chain
        ],
        "ledger_after_freeze": {
            "payment": payment,
            "reversed_by": [
                r for r in settlement.reversals if payment and r["payment_id"] == payment_id
            ],
            "makeup": makeup,
            "revisions": [r for r in world.revisions if r["entry_id"] == entry_id],
        },
    }
