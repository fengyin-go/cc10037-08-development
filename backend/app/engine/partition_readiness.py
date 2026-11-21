"""分区齐备校验与待补清单引擎。

按目标表把「生产日历」和「上游到达明细」对齐，判断某个业务日期是否应产、
是否已经到期、数据是否齐备、是否完成校验，形成待补分区清单，并据此生成
补偿批次。

核心口径：
- 应产日期由生产日历决定，停产日默认不应产。
- 已到期（业务日期 + 管线阈值 <= 评估日）才进入待补判断。
- 上游到达明细的齐备率低于管线阈值 → 未达阈值。
- 有到达但未校验、或校验失败 → 不可供下游读取。
- 停产日仍有数据到达 → 停产日冲突，默认不纳入当期，需人工确认或等日历修订。
- 补偿批次按已实际校验通过的分区串行衔接，上一批验过的分区是下一批的输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any, Iterable


class ReadinessState(str, Enum):
    """单个「表 + 业务日期」的齐备状态。"""

    CONFIRMABLE = "可确认"            # 已校验且齐备，可直接供下游读取
    MISSING = "缺数"                  # 已到期但没有上游到达
    UNDER_THRESHOLD = "未达阈值"      # 有到达但齐备率低于管线阈值
    UNVALIDATED = "未校验"            # 有到达且齐备，但尚未跑校验
    VALIDATION_FAILED = "校验失败"    # 校验未通过，需要重跑
    SHUTDOWN = "停产日"               # 停产日且无数据，不进入待补清单
    SHUTDOWN_CONFLICT = "停产日冲突"  # 停产日仍有数据到达，需确认是否纳入当期
    INCLUDED = "已确认纳入当期"       # 停产日额外到达已被确认纳入且完成校验
    INCLUDED_PENDING = "纳入待校验"   # 已确认纳入，但校验尚未完成
    NOT_DUE = "未到期"                # 尚未到该管线的应产确认时点


PENDING_STATES = frozenset({
    ReadinessState.MISSING,
    ReadinessState.UNDER_THRESHOLD,
    ReadinessState.UNVALIDATED,
    ReadinessState.VALIDATION_FAILED,
    ReadinessState.SHUTDOWN_CONFLICT,
    ReadinessState.INCLUDED_PENDING,
})

COMPENSABLE_STATES = frozenset({
    ReadinessState.MISSING,
    ReadinessState.UNDER_THRESHOLD,
    ReadinessState.UNVALIDATED,
    ReadinessState.VALIDATION_FAILED,
})

ACTION_BY_STATE: dict[ReadinessState, str] = {
    ReadinessState.MISSING: "重跑补数",
    ReadinessState.UNDER_THRESHOLD: "重跑补数",
    ReadinessState.VALIDATION_FAILED: "重跑补数",
    ReadinessState.UNVALIDATED: "补校验",
    ReadinessState.INCLUDED_PENDING: "补校验",
    ReadinessState.SHUTDOWN_CONFLICT: "待停产口径确认",
}


@dataclass(frozen=True)
class CalendarVersion:
    """一个版本的生产日历。停产日以 ISO 日期集合保存。"""

    calendar_id: str
    name: str
    version: str
    effective_from: str
    effective_to: str
    shutdown_dates: frozenset[str]
    status: str = "生效"             # 生效 / 草稿 / 已更正
    note: str = ""
    created_at: str = ""

    def is_shutdown(self, biz_date: str) -> bool:
        return biz_date in self.shutdown_dates


@dataclass(frozen=True)
class TableRule:
    """按目标表配置的应产校验口径。"""

    rule_id: str
    table_name: str
    pipeline: str
    calendar_id: str
    threshold_days: int = 1          # 业务日期后第几天视为应产已确认（T+N）
    completeness_threshold: float = 99.0  # 上游明细齐备率阈值（百分比）
    rule_version: str = "R1"
    status: str = "已启用"

    def due_on(self, biz_date: str) -> date:
        return date.fromisoformat(biz_date) + timedelta(days=self.threshold_days)


@dataclass(frozen=True)
class ArrivalRecord:
    """一次上游到达明细（同一业务日期多次到达时取最晚一次）。"""

    arrival_id: str
    table_name: str
    pipeline: str
    biz_date: str
    arrived_at: str
    row_count: int
    completeness: float
    source_batch: str
    evidence: str


@dataclass(frozen=True)
class ValidationRecord:
    """日分区的校验记录。"""

    table_name: str
    biz_date: str
    status: str                      # 已校验 / 待校验 / 校验失败
    checked_at: str
    calendar_version: str
    evidence: str
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "已校验"


@dataclass
class DateAssessment:
    """单个「表 + 业务日期」的完整判断结果。"""

    table_name: str
    pipeline: str
    biz_date: str
    weekday: str
    shutdown: bool
    due: bool
    due_date: str
    expected: bool
    state: ReadinessState
    reason: str
    arrival: ArrivalRecord | None
    validation: ValidationRecord | None
    readable: bool
    conflict: bool
    includable: bool
    calendar_version: str
    rule_version: str
    rule_id: str
    evidence: str
    resolution: str = ""
    row_count: int = 0
    completeness: float = 0.0

    @property
    def pending(self) -> bool:
        return self.state in PENDING_STATES

    @property
    def compensable(self) -> bool:
        return self.state in COMPENSABLE_STATES

    def arrival_payload(self) -> dict[str, Any] | None:
        if self.arrival is None:
            return None
        a = self.arrival
        return {
            "arrival_id": a.arrival_id,
            "biz_date": a.biz_date,
            "arrived_at": a.arrived_at,
            "row_count": a.row_count,
            "completeness": a.completeness,
            "source_batch": a.source_batch,
            "evidence": a.evidence,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "pipeline": self.pipeline,
            "biz_date": self.biz_date,
            "weekday": self.weekday,
            "shutdown": self.shutdown,
            "due": self.due,
            "due_date": self.due_date,
            "expected": self.expected,
            "state": self.state.value,
            "reason": self.reason,
            "arrival": self.arrival_payload(),
            "validation_status": (
                self.validation.status if self.validation else "未校验"
            ),
            "readable": self.readable,
            "conflict": self.conflict,
            "includable": self.includable,
            "resolution": self.resolution,
            "calendar_version": self.calendar_version,
            "rule_version": self.rule_version,
            "rule_id": self.rule_id,
            "row_count": self.row_count,
            "completeness": self.completeness,
            "evidence": self.evidence,
        }


def _weekday_cn(biz_date: str) -> str:
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[date.fromisoformat(biz_date).weekday()]


def _latest_arrival(
    arrivals: Iterable[ArrivalRecord], biz_date: str
) -> ArrivalRecord | None:
    candidates = [a for a in arrivals if a.biz_date == biz_date]
    if not candidates:
        return None
    return max(candidates, key=lambda a: (a.arrived_at, a.arrival_id))


class PartitionReadinessEngine:
    """分区齐备校验引擎（纯计算，不依赖数据仓库）。"""

    # ---- 单表评估 ----

    def evaluate_table(
        self,
        rule: TableRule,
        calendar: CalendarVersion,
        arrivals: list[ArrivalRecord],
        validations: list[ValidationRecord],
        *,
        start_date: str,
        end_date: str,
        today: date | None = None,
        resolutions: dict[str, str] | None = None,
    ) -> list[DateAssessment]:
        """评估一张目标表在 [start_date, end_date] 内每个业务日期的齐备情况。"""
        today = today or date.today()
        resolutions = resolutions or {}
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        if end < start:
            return []

        by_date: dict[str, list[ArrivalRecord]] = {}
        for arrival in arrivals:
            if arrival.table_name == rule.table_name:
                by_date.setdefault(arrival.biz_date, []).append(arrival)

        validation_by_date = {
            v.biz_date: v for v in validations if v.table_name == rule.table_name
        }

        assessments: list[DateAssessment] = []
        current = start
        while current <= end:
            biz_date = current.isoformat()
            arrival = _latest_arrival(by_date.get(biz_date, []), biz_date)
            validation = validation_by_date.get(biz_date)
            resolution = resolutions.get(biz_date, "")

            shutdown = calendar.is_shutdown(biz_date)
            due = today >= rule.due_on(biz_date)
            expected = not shutdown

            state, reason, readable, conflict, includable = self._classify(
                rule=rule,
                calendar=calendar,
                biz_date=biz_date,
                shutdown=shutdown,
                due=due,
                arrival=arrival,
                validation=validation,
                resolution=resolution,
            )

            assessments.append(DateAssessment(
                table_name=rule.table_name,
                pipeline=rule.pipeline,
                biz_date=biz_date,
                weekday=_weekday_cn(biz_date),
                shutdown=shutdown,
                due=due,
                due_date=rule.due_on(biz_date).isoformat(),
                expected=expected,
                state=state,
                reason=reason,
                arrival=arrival,
                validation=validation,
                readable=readable,
                conflict=conflict,
                includable=includable,
                calendar_version=calendar.version,
                rule_version=rule.rule_version,
                rule_id=rule.rule_id,
                evidence=(
                    validation.evidence if validation and validation.evidence
                    else arrival.evidence if arrival
                    else ""
                ),
                resolution=resolution,
                row_count=arrival.row_count if arrival else 0,
                completeness=arrival.completeness if arrival else 0.0,
            ))
            current += timedelta(days=1)

        return assessments

    @staticmethod
    def _classify(
        *,
        rule: TableRule,
        calendar: CalendarVersion,
        biz_date: str,
        shutdown: bool,
        due: bool,
        arrival: ArrivalRecord | None,
        validation: ValidationRecord | None,
        resolution: str,
    ) -> tuple[ReadinessState, str, bool, bool, bool]:
        """返回 (状态, 原因, 是否可读, 是否冲突, 是否可纳入当期)。"""

        if shutdown:
            if arrival is None:
                return (
                    ReadinessState.SHUTDOWN,
                    f"{calendar.version} 标记 {biz_date} 为停产日，无需应产",
                    False, False, False,
                )
            # 停产日仍有数据到达：冲突。
            if resolution == "纳入当期":
                validated = validation is not None and validation.passed
                if validated:
                    return (
                        ReadinessState.INCLUDED,
                        "停产日额外到达已确认纳入当期，且校验通过",
                        True, True, True,
                    )
                return (
                    ReadinessState.INCLUDED_PENDING,
                    "停产日额外到达已确认纳入当期，但尚未校验",
                    False, True, True,
                )
            note = "等待停产口径修订" if resolution == "等待修订" else "尚未确认"
            return (
                ReadinessState.SHUTDOWN_CONFLICT,
                f"停产日仍有 {arrival.row_count} 行到达，{note}；默认不纳入当期",
                False, True, False,
            )

        if not due:
            return (
                ReadinessState.NOT_DUE,
                f"管线阈值 T+{rule.threshold_days}，{biz_date} 尚未到应产确认时点",
                False, False, False,
            )

        if arrival is None:
            return (
                ReadinessState.MISSING,
                f"已到应产确认时点（T+{rule.threshold_days}），上游无到达明细",
                False, False, False,
            )

        if arrival.completeness < rule.completeness_threshold:
            return (
                ReadinessState.UNDER_THRESHOLD,
                (
                    f"上游齐备率 {arrival.completeness:.2f}% 低于管线阈值 "
                    f"{rule.completeness_threshold:.2f}%"
                ),
                False, False, False,
            )

        if validation is None or validation.status == "待校验":
            return (
                ReadinessState.UNVALIDATED,
                "上游到达齐备，但日分区尚未校验，不可供下游读取",
                False, False, False,
            )

        if not validation.passed:
            return (
                ReadinessState.VALIDATION_FAILED,
                f"日分区校验失败：{validation.detail or '未通过质量门禁'}",
                False, False, False,
            )

        return (
            ReadinessState.CONFIRMABLE,
            "应产日期已确认、到达齐备且校验通过",
            True, False, False,
        )

    # ---- 待补清单 ----

    def build_pending_rows(
        self,
        assessments: list[DateAssessment],
        *,
        existing: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """把评估结果折叠成待补分区清单行。

        同一天已有「已排批 / 已补偿 / 已完成」记录时保留其执行状态与批次，
        只刷新判断口径（日历版本、有效日期、证据）。
        """
        existing = existing or {}
        rows: list[dict[str, Any]] = []
        for item in assessments:
            key = f"{item.table_name}#{item.biz_date}"
            prior = existing.get(key, {})
            if (
                item.state in (ReadinessState.NOT_DUE, ReadinessState.SHUTDOWN)
                and not prior.get("补偿批次")
            ):
                continue
            prior_status = prior.get("状态") or prior.get("status")
            status = self._row_status(item)
            batch_id = prior.get("补偿批次", "")
            executed_calendar = prior.get("执行日历版本", "")
            readable = item.readable
            if prior_status in ("已排批", "已补偿", "已完成"):
                # 已经实际补偿的日期保留原状态与批次，日历更正也不回退。
                status = prior_status
                batch_id = prior.get("补偿批次", batch_id)
                executed_calendar = (
                    prior.get("执行日历版本") or prior.get("日历版本", "")
                )
                if prior.get("下游可读") == "是":
                    readable = True

            warning = ""
            if item.state is ReadinessState.SHUTDOWN_CONFLICT:
                warning = "停产日额外到达，需确认是否纳入当期或等待停产口径修订"
            elif (
                item.state is ReadinessState.SHUTDOWN
                and prior_status in ("已排批", "已补偿", "已完成")
            ):
                warning = (
                    "日历更正后该日已属停产；更正前已完成的数据保留在原批次，"
                    "不再进入应产范围"
                )

            rows.append({
                "partition_id": f"PR-{item.table_name}-{item.biz_date}",
                "目标表": item.table_name,
                "管线": item.pipeline,
                "业务日期": item.biz_date,
                "星期": item.weekday,
                "应产判断": "停产" if item.shutdown else "应产",
                "应产确认日": "—" if item.shutdown else item.due_date,
                "实到日期": item.biz_date if item.arrival else "—",
                "到达行数": item.row_count,
                "齐备率": f"{item.completeness:.2f}%",
                "校验状态": item.validation.status if item.validation else "未校验",
                "待补原因": item.reason,
                "状态": status,
                "处理动作": ACTION_BY_STATE.get(item.state, "重跑补数"),
                "日历版本": item.calendar_version,
                "判断有效日期": (
                    item.biz_date if (item.expected or item.includable)
                    else "停产日不纳入"
                ),
                "执行日历版本": executed_calendar,
                "规则版本": item.rule_version,
                "到达证据": item.arrival.evidence if item.arrival else "—",
                "校验证据": (
                    item.validation.evidence if item.validation else "—"
                ) or "—",
                "补偿批次": batch_id,
                "停产冲突": "是" if item.conflict else "否",
                "可纳入当期": "是" if item.includable else "否",
                "确认结论": item.resolution or "—",
                "下游可读": "是" if readable else "否",
                "提示": warning,
                "pending": status in ("待补", "待确认", "待校验"),
                "abnormal": item.state in (
                    ReadinessState.SHUTDOWN_CONFLICT,
                    ReadinessState.VALIDATION_FAILED,
                    ReadinessState.MISSING,
                ),
                "判断有效": item.expected or item.includable,
                "是否到期": item.due,
            })
        return rows

    @staticmethod
    def _row_status(item: DateAssessment) -> str:
        if item.state is ReadinessState.SHUTDOWN_CONFLICT:
            return "待确认"
        if item.state in (ReadinessState.UNVALIDATED, ReadinessState.INCLUDED_PENDING):
            return "待校验"
        if item.state is ReadinessState.CONFIRMABLE or item.state is ReadinessState.INCLUDED:
            return "已完成"
        return "待补"

    # ---- 冲突与相邻日期 ----

    def conflict_review(
        self,
        assessments: list[DateAssessment],
        biz_date: str,
    ) -> dict[str, Any]:
        """给出停产日冲突本身和前后相邻日期的可用性说明。"""
        by_date = {a.biz_date: a for a in assessments}
        center = by_date.get(biz_date)
        if center is None:
            return {"error": f"{biz_date} 不在评估范围内"}
        if not center.conflict:
            return {
                "conflict_date": biz_date,
                "has_conflict": False,
                "message": "该日期没有停产日到达冲突",
            }

        center_dt = date.fromisoformat(biz_date)
        adjacent: list[dict[str, Any]] = []
        for offset in (-1, 0, 1):
            current = (center_dt + timedelta(days=offset)).isoformat()
            item = by_date.get(current)
            if item is None:
                continue
            effect = ""
            if offset == 0:
                effect = (
                    "额外到达计入当期，可作为相邻日期补偿输入"
                    if item.includable
                    else "额外到达未确认，不得作为任何日期的补偿输入"
                )
            elif item.includable:
                effect = "可将停产日额外到达并入当期齐备率核算"
            elif item.state is ReadinessState.SHUTDOWN:
                effect = "停产日本身不纳入，本日期只按自身到达与校验判断"
            else:
                effect = "停产日数据不可并入，本日期仍按自身到达与校验判断"
            adjacent.append({
                "biz_date": current,
                "weekday": item.weekday,
                "shutdown": item.shutdown,
                "expected": item.expected,
                "state": item.state.value,
                "readable": item.readable,
                "arrival_rows": item.row_count,
                "completeness": item.completeness,
                "validation_status": (
                    item.validation.status if item.validation else "未校验"
                ),
                "input_effect": effect,
            })

        recommendation = (
            "停产日额外到达已确认纳入当期，补齐校验后可与相邻日期同批调度"
            if center.includable
            else "停产日额外到达默认不纳入当期；相邻日期补偿不得引用该日数据，"
                 "如业务确需纳入，请先修订停产口径（生产日历）后重新评估"
        )
        return {
            "conflict_date": biz_date,
            "has_conflict": True,
            "table_name": center.table_name,
            "pipeline": center.pipeline,
            "calendar_version": center.calendar_version,
            "resolution": center.resolution or "未确认",
            "includable": center.includable,
            "extra_arrival": center.arrival_payload(),
            "adjacent_dates": adjacent,
            "recommendation": recommendation,
        }

    # ---- 下游可读分区 ----

    def readable_partitions(
        self, assessments: list[DateAssessment]
    ) -> list[dict[str, Any]]:
        """只返回校验通过、且不存在停产口径未决的日分区。"""
        return [
            {
                "table_name": a.table_name,
                "pipeline": a.pipeline,
                "biz_date": a.biz_date,
                "calendar_version": a.calendar_version,
                "rule_version": a.rule_version,
                "validation_status": (
                    a.validation.status if a.validation else "未校验"
                ),
                "evidence": a.evidence,
                "row_count": a.row_count,
                "completeness": a.completeness,
            }
            for a in assessments
            if a.readable
        ]

    # ---- 补偿规划 ----

    def plan_batches(
        self,
        pending_rows: list[dict[str, Any]],
        *,
        validated_dates: list[str] | None = None,
        max_per_batch: int = 3,
        calendar_version: str = "",
        rule_version: str = "",
        batch_prefix: str = "CB",
        retained_ranges: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """把待补清单按表拆成补偿批次。

        批次之间串行衔接：第 N 批的 input_partitions 取第 N-1 批的日期，
        只有这些分区都实际校验通过（出现在 validated_dates 中）才可调度。
        """
        validated = set(validated_dates or [])
        retained_ranges = retained_ranges or []
        by_table: dict[str, list[dict[str, Any]]] = {}
        for row in pending_rows:
            if row.get("状态") not in ("待补", "待校验"):
                continue
            by_table.setdefault(row["目标表"], []).append(row)

        batches: list[dict[str, Any]] = []
        for table, rows in sorted(by_table.items()):
            rows = sorted(rows, key=lambda r: r["业务日期"])
            dates = [r["业务日期"] for r in rows]
            for index in range(0, len(dates), max_per_batch):
                chunk_rows = rows[index:index + max_per_batch]
                chunk = [r["业务日期"] for r in chunk_rows]
                previous_same_table = [
                    b for b in batches if b["target_table"] == table
                ]
                if previous_same_table:
                    previous = previous_same_table[-1]
                    # 上一批还没验过的日期继续作为本批输入，直到实际校验通过。
                    input_partitions = list(previous["dates"])
                    input_note = "承接上一批待验分区"
                else:
                    baseline = sorted(
                        d for d in validated if d < chunk[0]
                    )
                    input_partitions = baseline[-max_per_batch:]
                    input_note = (
                        "窗口内无更早的已验分区，作为起始批次"
                        if not input_partitions
                        else "取窗口内最近的已验分区"
                    )
                input_ready = (not input_partitions) or all(
                    d in validated for d in input_partitions
                )

                batches.append({
                    "batch_id": f"{batch_prefix}-{len(batches) + 1}",
                    "target_table": table,
                    "pipeline": chunk_rows[0]["管线"],
                    "dates": chunk,
                    "actions": {
                        r["业务日期"]: r.get("处理动作", "重跑补数")
                        for r in chunk_rows
                    },
                    "input_partitions": input_partitions,
                    "input_note": input_note,
                    "input_ready": input_ready,
                    "status": "可调度" if input_ready else "等待校验",
                    "calendar_version": (
                        chunk_rows[0].get("日历版本") or calendar_version
                    ),
                    "rule_version": chunk_rows[0].get("规则版本") or rule_version,
                    "arrival_evidence": {
                        r["业务日期"]: r.get("到达证据", "—") for r in chunk_rows
                    },
                    "pending_reasons": {
                        r["业务日期"]: r.get("待补原因", "") for r in chunk_rows
                    },
                    "retained_ranges": retained_ranges,
                })
        return batches

    # ---- 日汇总 ----

    def daily_summary(
        self,
        rule: TableRule,
        calendar: CalendarVersion,
        arrivals: list[ArrivalRecord],
        *,
        start_date: str,
        end_date: str,
        validated_dates: list[str] | None = None,
    ) -> dict[str, Any]:
        """汇总一段日期内的到达行数与齐备率，用于核对补偿任务的日汇总范围。"""
        validated = set(validated_dates or [])
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        days: list[dict[str, Any]] = []
        current = start
        total_rows = 0
        while current <= end:
            biz_date = current.isoformat()
            arrival = _latest_arrival(
                [a for a in arrivals if a.table_name == rule.table_name], biz_date
            )
            rows = arrival.row_count if arrival else 0
            total_rows += rows
            days.append({
                "biz_date": biz_date,
                "weekday": _weekday_cn(biz_date),
                "shutdown": calendar.is_shutdown(biz_date),
                "arrived": arrival is not None,
                "row_count": rows,
                "completeness": arrival.completeness if arrival else 0.0,
                "validated": biz_date in validated,
            })
            current += timedelta(days=1)
        return {
            "table_name": rule.table_name,
            "pipeline": rule.pipeline,
            "scope_start": start_date,
            "scope_end": end_date,
            "day_count": len(days),
            "total_rows": total_rows,
            "arrived_days": sum(1 for d in days if d["arrived"]),
            "validated_days": sum(1 for d in days if d["validated"]),
            "days": days,
        }
