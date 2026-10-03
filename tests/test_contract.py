import json
import unittest
from pathlib import Path

from src.events import EVENT_AGGREGATE, EVENT_TYPES, payload_required
from src.validator import validate_event

ROOT = Path(__file__).parents[1]


def base_event(**overrides) -> dict:
    event = {
        "event_id": "e-1",
        "event_type": "TIMING_RECEIVED",
        "aggregate_type": "timing_evidence",
        "aggregate_id": "timing-E1",
        "occurred_at": "2026-09-20T12:00:00+08:00",
        "version": 1,
        "summary": "计时证据",
        "payload": {
            "entry_id": "E1", "bib": "0001", "timing_source": "chip_and_gun",
            "evidence_ref": "T/1",
        },
        "provenance": {
            "source_type": "official_timing",
            "document_ref": "T/1",
            "captured_at": "2026-09-20T12:00:00+08:00",
        },
    }
    event.update(overrides)
    return event


class ContractTest(unittest.TestCase):
    def test_sample_matches_envelope(self) -> None:
        sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_event(sample), [])

    def test_scenario_jsonl_all_valid(self) -> None:
        path = ROOT / "data" / "taiyuan_marathon_events.jsonl"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                errors = validate_event(json.loads(line))
                self.assertEqual(errors, [], f"第 {i} 行契约错误：{errors}")

    def test_missing_envelope_fields(self) -> None:
        errors = validate_event({"event_id": "x"})
        self.assertTrue(any("缺少字段" in e for e in errors))

    def test_unknown_event_type_rejected(self) -> None:
        errors = validate_event(base_event(event_type="NO_SUCH_EVENT"))
        self.assertTrue(any("枚举" in e for e in errors))

    def test_event_aggregate_must_match_catalog(self) -> None:
        errors = validate_event(base_event(aggregate_type="race_entry"))
        self.assertTrue(any("必须归属聚合" in e for e in errors))

    def test_version_must_be_positive_integer(self) -> None:
        self.assertTrue(validate_event(base_event(version=0)))
        self.assertTrue(validate_event(base_event(version="1")))

    def test_payload_required_per_event_type(self) -> None:
        for event_type in EVENT_TYPES:
            self.assertIsInstance(payload_required(event_type), tuple)
        self.assertIn("list_id", payload_required("RESULT_LIST_PUBLISHED"))

    def test_every_event_type_has_aggregate_home(self) -> None:
        self.assertEqual(set(EVENT_TYPES), set(EVENT_AGGREGATE))

    def test_face_mismatch_cannot_auto_disqualify(self) -> None:
        event = base_event(
            event_id="f1", event_type="FACE_MISMATCH_FLAGGED",
            aggregate_type="investigation", aggregate_id="investigation-c1",
            payload={"case_id": "c1", "entry_id": "E1", "bib": "0001",
                     "evidence_ref": "F/1", "flagged_at": "2026-09-21T09:00:00+08:00",
                     "automatic_disqualification": True},
            provenance={"source_type": "face_recognition_system",
                        "document_ref": "F/1", "captured_at": "2026-09-21T09:00:00+08:00"},
        )
        self.assertTrue(any("不得直接取消资格" in e for e in validate_event(event)))

    def test_face_flag_without_dq_is_valid(self) -> None:
        event = base_event(
            event_id="f1", event_type="FACE_MISMATCH_FLAGGED",
            aggregate_type="investigation", aggregate_id="investigation-c1",
            payload={"case_id": "c1", "entry_id": "E1", "bib": "0001",
                     "evidence_ref": "F/1", "flagged_at": "2026-09-21T09:00:00+08:00",
                     "automatic_disqualification": False},
            provenance={"source_type": "face_recognition_system",
                        "document_ref": "F/1", "captured_at": "2026-09-21T09:00:00+08:00"},
        )
        self.assertEqual(validate_event(event), [])

    def test_adjudication_rejects_timing_source(self) -> None:
        decision = {"decision_id": "d1", "deciding_body": "仲裁", "decided_at": "2026-10-01T10:00:00+08:00",
                    "signed_by": "仲裁长", "document_ref": "D/1", "outcome": "disqualified"}
        event = base_event(
            event_id="dq1", event_type="DISQUALIFICATION_ADJUDICATED",
            aggregate_type="investigation", aggregate_id="investigation-c1",
            payload={"case_id": "c1", "entry_id": "E1", "decision": decision,
                     "decided_at": "2026-10-01T10:00:00+08:00"},
            provenance={"source_type": "face_recognition_system",
                        "document_ref": "D/1", "captured_at": "2026-10-01T10:00:00+08:00"},
        )
        self.assertTrue(any("有权裁决来源" in e for e in validate_event(event)))


if __name__ == "__main__":
    unittest.main()
