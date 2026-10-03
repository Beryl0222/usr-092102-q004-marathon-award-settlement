"""领域服务：把业务规则守卫在写路径上。

所有方法返回构造好的事件 dict（由调用方追加进 EventStore），不直接改状态——
现态始终由 src.model 重放得到，保证"任何金额都能重新算出"。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .events import EVENT_AGGREGATE
from .model import World, compute_line_items, replay


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _e(event_type: str, aggregate_id: str, payload: dict, provenance: dict,
       summary: str, *, occurred_at: str | None = None, caused_by: list[str] | None = None,
       store=None) -> dict:
    version = store.next_version(aggregate_id) if store is not None else 1
    return {
        "event_id": f"{event_type.lower()}-{uuid.uuid4().hex[:12]}",
        "event_type": event_type,
        "aggregate_type": EVENT_AGGREGATE[event_type],
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at or now_iso(),
        "version": version,
        "summary": summary,
        "payload": payload,
        "provenance": provenance,
        "caused_by": caused_by or [],
    }


def build_event(store, *, event_type: str, aggregate_id: str, payload: dict,
                provenance: dict, summary: str, occurred_at: str | None = None,
                caused_by: list[str] | None = None) -> dict:
    """按聚合当前版本构造事件（不落库），供手工事件使用。"""
    return _e(event_type, aggregate_id, payload, provenance, summary,
              occurred_at=occurred_at, caused_by=caused_by, store=store)


class DomainRuleError(Exception):
    pass


def _event_id(store, event_type: str, key: str, value: str) -> str | None:
    if "." in key:
        head, tail = key.split(".", 1)
        def getnested(e):
            return e["payload"].get(head, {}).get(tail)
    else:
        def getnested(e):
            return e["payload"].get(key)
    return next(
        (e["event_id"] for e in store.events
         if e["event_type"] == event_type and getnested(e) == value),
        None,
    )


# ---- 证据与调查 ---------------------------------------------------------

def flag_face_mismatch(store, *, case_id, entry_id, bib, evidence_ref, flagged_at,
                       document_ref, note: str = "") -> dict:
    """人脸检录异常：只能开立替跑调查，payload 明确不自动取消资格。"""
    p = {
        "case_id": case_id,
        "entry_id": entry_id,
        "bib": bib,
        "evidence_ref": evidence_ref,
        "flagged_at": flagged_at,
        "automatic_disqualification": False,
    }
    if note:
        p["note"] = note
    return _e(
        "FACE_MISMATCH_FLAGGED", f"investigation-{case_id}", p,
        {
            "source_type": "face_recognition_system",
            "document_ref": document_ref,
            "captured_at": flagged_at,
            "signed": False,
        },
        f"人脸检录异常立案 {case_id}（仅进入替跑调查，不产生资格处罚）",
        occurred_at=flagged_at, store=store,
    )


def decide_investigation(store, *, case_id, entry_id, investigation_kind, outcome,
                         decision, decided_at) -> dict:
    """替跑/号码布调查的结论只能由签署裁决驱动。"""
    p = {
        "case_id": case_id,
        "investigation_kind": investigation_kind,
        "entry_id": entry_id,
        "outcome": outcome,
        "decision": decision,
        "decided_at": decided_at,
    }
    return _e(
        "INVESTIGATION_DECIDED", f"investigation-{case_id}", p,
        {
            "source_type": "jury_decision",
            "document_ref": decision["document_ref"],
            "issued_by": decision["deciding_body"],
            "captured_at": decided_at,
            "signed": True,
        },
        f"调查 {case_id} 裁决：{outcome}",
        occurred_at=decided_at, store=store,
    )


def adjudicate_disqualification(store, *, case_id, entry_id, decision, decided_at,
                                trigger_event_id: str) -> dict:
    """取消资格：签署裁决事件。榜单撤出/权益作废由后续事件接续，原付款事实保留。"""
    return _e(
        "DISQUALIFICATION_ADJUDICATED", f"investigation-{case_id}",
        {"case_id": case_id, "entry_id": entry_id, "decision": decision, "decided_at": decided_at},
        {
            "source_type": "jury_decision",
            "document_ref": decision["document_ref"],
            "issued_by": decision["deciding_body"],
            "captured_at": decided_at,
            "signed": True,
        },
        f"裁决取消 {entry_id} 比赛资格：{decision.get('reason', '')}",
        occurred_at=decided_at,
        caused_by=[trigger_event_id] if trigger_event_id else [],
        store=store,
    )


# ---- 榜单修订 -----------------------------------------------------------

def amend_list_for_disqualification(store, world: World, *, list_id: str,
                                    withdrawn_entry_id: str, reason: str,
                                    decision: dict, at: str) -> dict:
    """因签署裁决从榜单撤出选手并触发后续名次递补（名次由 rank 重排生成）。

    榜单即使已冻结也允许出修订版：历史冻结权益按 list_revision 钉在旧修订上，
    新修订只影响之后的核算，原付款事实不动。
    """
    lst = world.lists[list_id]
    from_rev = lst.revision
    changes = [{
        "op": "withdraw",
        "entry_id": withdrawn_entry_id,
        "reason": reason,
        "source_ref": decision["document_ref"],
        "decision_id": decision["decision_id"],
    }]
    remaining = sorted(
        (e for e in lst.entries.values() if e["entry_id"] != withdrawn_entry_id),
        key=lambda e: e["rank"],
    )
    # 重新编排顺序名次，同时保留并列组：组成员连续占位，tied_rank 取组内首名。
    group_seen: dict[str, int] = {}
    for new_rank, e in enumerate(remaining, start=1):
        group = e.get("tie_group")
        if group:
            group_seen.setdefault(group, new_rank)
    for new_rank, e in enumerate(remaining, start=1):
        if e["rank"] != new_rank or (e.get("tie_group") and e.get("tied_rank") != group_seen[e["tie_group"]]):
            before = dict(e)
            after = dict(e)
            after["rank"] = new_rank
            if e.get("tie_group"):
                after["tied_rank"] = group_seen[e["tie_group"]]
            changes.append({
                "op": "correct",
                "entry_id": e["entry_id"],
                "before": before,
                "after": after,
                "reason": f"递补：{withdrawn_entry_id} 被撤出后名次顺延",
                "source_ref": decision["document_ref"],
            })
    payload = {
        "list_id": list_id,
        "from_revision": from_rev,
        "to_revision": from_rev + 1,
        "changes": changes,
        "amended_at": at,
    }
    return _e(
        "RESULT_LIST_AMENDED", list_id, payload,
        {
            "source_type": "jury_decision",
            "document_ref": decision["document_ref"],
            "issued_by": decision["deciding_body"],
            "captured_at": at,
            "signed": True,
        },
        f"榜单 {list_id} 修订至 r{from_rev + 1}：撤出 {withdrawn_entry_id} 并递补",
        occurred_at=at, store=store,
    )


# ---- 奖金核算与冻结 ------------------------------------------------------

class AwardService:
    def __init__(self, store):
        self.store = store

    def _world(self) -> World:
        return replay(self.store.events)

    def calculate(self, *, division_id: str, lists: dict[str, str], edition_id: str,
                  calc_at: str, eligibility_refs: dict[str, list[str]] | None = None,
                  status: str = "provisional", supersedes_of: dict[str, str] | None = None,
                  only_entries: list[str] | None = None) -> list[dict]:
        """依据各奖项种类对应榜单的当前修订与生效奖项表重算。

        lists 形如 {"place": "list-gun-M", "special": "list-net-waveA-M"}。
        """
        world = self._world()
        schedule = world.schedule_at(edition_id, calc_at)
        if schedule is None:
            raise DomainRuleError(f"{calc_at} 没有已生效的奖项表")
        # 奖项依据守卫：规则要求的计时基准（枪声/净计时）必须与采用榜单一致。
        basis_rules = schedule.get("basis_rules", {})
        kinds_in_schedule = {
            spec["award_kind"] for spec in schedule["items"]
            if spec.get("division_id") in (division_id, "*")
        }
        for kind in kinds_in_schedule:
            list_id = lists.get(kind)
            if list_id is None:
                raise DomainRuleError(f"{kind} 奖项缺少对应榜单，不得核算")
            required_basis = basis_rules.get(kind)
            actual_basis = world.lists[list_id].rank_basis
            if required_basis and required_basis != actual_basis:
                raise DomainRuleError(
                    f"{kind} 奖项要求 {required_basis} 榜单，{list_id} 为 {actual_basis}，不得作为核算依据"
                )
        line_map = compute_line_items(
            world, division_id=division_id, lists=lists,
            schedule=schedule, at=calc_at,
        )
        basis = {
            "list_refs": {
                kind: {
                    "list_id": lid,
                    "list_revision": world.lists[lid].revision,
                    "rank_basis": world.lists[lid].rank_basis,
                    "wave": world.lists[lid].wave,
                }
                for kind, lid in lists.items()
            },
            "list_id": lists.get("place") or next(iter(lists.values())),
            "list_revision": world.lists[lists.get("place") or next(iter(lists.values()))].revision,
            "schedule_version": schedule["schedule_version"],
            "rulebook_version": schedule["rulebook_version"],
            "eligibility_refs": [],
        }
        primary_list_id = basis["list_id"]
        primary_revision = basis["list_revision"]
        refs_desc = ",".join(
            f"{kind}:{lid}#r{world.lists[lid].revision}" for kind, lid in lists.items()
        )
        currency = schedule.get("currency", "CNY")
        events_out = []
        for entry_id, lines in line_map.items():
            if only_entries and entry_id not in only_entries:
                continue
            gross = sum(l["amount"]["amount_minor"] for l in lines)
            calc_id = f"calc-{entry_id}-r{primary_revision}-{uuid.uuid4().hex[:6]}"
            basis_entry = dict(basis)
            basis_entry["eligibility_refs"] = (eligibility_refs or {}).get(entry_id, [])
            payload = {
                "calc_id": calc_id,
                "entry_id": entry_id,
                "division_id": division_id,
                "basis": basis_entry,
                "line_items": lines,
                "gross_amount": {"amount_minor": gross, "currency": currency},
                "calc_status": status,
            }
            prior = (supersedes_of or {}).get(entry_id)
            prior_event_id = None
            if prior:
                payload["supersedes_calc_id"] = prior
                prior_event_id = _event_id(self.store, "AWARD_CALCULATED", "calc_id", prior)
            events_out.append(_e(
                "AWARD_CALCULATED", f"entitlement-{entry_id}", payload,
                {
                    "source_type": "race_organization",
                    "document_ref": refs_desc,
                    "captured_at": calc_at,
                    "signed": False,
                },
                f"{entry_id} 按榜单[{refs_desc}] 核算奖金 {gross} 最小单位",
                occurred_at=calc_at,
                caused_by=[prior_event_id] if prior_event_id else [],
                store=self.store,
            ))
        return events_out

    def suspend(self, *, hold_id, entry_id, reason_code, case_ref, suspended_at) -> dict:
        return _e(
            "ENTITLEMENT_SUSPENDED", f"entitlement-{entry_id}",
            {
                "hold_id": hold_id,
                "entry_id": entry_id,
                "reason_code": reason_code,
                "case_ref": case_ref,
                "suspended_at": suspended_at,
            },
            {
                "source_type": "race_organization",
                "document_ref": case_ref,
                "captured_at": suspended_at,
                "signed": False,
            },
            f"暂缓 {entry_id} 权益：{reason_code}",
            occurred_at=suspended_at, store=self.store,
        )

    def resolve_hold(self, *, hold_id, entry_id, resolution, resolved_at,
                     decision_ref: str | None = None) -> dict:
        payload = {
            "hold_id": hold_id,
            "entry_id": entry_id,
            "resolution": resolution,
            "resolved_at": resolved_at,
        }
        if decision_ref:
            payload["decision_ref"] = decision_ref
        provenance_type = "jury_decision" if decision_ref else "race_organization"
        return _e(
            "ENTITLEMENT_HOLD_RESOLVED", f"entitlement-{entry_id}", payload,
            {
                "source_type": provenance_type,
                "document_ref": decision_ref or f"hold-{hold_id}",
                "captured_at": resolved_at,
                "signed": bool(decision_ref),
            },
            f"暂缓 {hold_id} 解除：{resolution}",
            occurred_at=resolved_at, store=self.store,
        )

    def freeze(self, *, entry_id: str, freeze_id: str, frozen_at: str,
               frozen_by: str, calc_id: str | None = None) -> dict:
        """冻结获奖权益。前置：核算已确认、无生效暂缓、未被作废、同一核算未重复冻结。"""
        world = self._world()
        calc = world.calcs[calc_id] if calc_id else world.latest_calc(entry_id)
        if calc is None:
            raise DomainRuleError(f"{entry_id} 没有可冻结的核算结果")
        if calc.status != "confirmed":
            raise DomainRuleError(f"{entry_id} 核算 {calc.calc_id} 尚未确认，不能冻结")
        if world.freeze_of_calc(calc.calc_id) is not None:
            raise DomainRuleError(f"核算 {calc.calc_id} 已冻结，冻结不可变")
        if world.active_holds(entry_id):
            raise DomainRuleError(f"{entry_id} 存在未决暂缓，不能冻结")
        entry = world.entries[entry_id]
        if entry.eligibility_status != "eligible":
            raise DomainRuleError(
                f"{entry_id} 资格状态为 {entry.eligibility_status}，仅 reviewed-eligible 可冻结"
            )
        payload = {
            "freeze_id": freeze_id,
            "entry_id": entry_id,
            "calc_id": calc.calc_id,
            "basis": calc.basis,
            "gross_amount": calc.gross_amount,
            "frozen_at": frozen_at,
            "frozen_by": frozen_by,
        }
        calc_event_id = next(
            (e["event_id"] for e in self.store.events
             if e["event_type"] == "AWARD_CALCULATED" and e["payload"].get("calc_id") == calc.calc_id),
            None,
        )
        return _e(
            "ENTITLEMENT_FROZEN", f"entitlement-{entry_id}", payload,
            {
                "source_type": "race_organization",
                "document_ref": f"freeze-{freeze_id}",
                "issued_by": frozen_by,
                "captured_at": frozen_at,
                "signed": True,
            },
            f"冻结 {entry_id} 权益 {freeze_id}，gross={calc.gross_amount['amount_minor']}",
            occurred_at=frozen_at,
            caused_by=[calc_event_id] if calc_event_id else [],
            store=self.store,
        )

    def void_entitlement(self, *, entry_id, calc_id, decision, voided_at) -> dict:
        return _e(
            "ENTITLEMENT_VOIDED", f"entitlement-{entry_id}",
            {
                "entry_id": entry_id,
                "calc_id": calc_id,
                "decision_ref": decision["document_ref"],
                "voided_at": voided_at,
            },
            {
                "source_type": "jury_decision",
                "document_ref": decision["document_ref"],
                "issued_by": decision["deciding_body"],
                "captured_at": voided_at,
                "signed": True,
            },
            f"作废 {entry_id} 权益（依据裁决 {decision['decision_id']}，付款事实保留）",
            occurred_at=voided_at, store=self.store,
        )

    def revise_after_change(self, *, entry_id: str, trigger_event_id: str,
                            revised_at: str, ledger_effect: str,
                            after_calc_id: str | None) -> dict:
        """改判更正记录：只增不删，记录改判前后快照与账目影响。"""
        world = self._world()
        calc = world.latest_calc(entry_id)
        freeze = world.freeze_for(entry_id)
        before_snapshot = {
            "calc_id": calc.calc_id if calc else None,
            "gross_amount": calc.gross_amount if calc else None,
            "freeze_id": freeze["freeze_id"] if freeze else None,
        }
        after_calc = world.calcs.get(after_calc_id) if after_calc_id else None
        after_snapshot = {
            "calc_id": after_calc_id,
            "gross_amount": after_calc.gross_amount if after_calc else None,
        }
        revision_id = f"rev-{entry_id}-{uuid.uuid4().hex[:8]}"
        return _e(
            "RESULT_REVISED", f"entitlement-{entry_id}",
            {
                "revision_id": revision_id,
                "entry_id": entry_id,
                "trigger_event_id": trigger_event_id,
                "before_snapshot": before_snapshot,
                "after_snapshot": after_snapshot,
                "ledger_effect": ledger_effect,
                "revised_at": revised_at,
            },
            {
                "source_type": "finance_operation",
                "document_ref": revision_id,
                "captured_at": revised_at,
                "signed": True,
            },
            f"改判更正 {revision_id}：{entry_id} 账目影响 {ledger_effect}",
            occurred_at=revised_at, caused_by=[trigger_event_id], store=self.store,
        )


# ---- 税费与付款 ----------------------------------------------------------

class PaymentService:
    def __init__(self, store):
        self.store = store
        self.awards = AwardService(store)

    def _world(self) -> World:
        return replay(self.store.events)

    def submit_tax(self, *, entry_id, tax_residency, id_document_ref, submitted_at) -> dict:
        return _e(
            "TAX_DETAILS_SUBMITTED", f"settlement-{entry_id}",
            {
                "entry_id": entry_id,
                "tax_residency": tax_residency,
                "id_document_ref": id_document_ref,
                "submitted_at": submitted_at,
            },
            {
                "source_type": "athlete_submission",
                "document_ref": id_document_ref,
                "captured_at": submitted_at,
                "signed": False,
            },
            f"{entry_id} 提交税务资料",
            occurred_at=submitted_at, store=self.store,
        )

    def calculate_withholding(self, *, entry_id, rate_bps, calculated_at,
                              freeze_id: str | None = None) -> dict:
        world = self._world()
        freeze = world.freezes[freeze_id] if freeze_id else world.freeze_for(entry_id)
        if freeze is None:
            raise DomainRuleError(f"{entry_id} 权益未冻结，不能计税")
        if world.withholding_for(entry_id, freeze["freeze_id"]) is not None:
            raise DomainRuleError(f"冻结 {freeze['freeze_id']} 已计税，不得重复计算")
        settlement = world.settlements[entry_id]
        if settlement.tax is None:
            raise DomainRuleError(f"{entry_id} 缺少税务资料，不能计税")
        gross = freeze["gross_amount"]
        wh = gross["amount_minor"] * rate_bps // 10000
        net = gross["amount_minor"] - wh
        fid = freeze["freeze_id"]
        freeze_event_id = next(
            (e["event_id"] for e in self.store.events
             if e["event_type"] == "ENTITLEMENT_FROZEN" and e["payload"].get("freeze_id") == fid),
            None,
        )
        return _e(
            "WITHHOLDING_CALCULATED", f"settlement-{entry_id}",
            {
                "entry_id": entry_id,
                "freeze_id": fid,
                "gross_amount": gross,
                "rate_bps": rate_bps,
                "withholding_amount": {"amount_minor": wh, "currency": gross["currency"]},
                "net_amount": {"amount_minor": net, "currency": gross["currency"]},
                "calculated_at": calculated_at,
            },
            {
                "source_type": "finance_operation",
                "document_ref": f"wh-{fid}",
                "captured_at": calculated_at,
                "signed": False,
            },
            f"{entry_id} 代扣税额 {wh}（费率 {rate_bps}bps）",
            occurred_at=calculated_at,
            caused_by=[freeze_event_id] if freeze_event_id else [],
            store=self.store,
        )

    def open_batch(self, *, batch_id, currency, entry_ids, opened_at) -> dict:
        return _e(
            "PAYMENT_BATCH_OPENED", f"batch-{batch_id}",
            {
                "batch_id": batch_id,
                "currency": currency,
                "entry_ids": entry_ids,
                "opened_at": opened_at,
            },
            {
                "source_type": "finance_operation",
                "document_ref": f"batch-{batch_id}",
                "captured_at": opened_at,
                "signed": False,
            },
            f"开启付款批次 {batch_id}（{len(entry_ids)} 人）",
            occurred_at=opened_at, store=self.store,
        )

    def release(self, *, payment_id, batch_id, entry_id, paid_at,
                freeze_id: str | None = None) -> dict:
        """放款门槛：批次包含本人 + 权益已冻结 + 无生效暂缓 + 已计税。"""
        world = self._world()
        batch = world.batches.get(batch_id)
        if batch is None or entry_id not in batch["entry_ids"]:
            raise DomainRuleError(f"{entry_id} 不在批次 {batch_id} 内")
        freeze = world.freezes[freeze_id] if freeze_id else world.freeze_for(entry_id)
        if freeze is None:
            raise DomainRuleError(f"{entry_id} 权益未冻结，禁止先付款后等结果")
        if world.active_holds(entry_id):
            raise DomainRuleError(f"{entry_id} 有未决暂缓（申诉/兴奋剂/调查），禁止放款")
        if entry_id in world.voided:
            raise DomainRuleError(f"{entry_id} 权益已作废，禁止放款")
        wh = world.withholding_for(entry_id, freeze["freeze_id"])
        if wh is None:
            raise DomainRuleError(f"{entry_id} 冻结 {freeze['freeze_id']} 尚未完成代扣计税，禁止放款")
        # 同一冻结权益只能支付一次（重复付款防线）
        for existing in world.settlements[entry_id].payments:
            if existing["freeze_id"] == freeze["freeze_id"] and not _reversed(world, entry_id, existing["payment_id"]):
                raise DomainRuleError(
                    f"冻结 {freeze['freeze_id']} 已存在有效付款 {existing['payment_id']}，不得重复支付"
                )
        payload = {
            "payment_id": payment_id,
            "batch_id": batch_id,
            "entry_id": entry_id,
            "freeze_id": freeze["freeze_id"],
            "gross_amount": wh["gross_amount"],
            "withholding_amount": wh["withholding_amount"],
            "net_amount": wh["net_amount"],
            "paid_at": paid_at,
        }
        fid = freeze["freeze_id"]
        refs = [
            _event_id(self.store, "ENTITLEMENT_FROZEN", "freeze_id", fid),
            _event_id(self.store, "WITHHOLDING_CALCULATED", "freeze_id", fid),
            _event_id(self.store, "PAYMENT_BATCH_OPENED", "batch_id", batch_id),
        ]
        return _e(
            "PAYMENT_RELEASED", f"settlement-{entry_id}", payload,
            {
                "source_type": "finance_operation",
                "document_ref": payment_id,
                "captured_at": paid_at,
                "signed": True,
            },
            f"付款 {payment_id}：{entry_id} 实付 {wh['net_amount']['amount_minor']}",
            occurred_at=paid_at, caused_by=[r for r in refs if r], store=self.store,
        )

    def reverse(self, *, reversal_id, payment_id, entry_id, reason_code, reversed_at,
                decision_ref: str | None = None, against: str = "payment") -> dict:
        """冲正：原付款/补付事实保留，追加反向记录保持账目连续。

        against="payment" 冲正 PAYMENT_RELEASED；against="makeup" 冲正 MAKEUP_PAYMENT_RELEASED。
        """
        world = self._world()
        if against == "makeup":
            target = next(
                (m for m in world.settlements[entry_id].makeups if m["makeup_id"] == payment_id),
                None,
            )
            target_kind = "MAKEUP_PAYMENT_RELEASED"
        else:
            target = next(
                (p for p in world.settlements[entry_id].payments if p["payment_id"] == payment_id),
                None,
            )
            target_kind = "PAYMENT_RELEASED"
        if target is None:
            raise DomainRuleError(f"找不到{('补付' if against == 'makeup' else '付款')} {payment_id}")
        if _reversed(world, entry_id, payment_id):
            raise DomainRuleError(f"{payment_id} 已冲正，不得重复冲正")
        if reason_code == "disqualification" and not decision_ref:
            raise DomainRuleError("取消资格导致的冲正必须引用签署裁决")
        payload = {
            "reversal_id": reversal_id,
            "payment_id": payment_id,
            "reversed_event_type": target_kind,
            "entry_id": entry_id,
            "amount": target["net_amount"],
            "reason_code": reason_code,
            "reversed_at": reversed_at,
        }
        if decision_ref:
            payload["decision_ref"] = decision_ref
        refs = [_event_id(self.store, "PAYMENT_RELEASED", "payment_id", payment_id)]
        if decision_ref:
            refs.append(
                _event_id(self.store, "DISQUALIFICATION_ADJUDICATED", "decision.document_ref", decision_ref)
                or _event_id(self.store, "INVESTIGATION_DECIDED", "decision.document_ref", decision_ref)
            )
        return _e(
            "PAYMENT_REVERSED", f"settlement-{entry_id}", payload,
            {
                "source_type": "jury_decision" if decision_ref else "finance_operation",
                "document_ref": decision_ref or reversal_id,
                "captured_at": reversed_at,
                "signed": bool(decision_ref),
            },
            f"冲正 {reversal_id}：撤回 {payment_id}（{reason_code}），原付款事实保留",
            occurred_at=reversed_at, caused_by=[r for r in refs if r], store=self.store,
        )

    def makeup(self, *, makeup_id, entry_id, reason_code, related_payment_ids, paid_at,
               freeze_id: str | None = None,
               delta_against_freeze_id: str | None = None) -> dict:
        """补付：依据冻结权益发放（递补产生的新权益走新 freeze，不改旧账）。

        delta_against_freeze_id 存在时只补发新旧冻结之间的差额（名次递补常用）；
        否则按该冻结全额补发（如冲正后的纠正重付）。
        """
        world = self._world()
        freeze = world.freezes[freeze_id] if freeze_id else world.freeze_for(entry_id)
        if freeze is None:
            raise DomainRuleError(f"{entry_id} 没有可补付的冻结权益")
        if world.active_holds(entry_id):
            raise DomainRuleError(f"{entry_id} 有未决暂缓，禁止补付")
        wh = world.withholding_for(entry_id, freeze["freeze_id"])
        if wh is None:
            raise DomainRuleError(f"{entry_id} 冻结 {freeze['freeze_id']} 尚未完成代扣计税，禁止补付")
        # 新冻结若已支付则拒绝；冲正后的补付允许（同一 freeze 已无有效付款）
        for existing in world.settlements[entry_id].payments:
            if existing["freeze_id"] == freeze["freeze_id"] and not _reversed(world, entry_id, existing["payment_id"]):
                raise DomainRuleError(f"冻结 {freeze['freeze_id']} 已支付，不得重复补付")
        gross_amount = wh["gross_amount"]
        withholding_amount = wh["withholding_amount"]
        net_amount = wh["net_amount"]
        if delta_against_freeze_id:
            old_freeze = world.freezes.get(delta_against_freeze_id)
            old_wh = world.withholding_for(entry_id, delta_against_freeze_id)
            if old_freeze is None or old_wh is None:
                raise DomainRuleError("差额补付需要旧冻结及其计税记录")
            gross_delta = gross_amount["amount_minor"] - old_freeze["gross_amount"]["amount_minor"]
            wh_delta = withholding_amount["amount_minor"] - old_wh["withholding_amount"]["amount_minor"]
            if gross_delta <= 0:
                raise DomainRuleError(
                    f"新冻结金额不高于旧冻结（差额 {gross_delta}），不应发起递补补付"
                )
            currency = gross_amount["currency"]
            gross_amount = {"amount_minor": gross_delta, "currency": currency}
            withholding_amount = {"amount_minor": wh_delta, "currency": currency}
            net_amount = {"amount_minor": gross_delta - wh_delta, "currency": currency}
        payload = {
            "makeup_id": makeup_id,
            "entry_id": entry_id,
            "freeze_id": freeze["freeze_id"],
            "gross_amount": gross_amount,
            "withholding_amount": withholding_amount,
            "net_amount": net_amount,
            "reason_code": reason_code,
            "related_payment_ids": related_payment_ids,
            "paid_at": paid_at,
        }
        if delta_against_freeze_id:
            payload["delta_of_freeze_id"] = delta_against_freeze_id
        return _e(
            "MAKEUP_PAYMENT_RELEASED", f"settlement-{entry_id}", payload,
            {
                "source_type": "finance_operation",
                "document_ref": makeup_id,
                "captured_at": paid_at,
                "signed": True,
            },
            f"补付 {makeup_id}：{entry_id} 实付 {wh['net_amount']['amount_minor']}（{reason_code}）",
            occurred_at=paid_at, store=self.store,
        )


def _reversed(world: World, entry_id: str, payment_id: str) -> bool:
    return any(
        r["payment_id"] == payment_id
        for r in world.settlements[entry_id].reversals
    )
