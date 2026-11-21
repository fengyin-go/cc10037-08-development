"""分区齐备核对领域服务：生产日历、应产规则、到达明细、待补清单和补偿批次。

这个服务把磁盘上的示例数据翻译成引擎输入，并维护一次运行期内的判断状态：
- 应产规则按目标表配置，停产日来自生效的生产日历；
- 到齐、齐备率和校验状态共同决定某个业务日期是否可供下游读取；
- 待补清单是按表重算出来的，不是简单按日历天数递增；
- 补偿批次从待补清单生成，批次之间以实际校验通过的分区衔接。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from app.engine.partition_readiness import (
    ArrivalRecord,
    CalendarVersion,
    DateAssessment,
    PartitionReadinessEngine,
    TableRule,
    ValidationRecord,
)
from app.store import store

# 评估窗口：覆盖 11 月的周日停产日与最近一次调度。
DEFAULT_WINDOW_START = "2026-11-01"

# 停产日额外到达的人工结论。
RESOLUTION_INCLUDE = "纳入当期"
RESOLUTION_WAIT = "等待修订"


class ReadinessError(ValueError):
    """可直接回给接口的业务错误。"""


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        return default


class PartitionReadinessService:
    """待补分区清单与补偿规划服务。"""

    def __init__(self) -> None:
        self.engine = PartitionReadinessEngine()
        self.today = date.today()
        self.window_start = DEFAULT_WINDOW_START
        self.window_end = self.today.isoformat()

        self.calendars: dict[str, CalendarVersion] = {}
        self.rules: dict[str, TableRule] = {}
        self.resolutions: dict[tuple[str, str], str] = {}
        self.pending_rows: dict[str, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.retained_ranges: dict[str, list[dict[str, Any]]] = {}
        self.last_rebuild: dict[str, Any] = {}
        self._judgment_seq = 0
        self._built = False

        self._load_calendars()
        self._load_rules()
        self._load_resolutions()
        self._load_retained_ranges()
        self.rebuild_pending(reason="启动初始化")

    # ---- 装配 ----

    def _load_calendars(self) -> None:
        for row in store.rows("production_calendar"):
            shutdown = [
                item.strip()
                for item in str(row.get("停产日", "")).split(",")
                if item.strip()
            ]
            start, _, end = str(row.get("生效区间", "")).partition("~")
            calendar = CalendarVersion(
                calendar_id=str(row.get("日历编码", "")),
                name=str(row.get("日历名称", "")),
                version=str(row.get("版本号", "R1")),
                effective_from=start or self.window_start,
                effective_to=end or self.window_end,
                shutdown_dates=frozenset(shutdown),
                status=str(row.get("status", "草稿")),
                note=str(row.get("修订原因", "")),
                created_at=str(row.get("created_at", "")),
            )
            self.calendars[calendar.calendar_id] = calendar

    def _active_calendar(self, calendar_id: str) -> CalendarVersion:
        calendar = self.calendars.get(calendar_id)
        if calendar is None:
            raise ReadinessError(f"生产日历 {calendar_id} 不存在")
        return calendar

    def active_calendar_for(self, table_name: str) -> CalendarVersion:
        rule = self._rule_for(table_name)
        return self._active_calendar(rule.calendar_id)

    def _load_rules(self) -> None:
        for row in store.rows("partition_target_rule"):
            rule = TableRule(
                rule_id=str(row.get("id")),
                table_name=str(row.get("目标表", "")),
                pipeline=str(row.get("目标管线", "")),
                calendar_id=str(row.get("生产日历", "")),
                threshold_days=_to_int(row.get("确认阈值天数"), 1),
                completeness_threshold=_to_float(row.get("齐备率阈值"), 99.0),
                rule_version=str(row.get("规则版本", "R1")),
                status=str(row.get("status", "已启用")),
            )
            self.rules[rule.table_name] = rule

    def _load_resolutions(self) -> None:
        for row in store.rows("shutdown_confirmation"):
            conclusion = str(row.get("确认结论", ""))
            if conclusion in (RESOLUTION_INCLUDE, RESOLUTION_WAIT):
                self.resolutions[
                    (str(row.get("目标表", "")), str(row.get("业务日期", "")))
                ] = conclusion

    def _load_retained_ranges(self) -> None:
        """把历史补偿任务当作已经跑过的日期范围保留下来。"""
        for row in store.rows("rerun_compensation"):
            pipeline = str(row.get("目标管线", ""))
            rule = next(
                (r for r in self.rules.values() if r.pipeline == pipeline), None
            )
            if rule is None:
                continue
            self.retained_ranges.setdefault(rule.table_name, []).append({
                "task_name": str(row.get("任务名称", "")),
                "start": str(row.get("开始分区", "")),
                "end": str(row.get("结束分区", "")),
                "status": str(row.get("status", "")),
                "calendar_version": "CAL-2026-11-R1",
                "rule_version": rule.rule_version,
            })

    # ---- 输入翻译 ----

    def _rule_for(self, table_name: str) -> TableRule:
        rule = self.rules.get(table_name)
        if rule is None:
            raise ReadinessError(f"目标表 {table_name} 没有配置应产校验规则")
        return rule

    def enabled_rules(self) -> list[TableRule]:
        return [
            rule for rule in self.rules.values()
            if rule.status == "已启用"
        ]

    def arrivals(self, table_name: str) -> list[ArrivalRecord]:
        records: list[ArrivalRecord] = []
        for row in store.rows("upstream_arrival"):
            if str(row.get("目标表", "")) != table_name:
                continue
            records.append(ArrivalRecord(
                arrival_id=f"ARR-{row.get('id')}",
                table_name=table_name,
                pipeline=str(row.get("目标管线", "")),
                biz_date=str(row.get("业务日期", "")),
                arrived_at=str(row.get("到达时间", "")),
                row_count=_to_int(row.get("到达行数")),
                completeness=_to_float(row.get("齐备率")),
                source_batch=str(row.get("来源批次", "")),
                evidence=str(row.get("到达证据", "")),
            ))
        return records

    def validations(self, table_name: str) -> list[ValidationRecord]:
        records: list[ValidationRecord] = []
        for row in store.rows("partition_validation"):
            if str(row.get("目标表", "")) != table_name:
                continue
            records.append(ValidationRecord(
                table_name=table_name,
                biz_date=str(row.get("业务日期", "")),
                status=str(row.get("校验状态", "待校验")),
                checked_at=str(row.get("校验时间", "")),
                calendar_version=str(row.get("日历版本", "")),
                evidence=str(row.get("校验证据", "")),
                detail=str(row.get("校验说明", "")),
            ))
        return records

    def validated_dates(self, table_name: str) -> list[str]:
        return sorted(
            v.biz_date for v in self.validations(table_name) if v.passed
        )

    def assessments(self, table_name: str) -> list[DateAssessment]:
        rule = self._rule_for(table_name)
        return self.engine.evaluate_table(
            rule=rule,
            calendar=self._active_calendar(rule.calendar_id),
            arrivals=self.arrivals(table_name),
            validations=self.validations(table_name),
            start_date=self.window_start,
            end_date=self.window_end,
            today=self.today,
            resolutions={
                biz_date: conclusion
                for (table, biz_date), conclusion in self.resolutions.items()
                if table == table_name
            },
        )

    # ---- 待补清单 ----

    def ensure_built(self) -> None:
        if not self._built:
            self.rebuild_pending(reason="启动初始化")

    def rebuild_pending(
        self,
        *,
        tables: Iterable[str] | None = None,
        reason: str = "按最新口径重算",
    ) -> dict[str, Any]:
        """重算待补清单。

        已完成的行保留原补偿批次与执行时的日历版本，只刷新判断口径；
        其余行按本次生产日历重新关联「判断有效日期」。
        """
        targets = list(tables) if tables else [r.table_name for r in self.enabled_rules()]
        changed: list[dict[str, Any]] = []
        # 只重算本次涉及的表，其余表的清单原样保留。
        refreshed: dict[str, dict[str, Any]] = {
            key: row for key, row in self.pending_rows.items()
            if row.get("目标表") not in set(targets)
        }
        judgment_id = self._next_judgment_id()

        for table in targets:
            for item in self.assessments(table):
                key = f"{table}#{item.biz_date}"
                prior = self.pending_rows.get(key)
                existing = {key: prior} if prior else {}
                rows = self.engine.build_pending_rows([item], existing=existing)
                if not rows:
                    continue
                row = rows[0]
                row["判断批次"] = judgment_id
                prior_effective = (prior or {}).get("判断有效日期")
                row["判断有效日期"] = (
                    item.biz_date if (item.expected or item.includable)
                    else "停产日不纳入"
                )
                if prior and prior.get("状态") in ("已补偿", "已完成"):
                    row["状态"] = prior["状态"]
                    row["补偿批次"] = prior.get("补偿批次", "")
                    row["执行日历版本"] = (
                        prior.get("执行日历版本") or prior.get("日历版本", "")
                    )
                    row["下游可读"] = prior.get("下游可读", row.get("下游可读", "否"))
                    row["pending"] = False
                refreshed[key] = row
                previous_state = prior.get("状态") if prior else ""
                effective_changed = (
                    prior is not None and prior_effective != row["判断有效日期"]
                )
                if previous_state != row["状态"] or effective_changed:
                    changed.append({
                        "目标表": table,
                        "业务日期": item.biz_date,
                        "原状态": previous_state or "（新增）",
                        "新状态": row["状态"],
                        "原判断有效日期": prior_effective or "—",
                        "判断有效日期": row["判断有效日期"],
                        "日历版本": row["日历版本"],
                        "判断批次": judgment_id,
                    })

        self.pending_rows = refreshed
        self._built = True
        effective_dates = self._table_effective_dates(targets)
        self.last_rebuild = {
            "judgment_id": judgment_id,
            "reason": reason,
            "at": datetime.now().isoformat(),
            "tables": targets,
            "calendar_versions": {
                table: self._active_calendar(self._rule_for(table).calendar_id).version
                for table in targets
            },
            "rule_versions": {
                table: self._rule_for(table).rule_version for table in targets
            },
            "effective_dates": effective_dates,
            "changed": changed,
            "total_rows": len(refreshed),
        }
        self.refresh_batch_readiness()
        return self.last_rebuild

    def _next_judgment_id(self) -> str:
        self._judgment_seq += 1
        return (
            f"JDG-{datetime.now().strftime('%Y%m%d')}-{self._judgment_seq:03d}"
        )

    def pending_list(
        self,
        *,
        table_name: str | None = None,
        status: str | None = None,
        conflict_only: bool = False,
    ) -> list[dict[str, Any]]:
        self.ensure_built()
        rows = list(self.pending_rows.values())
        if table_name:
            rows = [r for r in rows if r["目标表"] == table_name]
        if status:
            rows = [r for r in rows if r["状态"] == status]
        if conflict_only:
            rows = [r for r in rows if r["停产冲突"] == "是"]
        rows.sort(key=lambda r: (r["目标表"], r["业务日期"]))
        return rows

    def pending_summary(self) -> dict[str, Any]:
        rows = self.pending_list()
        by_state: dict[str, int] = {}
        for row in rows:
            by_state[row["状态"]] = by_state.get(row["状态"], 0) + 1
        return {
            "total": len(rows),
            "by_state": by_state,
            "conflicts": sum(1 for row in rows if row["停产冲突"] == "是"),
            "tables": sorted({row["目标表"] for row in rows}),
            "window": {"start": self.window_start, "end": self.window_end},
            "last_rebuild": self.last_rebuild,
        }

    # ---- 停产日冲突 ----

    def conflict_review(self, table_name: str, biz_date: str) -> dict[str, Any]:
        review = self.engine.conflict_review(
            self.assessments(table_name), biz_date
        )
        if "error" in review:
            raise ReadinessError(str(review["error"]))
        review["target_table"] = table_name
        review["pending_row"] = self.pending_rows.get(f"{table_name}#{biz_date}")
        return review

    def resolve_conflict(
        self,
        table_name: str,
        biz_date: str,
        resolution: str,
        *,
        operator: str = "数据工程组",
    ) -> dict[str, Any]:
        if resolution not in (RESOLUTION_INCLUDE, RESOLUTION_WAIT):
            raise ReadinessError(
                f"确认结论只能是「{RESOLUTION_INCLUDE}」或「{RESOLUTION_WAIT}」"
            )
        review = self.engine.conflict_review(self.assessments(table_name), biz_date)
        if "error" in review:
            raise ReadinessError(str(review["error"]))
        if not review.get("has_conflict"):
            raise ReadinessError(f"{table_name} {biz_date} 不是停产日到达冲突")
        self.resolutions[(table_name, biz_date)] = resolution
        self._sync_confirmation_store(table_name, biz_date, resolution, operator)
        rebuilt = self.rebuild_pending(
            tables=[table_name], reason=f"确认 {biz_date} 结论：{resolution}"
        )
        # 结论写入后重新评估，相邻日期才能看到是否可并入。
        updated = self.conflict_review(table_name, biz_date)
        return {
            "target_table": table_name,
            "biz_date": biz_date,
            "resolution": resolution,
            "row": self.pending_rows.get(f"{table_name}#{biz_date}"),
            "neighbour_impact": updated.get("adjacent_dates", []),
            "recommendation": updated.get("recommendation", ""),
            "rebuild": rebuilt,
        }

    def _sync_confirmation_store(
        self, table_name: str, biz_date: str, resolution: str, operator: str
    ) -> None:
        for row in store.rows("shutdown_confirmation"):
            if (
                str(row.get("目标表")) == table_name
                and str(row.get("业务日期")) == biz_date
            ):
                row["确认结论"] = resolution
                row["确认人"] = operator
                row["确认时间"] = datetime.now().isoformat()
                row["status"] = "已确认"
                row["pending"] = False
                row["abnormal"] = resolution == RESOLUTION_WAIT
                return

    # ---- 补偿批次 ----

    def plan_batches(
        self,
        *,
        tables: Iterable[str] | None = None,
        max_per_batch: int = 3,
        reason: str = "待补分区清单补偿",
    ) -> dict[str, Any]:
        """读取待补清单，为已到期且可确认的应产日期生成补偿批次。"""
        self.ensure_built()
        rows = self.pending_list()
        if tables:
            wanted = set(tables)
            rows = [row for row in rows if row["目标表"] in wanted]

        plan_rows: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for row in rows:
            if row["状态"] in ("待补", "待校验"):
                plan_rows.append(row)
            elif row["状态"] == "待确认":
                excluded.append({
                    "目标表": row["目标表"],
                    "业务日期": row["业务日期"],
                    "原因": "停产日到达冲突未确认，不进入补偿批次",
                })

        validated_by_table = {
            table: self.validated_dates(table)
            for table in {row["目标表"] for row in plan_rows}
        }

        generated = self.engine.plan_batches(
            plan_rows,
            max_per_batch=max_per_batch,
            batch_prefix="CB",
            retained_ranges=[],
        )
        now = datetime.now().isoformat()
        created: list[dict[str, Any]] = []
        for batch in generated:
            table = batch["target_table"]
            batch.update({
                "plan_reason": reason,
                "created_at": now,
                "validated_inputs": [
                    d for d in batch["input_partitions"]
                    if d in validated_by_table.get(table, [])
                ],
                "retained_ranges": self.retained_ranges.get(table, []),
            })
            self.batches[batch["batch_id"]] = batch
            created.append(batch)

        self.refresh_batch_readiness()
        return {
            "reason": reason,
            "created_batches": created,
            "excluded": excluded,
            "retained_ranges": {
                table: ranges for table, ranges in self.retained_ranges.items()
                if not tables or table in set(tables)
            },
            "pending_total": len(plan_rows),
        }

    def batch_list(self) -> list[dict[str, Any]]:
        self.ensure_built()
        self.refresh_batch_readiness()
        return sorted(
            self.batches.values(), key=lambda b: (b["target_table"], b["batch_id"])
        )

    def refresh_batch_readiness(self) -> None:
        """按最新校验结果刷新批次输入是否就绪。"""
        for batch in self.batches.values():
            table = batch["target_table"]
            validated = set(self.validated_dates(table))
            batch["validated_inputs"] = [
                d for d in batch["input_partitions"] if d in validated
            ]
            batch["input_ready"] = all(
                d in validated for d in batch["input_partitions"]
            )
            batch["missing_inputs"] = [
                d for d in batch["input_partitions"] if d not in validated
            ]
            batch["is_entry_batch"] = not batch["input_partitions"]
            if batch.get("status") not in ("已完成", "调度中"):
                batch["status"] = "可调度" if batch["input_ready"] else "等待校验"

    def schedule_batch(self, batch_id: str) -> dict[str, Any]:
        """调度补偿批次：只放行输入分区已实际校验通过的任务。"""
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ReadinessError(f"补偿批次 {batch_id} 不存在")
        self.refresh_batch_readiness()
        if not batch["input_ready"]:
            missing = [
                d for d in batch["input_partitions"]
                if d not in set(self.validated_dates(batch["target_table"]))
            ]
            raise ReadinessError(
                f"批次 {batch_id} 的输入分区尚未全部校验通过：{', '.join(missing)}"
            )
        batch["status"] = "调度中"
        batch["scheduled_at"] = datetime.now().isoformat()
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "selected_dates": batch["dates"],
            "input_partitions": batch["input_partitions"],
            "calendar_version": batch["calendar_version"],
            "rule_version": batch["rule_version"],
            "arrival_evidence": batch["arrival_evidence"],
        }

    def complete_batch(self, batch_id: str) -> dict[str, Any]:
        """完成补偿：写入的日期标记为已完成并保留在该批次。

        未经校验的分区不允许标记完成，避免未校验数据被下游读取。
        """
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ReadinessError(f"补偿批次 {batch_id} 不存在")
        validated = set(self.validated_dates(batch["target_table"]))
        unvalidated = [d for d in batch["dates"] if d not in validated]
        if unvalidated:
            raise ReadinessError(
                f"批次 {batch_id} 仍有未校验日期，不能标记完成：{', '.join(unvalidated)}"
            )
        now = datetime.now().isoformat()
        batch["status"] = "已完成"
        batch["completed_at"] = now
        for biz_date in batch["dates"]:
            key = f"{batch['target_table']}#{biz_date}"
            row = self.pending_rows.get(key)
            if row is None:
                continue
            row["状态"] = "已完成"
            row["补偿批次"] = batch_id
            row["执行日历版本"] = batch["calendar_version"]
            row["下游可读"] = "是"
            row["pending"] = False
        self.refresh_batch_readiness()
        # 记录实际跑过的日期范围，后续口径变更时原样保留。
        self.retained_ranges.setdefault(batch["target_table"], []).append({
            "task_name": batch_id,
            "start": batch["dates"][0],
            "end": batch["dates"][-1],
            "status": "已完成",
            "calendar_version": batch["calendar_version"],
            "rule_version": batch["rule_version"],
        })
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "dates": batch["dates"],
            "calendar_version": batch["calendar_version"],
            "rule_version": batch["rule_version"],
            "retained_in_batch": True,
        }

    def compensation_detail(self, batch_id: str) -> dict[str, Any]:
        """核对补偿任务：选择的日期、待确认明细和日汇总范围。"""
        batch = self.batches.get(batch_id)
        if batch is None:
            raise ReadinessError(f"补偿批次 {batch_id} 不存在")
        self.refresh_batch_readiness()
        table = batch["target_table"]
        detail_rows = []
        for biz_date in batch["dates"]:
            row = self.pending_rows.get(f"{table}#{biz_date}") or {}
            detail_rows.append({
                "业务日期": biz_date,
                "状态": row.get("状态", "—"),
                "处理动作": row.get("处理动作", "—"),
                "待补原因": row.get("待补原因", "—"),
                "到达证据": row.get("到达证据", "—"),
                "下游可读": row.get("下游可读", "否"),
            })
        conflicts = [
            row for row in self.pending_list(table_name=table, conflict_only=True)
        ]
        summary = self.daily_summary(
            table, batch["dates"][0], batch["dates"][-1]
        )
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "target_table": table,
            "pipeline": batch["pipeline"],
            "selected_dates": batch["dates"],
            "input_partitions": batch["input_partitions"],
            "input_ready": batch["input_ready"],
            "calendar_version": batch["calendar_version"],
            "rule_version": batch["rule_version"],
            "pending_detail": detail_rows,
            "confirmation_detail": conflicts,
            "daily_summary": summary,
        }

    # ---- 校验与下游可读 ----

    def record_validation(
        self,
        table_name: str,
        biz_date: str,
        *,
        status: str = "已校验",
        evidence: str = "",
        detail: str = "",
        calendar_version: str | None = None,
    ) -> dict[str, Any]:
        if status not in ("已校验", "校验失败", "待校验"):
            raise ReadinessError(f"未知校验状态 {status}")
        calendar = self._active_calendar(self._rule_for(table_name).calendar_id)
        row = self._find_validation_row(table_name, biz_date)
        values = {
            "校验状态": status,
            "校验时间": datetime.now().isoformat() if status != "待校验" else "",
            "日历版本": calendar_version or calendar.version,
            "校验证据": evidence or "—",
            "校验说明": detail,
            "status": status,
            "pending": status != "已校验",
            "abnormal": status == "校验失败",
        }
        if row is None:
            row = {
                "id": max(
                    (int(r.get("id", 0)) for r in store.rows("partition_validation")),
                    default=0,
                ) + 1,
                "目标表": table_name,
                "业务日期": biz_date,
            }
            store.rows("partition_validation").append(row)
        row.update(values)
        rebuilt = self.rebuild_pending(
            tables=[table_name], reason=f"登记 {biz_date} 校验结果：{status}"
        )
        self.refresh_batch_readiness()
        return {
            "target_table": table_name,
            "biz_date": biz_date,
            "validation_status": status,
            "calendar_version": row["日历版本"],
            "evidence": row["校验证据"],
            "pending_row": self.pending_rows.get(f"{table_name}#{biz_date}"),
            "readable_dates": self.validated_dates(table_name),
            "rebuild": rebuilt,
        }

    def _find_validation_row(
        self, table_name: str, biz_date: str
    ) -> dict[str, Any] | None:
        for row in store.rows("partition_validation"):
            if (
                str(row.get("目标表")) == table_name
                and str(row.get("业务日期")) == biz_date
            ):
                return row
        return None

    def readable_partitions(self, table_name: str | None = None) -> list[dict[str, Any]]:
        tables = [table_name] if table_name else [r.table_name for r in self.enabled_rules()]
        result: list[dict[str, Any]] = []
        for table in tables:
            result.extend(self.engine.readable_partitions(self.assessments(table)))
        return result

    # ---- 生产日历与规则变更 ----

    def correct_calendar(
        self,
        calendar_id: str,
        *,
        shutdown_dates: list[str],
        version: str | None = None,
        note: str = "",
        tables: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """更正生产日历：生成新版本、同步受影响表的判断有效日期。

        已完成的数据保留在原补偿批次，不因日历更正而回退。
        """
        base = self._active_calendar(calendar_id)
        normalized = sorted({d.strip() for d in shutdown_dates if d.strip()})
        for biz_date in normalized:
            try:
                date.fromisoformat(biz_date)
            except ValueError as exc:
                raise ReadinessError(f"停产日格式不正确：{biz_date}") from exc

        new_version = version or self._next_version(base.version)
        new_id = f"{calendar_id.rsplit('-', 1)[0]}-{new_version}"
        effective = CalendarVersion(
            calendar_id=new_id,
            name=base.name,
            version=new_version,
            effective_from=base.effective_from,
            effective_to=base.effective_to,
            shutdown_dates=frozenset(normalized),
            status="生效",
            note=note or "生产日历更正",
            created_at=datetime.now().isoformat(),
        )

        affected_tables = list(tables) if tables else [
            rule.table_name
            for rule in self.enabled_rules()
            if rule.calendar_id == calendar_id
        ]
        before_effective = self._table_effective_dates(affected_tables)

        self.calendars[new_id] = effective
        self._retire_calendar(calendar_id)
        for table in affected_tables:
            rule = self._rule_for(table)
            self.rules[table] = TableRule(
                rule_id=rule.rule_id,
                table_name=rule.table_name,
                pipeline=rule.pipeline,
                calendar_id=new_id,
                threshold_days=rule.threshold_days,
                completeness_threshold=rule.completeness_threshold,
                rule_version=rule.rule_version,
                status=rule.status,
            )
        # 新版本投入使用时，旧版本从示例数据中标注为已更正。
        self._sync_calendar_store(new_id, effective, affected_tables)

        after_effective = self._table_effective_dates(affected_tables)
        rebuilt = self.rebuild_pending(
            tables=affected_tables, reason=f"生产日历更正为 {new_version}"
        )
        return {
            "calendar_id": new_id,
            "version": new_version,
            "status": effective.status,
            "shutdown_dates": sorted(effective.shutdown_dates),
            "affected_tables": affected_tables,
            "effective_date_changes": self._diff_effective(
                before_effective, after_effective
            ),
            "retained_batches": [
                batch for batch in self.batches.values()
                if batch["status"] in ("已完成", "调度中")
                and batch["target_table"] in affected_tables
            ],
            "retained_ranges": {
                table: self.retained_ranges.get(table, [])
                for table in affected_tables
            },
            "rebuild": rebuilt,
        }

    @staticmethod
    def _next_version(version: str) -> str:
        prefix = version.rstrip("0123456789") or "R"
        digits = version[len(prefix):] or "1"
        return f"{prefix}{int(digits) + 1}"

    def _retire_calendar(self, calendar_id: str) -> None:
        for row in store.rows("production_calendar"):
            if str(row.get("日历编码")) == calendar_id and row.get("status") == "生效":
                row["status"] = "已更正"
                row["pending"] = False

    def _sync_calendar_store(
        self,
        calendar_id: str,
        calendar: CalendarVersion,
        affected_tables: list[str],
    ) -> None:
        rows = store.rows("production_calendar")
        rows.append({
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "status": "生效",
            "pending": True,
            "abnormal": False,
            "日历编码": calendar_id,
            "日历名称": calendar.name,
            "版本号": calendar.version,
            "生效区间": f"{calendar.effective_from}~{calendar.effective_to}",
            "停产日": ",".join(sorted(calendar.shutdown_dates)),
            "停产口径": calendar.note,
            "修订原因": calendar.note,
            "影响表": ",".join(affected_tables),
        })

    def _table_effective_dates(
        self, tables: Iterable[str]
    ) -> dict[str, list[str]]:
        """按当前生效日历，给出每张表的应产有效日期。"""
        result: dict[str, list[str]] = {}
        for table in tables:
            rule = self._rule_for(table)
            calendar = self._active_calendar(rule.calendar_id)
            result[table] = [
                item.biz_date
                for item in self.assessments(table)
                if item.expected
            ]
        return result

    @staticmethod
    def _diff_effective(
        before: dict[str, list[str]], after: dict[str, list[str]]
    ) -> dict[str, dict[str, list[str]]]:
        changes: dict[str, dict[str, list[str]]] = {}
        for table in after:
            old = set(before.get(table, []))
            new = set(after[table])
            if old != new:
                changes[table] = {
                    "新增有效日期": sorted(new - old),
                    "移除有效日期": sorted(old - new),
                }
        return changes

    def change_rule(
        self,
        table_name: str,
        *,
        threshold_days: int | None = None,
        completeness_threshold: float | None = None,
        rule_version: str | None = None,
        reason: str = "规则口径变更",
    ) -> dict[str, Any]:
        """变更管线阈值：重算受影响表的待补数据，保留先前跑过的日期范围。"""
        old = self._rule_for(table_name)
        new = TableRule(
            rule_id=old.rule_id,
            table_name=old.table_name,
            pipeline=old.pipeline,
            calendar_id=old.calendar_id,
            threshold_days=(
                old.threshold_days if threshold_days is None else threshold_days
            ),
            completeness_threshold=(
                old.completeness_threshold
                if completeness_threshold is None
                else completeness_threshold
            ),
            rule_version=rule_version or self._next_version(old.rule_version),
            status=old.status,
        )
        self.rules[table_name] = new
        self._sync_rule_store(new, reason)
        before = {
            row["业务日期"]: row["状态"]
            for row in self.pending_list(table_name=table_name)
        }
        rebuilt = self.rebuild_pending(tables=[table_name], reason=reason)
        after = {
            row["业务日期"]: row["状态"]
            for row in self.pending_list(table_name=table_name)
        }
        reclassified = [
            {
                "业务日期": biz_date,
                "原状态": before.get(biz_date, "（新增）"),
                "新状态": after[biz_date],
            }
            for biz_date in sorted(after)
            if before.get(biz_date) != after[biz_date]
        ]
        return {
            "target_table": table_name,
            "old_rule_version": old.rule_version,
            "new_rule_version": new.rule_version,
            "threshold_days": new.threshold_days,
            "completeness_threshold": new.completeness_threshold,
            "reclassified": reclassified,
            "retained_ranges": self.retained_ranges.get(table_name, []),
            "retained_batches": [
                batch for batch in self.batches.values()
                if batch["target_table"] == table_name
                and batch["status"] in ("已完成", "调度中")
            ],
            "rebuild": rebuilt,
        }

    def _sync_rule_store(self, rule: TableRule, reason: str) -> None:
        for row in store.rows("partition_target_rule"):
            if str(row.get("目标表")) == rule.table_name:
                row["确认阈值天数"] = str(rule.threshold_days)
                row["齐备率阈值"] = f"{rule.completeness_threshold:.2f}"
                row["规则版本"] = rule.rule_version
                row["规则说明"] = reason
                return

    # ---- 上游补交 ----

    def submit_arrival(
        self,
        table_name: str,
        biz_date: str,
        *,
        arrived_at: str,
        row_count: int,
        completeness: float,
        source_batch: str,
        evidence: str = "",
    ) -> dict[str, Any]:
        """补交一份当天到达明细，并重算该表的状态。"""
        self._rule_for(table_name)
        try:
            date.fromisoformat(biz_date)
        except ValueError as exc:
            raise ReadinessError(f"业务日期格式不正确：{biz_date}") from exc
        rows = store.rows("upstream_arrival")
        if not evidence:
            evidence = f"{source_batch}@补交登记"
        rows.append({
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "status": "已到达",
            "pending": True,
            "abnormal": completeness < self._rule_for(table_name).completeness_threshold,
            "目标表": table_name,
            "目标管线": self._rule_for(table_name).pipeline,
            "业务日期": biz_date,
            "到达时间": arrived_at,
            "到达行数": str(row_count),
            "齐备率": f"{completeness:.2f}",
            "来源批次": source_batch,
            "到达证据": evidence,
        })
        calendar = self.active_calendar_for(table_name)
        rebuilt = self.rebuild_pending(
            tables=[table_name], reason=f"补交 {biz_date} 到达明细"
        )
        assessment = next(
            (a for a in self.assessments(table_name) if a.biz_date == biz_date),
            None,
        )
        conflict = None
        if assessment and assessment.conflict:
            conflict = self.conflict_review(table_name, biz_date)
        start = self._batch_anchor_date(table_name, biz_date)
        affected_batches = [
            batch for batch in self.batch_list()
            if batch["target_table"] == table_name and biz_date in batch["dates"]
        ]
        return {
            "target_table": table_name,
            "biz_date": biz_date,
            "shutdown": calendar.is_shutdown(biz_date),
            "calendar_version": calendar.version,
            "assessment": assessment.to_dict() if assessment else None,
            "pending_row": self.pending_rows.get(f"{table_name}#{biz_date}"),
            "conflict": conflict,
            "affected_batches": affected_batches,
            "daily_summary": self.daily_summary(table_name, start, biz_date),
            "rebuild": rebuilt,
        }

    def _batch_anchor_date(self, table_name: str, biz_date: str) -> str:
        """确定日汇总的下界：优先取该表最近一个待补批次的首日。"""
        candidates = [
            batch["dates"][0]
            for batch in self.batches.values()
            if batch["target_table"] == table_name
            and batch["dates"]
            and batch["dates"][0] <= biz_date
        ]
        return max(candidates) if candidates else self.window_start

    # ---- 日汇总 ----

    def daily_summary(
        self, table_name: str, start_date: str, end_date: str
    ) -> dict[str, Any]:
        rule = self._rule_for(table_name)
        return self.engine.daily_summary(
            rule=rule,
            calendar=self._active_calendar(rule.calendar_id),
            arrivals=self.arrivals(table_name),
            start_date=start_date,
            end_date=end_date,
            validated_dates=self.validated_dates(table_name),
        )

    # ---- 总览 ----

    def snapshot(self) -> dict[str, Any]:
        self.ensure_built()
        rules = []
        for rule in self.enabled_rules():
            calendar = self._active_calendar(rule.calendar_id)
            rows = self.pending_list(table_name=rule.table_name)
            rules.append({
                "rule_id": rule.rule_id,
                "target_table": rule.table_name,
                "pipeline": rule.pipeline,
                "calendar_id": rule.calendar_id,
                "calendar_version": calendar.version,
                "threshold_days": rule.threshold_days,
                "completeness_threshold": rule.completeness_threshold,
                "rule_version": rule.rule_version,
                "pending_count": sum(
                    1 for row in rows if row["状态"] in ("待补", "待校验")
                ),
                "conflict_count": sum(
                    1 for row in rows if row["停产冲突"] == "是"
                ),
                "readable_dates": self.validated_dates(rule.table_name),
            })
        return {
            "rules": rules,
            "calendars": [
                {
                    "calendar_id": calendar.calendar_id,
                    "name": calendar.name,
                    "version": calendar.version,
                    "effective_from": calendar.effective_from,
                    "effective_to": calendar.effective_to,
                    "shutdown_dates": sorted(calendar.shutdown_dates),
                    "status": calendar.status,
                    "note": calendar.note,
                }
                for calendar in self.calendars.values()
            ],
            "pending_summary": self.pending_summary(),
            "batches": self.batch_list(),
        }


readiness_service = PartitionReadinessService()
