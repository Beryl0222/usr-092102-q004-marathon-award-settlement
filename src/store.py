"""只增不改的事件存储。

保证：
- event_id 全局唯一：重复提交被拒绝，任何结果不会计算两次；
- 同一聚合 version 从 1 起连续、occurred_at 单调递增：乱序投递被隔离在边界之外；
- caused_by 引用的事件必须存在（裁决链不得悬空）；
- 事件落库前必须通过契约校验。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .validator import validate_event


class DomainError(Exception):
    """领域规则被违反。"""


class DuplicateEventError(DomainError):
    pass


class UnexpectedVersionError(DomainError):
    pass


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class EventStore:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._by_aggregate: dict[str, list[dict]] = defaultdict(list)
        self._ids: set[str] = set()

    @property
    def events(self) -> list[dict]:
        """按全局提交顺序返回全部事件（只增不改）。"""
        return list(self._events)

    def events_for(self, aggregate_id: str) -> list[dict]:
        return list(self._by_aggregate[aggregate_id])

    def exists(self, event_id: str) -> bool:
        return event_id in self._ids

    def get(self, event_id: str) -> dict | None:
        for event in self._events:
            if event["event_id"] == event_id:
                return event
        return None

    def next_version(self, aggregate_id: str) -> int:
        return len(self._by_aggregate[aggregate_id]) + 1

    def append(self, event: dict, *, expect_causal: bool = True) -> dict:
        """追加一个事件。重复 event_id 抛 DuplicateEventError；乱序版本抛 UnexpectedVersionError。"""
        errors = validate_event(event)
        if errors:
            raise DomainError(f"事件 {event.get('event_id')} 契约校验失败：{'; '.join(errors)}")

        event_id = event["event_id"]
        if event_id in self._ids:
            raise DuplicateEventError(f"事件 {event_id} 已存在，重复结果不得计算两次")

        aggregate_id = event["aggregate_id"]
        expected_version = self.next_version(aggregate_id)
        if event["version"] != expected_version:
            raise UnexpectedVersionError(
                f"聚合 {aggregate_id} 期望 version={expected_version}，收到 {event['version']}（乱序或跳号）"
            )

        history = self._by_aggregate[aggregate_id]
        if history:
            last_ts = _parse_ts(history[-1]["occurred_at"])
            if _parse_ts(event["occurred_at"]) < last_ts:
                raise DomainError(
                    f"聚合 {aggregate_id} 的 occurred_at 必须单调递增："
                    f"{event['occurred_at']} 早于 {history[-1]['occurred_at']}"
                )

        if expect_causal:
            for ref in event.get("caused_by", []):
                if ref not in self._ids:
                    raise DomainError(f"事件 {event_id} 的因果引用 {ref} 不存在，裁决链不得悬空")

        stored = dict(event)
        self._events.append(stored)
        self._by_aggregate[aggregate_id].append(stored)
        self._ids.add(event_id)
        return stored

    def ingest(self, events: list[dict]) -> list[dict]:
        """容错投递：按 (聚合, version) 归位，自动跳过重复与乱序事件。

        返回真正写入的事件；调用方可据差异报警，而不是把乱序数据计入账目。
        """
        pending = sorted(events, key=lambda e: (e["aggregate_id"], e["version"]))
        stored: list[dict] = []
        for event in pending:
            try:
                stored.append(self.append(event))
            except (DuplicateEventError, UnexpectedVersionError, DomainError):
                continue
        return stored

    def load_jsonl(self, path: str | Path) -> None:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                self.append(json.loads(line))

    def write_jsonl(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for event in self._events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
