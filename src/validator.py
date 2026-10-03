"""领域事件契约校验。

- validate_event：按 contracts/domain.schema.json 求值（标准库实现的 Draft 2020-12
  子集：type/required/properties/items/enum/const/minimum/minLength/$ref/allOf/if/then），
  并追加跨字段语义检查（事件-聚合归属、签署裁决来源、人脸异常不得直罚等）。
"""
from __future__ import annotations

from datetime import datetime

from .events import (
    EVENT_AGGREGATE,
    NON_ADJUDICATIVE_SOURCES,
    SCHEMA,
    SIGNED_DECISION_EVENTS,
    payload_required,
)

_ENVELOPE_REQUIRED = (
    "event_id",
    "event_type",
    "aggregate_type",
    "aggregate_id",
    "occurred_at",
    "version",
    "summary",
)


def _check_type(expected: str, instance) -> bool:
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "boolean":
        return isinstance(instance, bool)
    return True


def _walk(schema: dict, instance, path: str, errors: list[str]) -> None:
    """对本契约用到的 JSON Schema 关键字做最小求值。"""
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/$defs/"):
            _walk(SCHEMA["$defs"][ref.rsplit("/", 1)[-1]], instance, path, errors)
        return

    expected = schema.get("type")
    if expected and not _check_type(expected, instance):
        errors.append(f"{path} 类型应为 {expected}")
        return

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path} 不在允许枚举内：{instance!r}")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path} 必须等于 {schema['const']!r}")
    if "minimum" in schema and isinstance(instance, int) and instance < schema["minimum"]:
        errors.append(f"{path} 不得小于 {schema['minimum']}")
    if "minLength" in schema and isinstance(instance, str) and len(instance) < schema["minLength"]:
        errors.append(f"{path} 长度不足 {schema['minLength']}")
    if "maxLength" in schema and isinstance(instance, str) and len(instance) > schema["maxLength"]:
        errors.append(f"{path} 长度超过 {schema['maxLength']}")

    if expected == "object" or isinstance(instance, dict):
        for key in schema.get("required", []):
            if not isinstance(instance, dict) or key not in instance:
                errors.append(f"{path}.{key} 为必填项")
        if isinstance(instance, dict):
            for key, subschema in schema.get("properties", {}).items():
                if key in instance:
                    _walk(subschema, instance[key], f"{path}.{key}", errors)
            if schema.get("additionalProperties") is False:
                allowed = set(schema.get("properties", {}))
                for key in instance:
                    if key not in allowed:
                        errors.append(f"{path}.{key} 为未声明字段")

    if expected == "array" or isinstance(instance, list):
        if isinstance(instance, list) and "items" in schema:
            for i, item in enumerate(instance):
                _walk(schema["items"], item, f"{path}[{i}]", errors)

    if "format" in schema and schema["format"] == "date-time" and isinstance(instance, str):
        try:
            datetime.fromisoformat(instance)
        except ValueError:
            errors.append(f"{path} 不是合法的 date-time")

    for branch in schema.get("allOf", []):
        cond = branch.get("if")
        if cond is None or _matches(cond, instance):
            _walk(branch["then"], instance, path, errors)


def _matches(cond: dict, instance) -> bool:
    """求值 if 分支：仅支持 properties 内的 const 条件（本契约足够）。"""
    if not isinstance(instance, dict):
        return False
    for key, rule in cond.get("properties", {}).items():
        if "const" in rule and instance.get(key) != rule["const"]:
            return False
        if "enum" in rule and instance.get(key) not in rule["enum"]:
            return False
    return True


def validate_event(record: dict) -> list[str]:
    """返回错误列表；空列表表示通过。保持历史调用方式。"""
    errors: list[str] = [
        f"缺少字段：{name}" for name in _ENVELOPE_REQUIRED if name not in record
    ]
    if errors:
        return errors

    schema_errors: list[str] = []
    _walk(SCHEMA, record, "$", schema_errors)
    errors.extend(schema_errors)

    event_type = record.get("event_type")
    aggregate_type = record.get("aggregate_type")

    expected_agg = EVENT_AGGREGATE.get(event_type)
    if expected_agg and aggregate_type != expected_agg:
        errors.append(
            f"{event_type} 必须归属聚合 {expected_agg}，实际为 {aggregate_type}"
        )

    payload = record.get("payload")
    provenance = record.get("provenance")
    if isinstance(payload, dict):
        for field in payload_required(event_type):
            if field not in payload:
                errors.append(f"payload.{field} 为 {event_type} 的必填项")

        # 人脸异常是中立线索：若携带自动取消资格标记必须为 false。
        if event_type == "FACE_MISMATCH_FLAGGED":
            if payload.get("automatic_disqualification") is True:
                errors.append("人脸检录异常只能进入替跑调查，不得直接取消资格")

    # 产生法律效果的裁决事件，证据来源必须是有权裁决机构。
    if event_type in SIGNED_DECISION_EVENTS and isinstance(provenance, dict):
        source = provenance.get("source_type")
        if source in NON_ADJUDICATIVE_SOURCES:
            errors.append(
                f"{event_type} 需要有权裁决来源，计时/人脸证据不得作为取消资格或撤销依据"
            )

    if isinstance(payload, dict) and event_type in SIGNED_DECISION_EVENTS:
        decision = payload.get("decision")
        if isinstance(decision, dict) and not decision.get("signed_by"):
            errors.append("裁决必须由签署人 signed_by 负责")

    return errors
