"""事件目录：事件类型、聚合类型与事件-聚合归属。

与 contracts/domain.schema.json 保持单一事实来源——payload 必填字段直接从
schema 的条件分支派生，本模块只维护事件到聚合的归属等 schema 不表达的知识。
"""
from __future__ import annotations

import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "domain.schema.json"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


SCHEMA = load_schema()

EVENT_TYPES: tuple[str, ...] = tuple(SCHEMA["properties"]["event_type"]["enum"])
AGGREGATE_TYPES: tuple[str, ...] = tuple(SCHEMA["properties"]["aggregate_type"]["enum"])

# 事件归属的聚合。新增事件时在此登记，乱挂聚合会被拒绝。
EVENT_AGGREGATE: dict[str, str] = {
    "RACE_ENTRY_REGISTERED": "race_entry",
    "COMPETITION_REGISTERED": "race_edition",
    "EVENT_PROGRAM_DEFINED": "race_edition",
    "RULEBOOK_PUBLISHED": "race_edition",
    "AWARD_SCHEDULE_PUBLISHED": "race_edition",
    "TIMING_RECEIVED": "timing_evidence",
    "BIB_CHECKPOINT_RECORDED": "timing_evidence",
    "RESULT_LIST_PUBLISHED": "result_list",
    "RESULT_LIST_AMENDED": "result_list",
    "RESULT_LIST_FROZEN": "result_list",
    "ELIGIBILITY_REVIEWED": "race_entry",
    "FACE_MISMATCH_FLAGGED": "investigation",
    "BIB_DISPUTE_OPENED": "investigation",
    "DOPING_CASE_OPENED": "investigation",
    "DOPING_RESULT_ENTERED": "investigation",
    "APPEAL_OPENED": "investigation",
    "APPEAL_DECIDED": "investigation",
    "INVESTIGATION_DECIDED": "investigation",
    "DISQUALIFICATION_ADJUDICATED": "investigation",
    "RECORD_PERFORMANCE_DECLARED": "course_record",
    "RECORD_RATIFIED": "course_record",
    "RECORD_REVOKED": "course_record",
    "AWARD_CALCULATED": "award_entitlement",
    "ENTITLEMENT_SUSPENDED": "award_entitlement",
    "ENTITLEMENT_HOLD_RESOLVED": "award_entitlement",
    "ENTITLEMENT_FROZEN": "award_entitlement",
    "ENTITLEMENT_VOIDED": "award_entitlement",
    "TAX_DETAILS_SUBMITTED": "settlement_entry",
    "WITHHOLDING_CALCULATED": "settlement_entry",
    "PAYMENT_BATCH_OPENED": "payment_batch",
    "PAYMENT_RELEASED": "settlement_entry",
    "PAYMENT_REVERSED": "settlement_entry",
    "MAKEUP_PAYMENT_RELEASED": "settlement_entry",
    "RESULT_REVISED": "award_entitlement",
}

# 只有携带签署裁决（signed_decision）才能产生法律效果的事件。
SIGNED_DECISION_EVENTS = frozenset(
    {
        "APPEAL_DECIDED",
        "INVESTIGATION_DECIDED",
        "DISQUALIFICATION_ADJUDICATED",
        "RECORD_REVOKED",
    }
)

# 处罚类来源限制：人脸系统等自动证据不得用于取消资格。
NON_ADJUDICATIVE_SOURCES = frozenset(
    {
        "face_recognition_system",
        "chip_vendor_feed",
        "official_timing",
    }
)


def payload_required(event_type: str) -> tuple[str, ...]:
    """从 schema 根层 allOf 条件分支读取该事件 payload 的必填字段。"""
    for branch in SCHEMA.get("allOf", []):
        cond = branch.get("if", {})
        if cond.get("properties", {}).get("event_type", {}).get("const") == event_type:
            return tuple(
                branch["then"]["properties"]["payload"].get("required", [])
            )
    return ()
