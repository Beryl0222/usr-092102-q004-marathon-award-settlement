# 马拉松奖金核算台

赛事奖金在发奖日前常常面对三份都像"最终版"的榜单（枪声总榜、净计时分枪榜、新增特别奖名单）、
未决的兴奋剂/申诉/替跑调查，以及随时可能到来的递补与改判。本仓库用**事件溯源（event sourcing）**
实现一套可冻结、可重算、可审计的奖金核算领域：所有暂缓、确认、改判都作为只增不改的事件落库，
任何一笔已付或待付金额都能重新算出当时采用的榜单、资格与规则。

## 领域原则

1. **从证据与有权裁决开始**：官方计时（枪声/净计时）只陈述成绩事实；取消资格、撤销纪录只能由
   携带签署人、裁决机构的 `signed_decision` 驱动，计时/人脸证据来源会被校验器拒绝用于处罚。
2. **人脸检录异常不直接处罚**：`FACE_MISMATCH_FLAGGED` 只能开立替跑调查并可暂缓权益，
   payload 强制 `automatic_disqualification=false`；是否违规由签署的 `INVESTIGATION_DECIDED` 决定。
3. **榜单按奖项种类采用，且封存后只出修订版**：名次奖按枪声榜、中国籍前八特别奖按净计时分枪榜、
   破纪录奖按枪声榜；榜单冻结后改判以 `RESULT_LIST_AMENDED`（修订号递增、变更明细）表达，
   不覆盖原件。
4. **规则生效期**：`RULEBOOK_PUBLISHED` / `AWARD_SCHEDULE_PUBLISHED` 带 `effective_from`，
   核算时按时刻选取已生效版本；特别奖在其生效期之前不会被计入。
5. **可冻结的获奖权益**：`AWARD_CALCULATED`（provisional/confirmed）→
   `ENTITLEMENT_FROZEN` 把"榜单修订号 + 奖项表版本 + 规则版本 + 资格依据快照"钉在权益上。
6. **付款门槛**：必须先有税务资料、代扣计税、冻结权益、批次且无生效暂缓，`PAYMENT_RELEASED`
   才允许发生——禁止"先付款再等申诉和兴奋剂结果"；同一冻结只能支付一次。
7. **连续账目，改判不抹账**：取消资格若已付款，追加 `PAYMENT_REVERSED` 冲正（原付款事实保留）；
   递补产生新核算/新冻结，以 `MAKEUP_PAYMENT_RELEASED`（支持差额补付）发放；每次改判追加
   `RESULT_REVISED` 更正记录（clawback/makeup/none），从不删除历史事件。
8. **并列与奖项叠加**：并列按规则均分所占奖金名次的奖金池（余数按号码布分配）；同一
   `stack_group` 内奖项只取最高（名次奖与中国籍特别奖不叠加），破纪录奖无分组可叠加；
   破纪录奖须先 `RECORD_RATIFIED`，撤销由签署的 `RECORD_REVOKED` 驱动。
9. **重复/乱序不算两次**：`event_id` 全局唯一，聚合内 `version` 连续、`occurred_at` 单调递增，
   乱序/跳号/重复在存储边界被拒绝；`caused_by` 因果链不得悬空。

## 目录

- `contracts/domain.schema.json`：事件信封、34 种事件、9 类聚合、`provenance`/`signed_decision`/
  `freeze_basis` 等结构约定（Draft 2020-12，payload 按事件类型条件约束）。
- `src/events.py`：事件目录与事件-聚合归属，payload 必填项直接从 schema 派生（单一事实来源）。
- `src/validator.py`：标准库实现的 schema 子集求值 + 跨字段语义校验（人脸不直罚、裁决须签署等）。
- `src/store.py`：只增不改事件存储（幂等、版本连续、时间单调、因果链检查、JSONL 落盘）。
- `src/model.py`：事件流归约现态与奖金计算引擎（名次/特别/破纪录、并列均分、奖项叠加、国籍门槛）。
- `src/services.py`：写路径领域服务（调查、榜单修订、核算、暂缓/解除、冻结、计税、批次、
  放款、冲正、差额补付、改判更正）。
- `src/query.py`：选手视图（成绩状态、适用奖项、未决事项、连续账目）与 `trace_amount`
  金额重算审计追踪（截至冻结时点重放，独立重算并核对裁决链）。
- `src/scenario.py`：太原马拉松端到端样例，导出 `data/taiyuan_marathon_events.jsonl`。
- `data/`：联调样例 `sample.json` 与完整样例事件流。
- `tests/`：契约、存储、奖金引擎、付款守卫、调查/改判/冲正、纪录与规则生效期等 31 个测试。

## 端到端样例

```bash
python3 -m src.scenario        # 生成 data/taiyuan_marathon_events.jsonl 并打印关键视图与审计追踪
python3 -m unittest discover -s tests
```

样例情节：三份榜单并存且奖项表 09-24 才加入中国籍前八特别奖；何强人脸异常仅立案调查、无违规后
正常放款；李伟兴奋剂阳性（未付款，榜单 r2 递补）；孙浩号码布转让赛后裁决（冲正已付款、后续 r3
差额补付）；王军/赵磊同成绩并列均分；王军赛会纪录先 ratify 计奖、后经签署撤销，全额冲正后按
新冻结重发。选手查询可见当前状态与未决事项；任意付款都能用 `trace_amount` 复原其榜单修订、
规则版本、资格依据与裁决链并重新算出金额。
