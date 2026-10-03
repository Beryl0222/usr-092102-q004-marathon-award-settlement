import unittest

from src.model import replay
from src.query import athlete_view, trace_amount
from src.services import (
    AwardService, PaymentService, DomainRuleError,
    adjudicate_disqualification, amend_list_for_disqualification,
    build_event, decide_investigation, flag_face_mismatch,
)
from tests.fixtures import DIV, EDITION, GUN, NET, prov, build_world

LISTS = {"place": GUN, "special": NET}


def settle_to_payment(store, awards, payments, elig, entry_id, *,
                      freeze="FRZ", rate=2000, at="2026-09-30T10:00:00+08:00",
                      calc_id=None, batch="B1", pay="P1"):
    evs = awards.calculate(
        division_id=DIV, lists=LISTS, edition_id=EDITION, calc_at="2026-09-29T12:00:00+08:00",
        status="confirmed", only_entries=[entry_id],
        eligibility_refs={entry_id: [elig[entry_id]]})
    for ev in evs:
        store.append(ev)
    calc_id = evs[0]["payload"]["calc_id"]
    store.append(awards.freeze(entry_id=entry_id, freeze_id=freeze,
                               frozen_at="2026-09-29T15:00:00+08:00", frozen_by="核算员",
                               calc_id=calc_id))
    store.append(payments.submit_tax(entry_id=entry_id, tax_residency="CN",
                                     id_document_ref="tax/1",
                                     submitted_at="2026-09-24T16:00:00+08:00"))
    store.append(payments.calculate_withholding(entry_id=entry_id, rate_bps=rate,
                                                calculated_at="2026-09-29T16:00:00+08:00",
                                                freeze_id=freeze))
    store.append(payments.open_batch(batch_id=batch, currency="CNY", entry_ids=[entry_id],
                                     opened_at="2026-09-30T09:00:00+08:00"))
    store.append(payments.release(payment_id=pay, batch_id=batch, entry_id=entry_id,
                                  paid_at=at, freeze_id=freeze))
    return calc_id


class PaymentGateTest(unittest.TestCase):
    def test_cannot_pay_without_freeze(self):
        store, awards, payments, elig = build_world()
        # 直接开批次放款（未冻结、未计税）必须被拒
        store.append(payments.open_batch(batch_id="B1", currency="CNY", entry_ids=["E1"],
                                         opened_at="2026-09-30T09:00:00+08:00"))
        with self.assertRaises(DomainRuleError):
            payments.release(payment_id="P1", batch_id="B1", entry_id="E1",
                             paid_at="2026-09-30T10:00:00+08:00")

    def test_active_hold_blocks_freeze_and_payment(self):
        store, awards, payments, elig = build_world()
        store.append(awards.suspend(hold_id="H1", entry_id="E1", reason_code="doping",
                                    case_ref="C1", suspended_at="2026-09-26T11:00:00+08:00"))
        evs = awards.calculate(
            division_id=DIV, lists=LISTS, edition_id=EDITION, calc_at="2026-09-29T12:00:00+08:00",
            status="confirmed", only_entries=["E1"], eligibility_refs={"E1": [elig["E1"]]})
        for ev in evs:
            store.append(ev)
        with self.assertRaises(DomainRuleError):
            store.append(awards.freeze(entry_id="E1", freeze_id="FRZ-E1",
                                       frozen_at="2026-09-29T15:00:00+08:00", frozen_by="x",
                                       calc_id=evs[0]["payload"]["calc_id"]))

    def test_double_payment_same_freeze_blocked(self):
        store, awards, payments, elig = build_world()
        settle_to_payment(store, awards, payments, elig, "E1")
        with self.assertRaises(DomainRuleError):
            # 同批次同冻结再放一笔
            store.append(payments.release(payment_id="P2", batch_id="B1", entry_id="E1",
                                          paid_at="2026-10-01T10:00:00+08:00",
                                          freeze_id="FRZ"))


class FaceMismatchTest(unittest.TestCase):
    def test_face_flag_only_opens_investigation_and_hold(self):
        store, awards, payments, elig = build_world()
        store.append(flag_face_mismatch(
            store, case_id="FC1", entry_id="E1", bib="001", evidence_ref="F/1",
            document_ref="F/doc", flagged_at="2026-09-21T09:05:00+08:00"))
        store.append(awards.suspend(hold_id="H-FACE", entry_id="E1",
                                    reason_code="substitute_runner_investigation",
                                    case_ref="FC1", suspended_at="2026-09-22T09:00:00+08:00"))
        world = replay(store.events)
        # 人脸异常绝不直接改资格
        self.assertEqual(world.entries["E1"].eligibility_status, "eligible")
        self.assertEqual(world.cases["FC1"].status, "open")
        self.assertTrue(world.active_holds("E1"))

        decision = {"decision_id": "D1", "deciding_body": "仲裁委", "decided_at": "2026-09-29T10:00:00+08:00",
                    "signed_by": "仲裁长", "document_ref": "J/D1", "outcome": "no_violation",
                    "reason": "同一人"}
        store.append(decide_investigation(
            store, case_id="FC1", entry_id="E1", investigation_kind="substitute_runner",
            outcome="no_violation", decision=decision, decided_at="2026-09-29T10:00:00+08:00"))
        store.append(awards.resolve_hold(hold_id="H-FACE", entry_id="E1", resolution="lifted",
                                         resolved_at="2026-09-29T10:30:00+08:00",
                                         decision_ref="J/D1"))
        world = replay(store.events)
        self.assertFalse(world.active_holds("E1"))


class RevisionAndClawbackTest(unittest.TestCase):
    def test_disqualification_promotes_and_keeps_original_payment(self):
        store, awards, payments, elig = build_world()
        # E1（第1）先付款
        settle_to_payment(store, awards, payments, elig, "E1", freeze="FRZ-E1-R1",
                          batch="B1", pay="P-E1")
        calc_e1 = replay(store.events).latest_calc("E1").calc_id

        # 签署裁决取消 E1
        decision = {"decision_id": "DQ1", "deciding_body": "仲裁委", "decided_at": "2026-10-08T10:00:00+08:00",
                    "signed_by": "仲裁长", "document_ref": "J/DQ1", "outcome": "disqualified",
                    "reason": "违规"}
        dq = store.append(adjudicate_disqualification(
            store, case_id="C1", entry_id="E1", decision=decision,
            decided_at="2026-10-08T10:00:00+08:00", trigger_event_id=""))
        # dq 的 caused_by 为空可接受；榜单修订
        store.append(amend_list_for_disqualification(
            store, replay(store.events), list_id=GUN, withdrawn_entry_id="E1",
            reason="DQ", decision=decision, at="2026-10-08T11:00:00+08:00"))
        store.append(amend_list_for_disqualification(
            store, replay(store.events), list_id=NET, withdrawn_entry_id="E1",
            reason="DQ", decision=decision, at="2026-10-08T11:05:00+08:00"))
        store.append(awards.void_entitlement(entry_id="E1", calc_id=calc_e1,
                                             decision=decision, voided_at="2026-10-08T12:00:00+08:00"))

        world = replay(store.events)
        # E1 已撤出，E2 在枪声榜递补到第 1
        self.assertNotIn("E1", world.lists[GUN].entries)
        self.assertEqual(world.lists[GUN].entries["E2"]["rank"], 1)
        self.assertEqual(world.lists[GUN].revision, 2)

        # 冲正 E1 原付款（事实保留）
        store.append(payments.reverse(reversal_id="R1", payment_id="P-E1", entry_id="E1",
                                      reason_code="disqualification",
                                      reversed_at="2026-10-08T13:00:00+08:00",
                                      decision_ref="J/DQ1"))
        view = athlete_view(replay(store.events), "E1")
        self.assertEqual(view["ledger"]["net_paid_minor"], 0)
        self.assertTrue(view["ledger"]["payments"][0]["reversed"])

        # E2 按 r2 形成新权益并补付
        old = replay(store.events).latest_calc
        evs = awards.calculate(
            division_id=DIV, lists=LISTS, edition_id=EDITION, calc_at="2026-10-08T14:00:00+08:00",
            status="confirmed", only_entries=["E2"],
            supersedes_of={}, eligibility_refs={"E2": [elig["E2"]]})
        for ev in evs:
            store.append(ev)
        new_calc = evs[0]["payload"]["calc_id"]
        store.append(awards.freeze(entry_id="E2", freeze_id="FRZ-E2-R2",
                                   frozen_at="2026-10-08T15:00:00+08:00", frozen_by="x",
                                   calc_id=new_calc))
        store.append(payments.submit_tax(entry_id="E2", tax_residency="CN",
                                         id_document_ref="tax/2",
                                         submitted_at="2026-09-24T16:00:00+08:00"))
        store.append(payments.calculate_withholding(entry_id="E2", rate_bps=2000,
                                                    calculated_at="2026-10-08T15:30:00+08:00",
                                                    freeze_id="FRZ-E2-R2"))
        store.append(payments.makeup(makeup_id="MK1", entry_id="E2", reason_code="promotion",
                                     related_payment_ids=[], paid_at="2026-10-09T10:00:00+08:00",
                                     freeze_id="FRZ-E2-R2"))
        # E2 递补后第1名 80000，税后 64000
        view2 = athlete_view(replay(store.events), "E2")
        self.assertEqual(view2["ledger"]["net_paid_minor"], 64000)

    def test_recompute_trace_reproduces_paid_amount(self):
        store, awards, payments, elig = build_world()
        settle_to_payment(store, awards, payments, elig, "E1", freeze="FRZ-E1-R1",
                          batch="B1", pay="P-E1")
        trace = trace_amount(store.events, entry_id="E1", payment_id="P-E1")
        self.assertTrue(trace["recomputed"]["matches_frozen"])
        self.assertEqual(trace["adopted_basis"]["list_refs"]["place"]["list_revision"], 1)
        self.assertEqual(trace["adopted_basis"]["rulebook_version"], "RB1")


if __name__ == "__main__":
    unittest.main()
