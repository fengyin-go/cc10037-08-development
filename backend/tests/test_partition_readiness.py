"""分区齐备核对域的自检测试。

不引入 pytest 依赖，直接 python -m unittest 运行：
    cd backend && .venv/bin/python -m unittest tests.test_partition_readiness -v
"""

from __future__ import annotations

import unittest
from datetime import date

from app.engine.partition_readiness import (
    ArrivalRecord,
    CalendarVersion,
    PartitionReadinessEngine,
    ReadinessState,
    TableRule,
    ValidationRecord,
)
from app.services import partition_readiness as service_module
from app.services.partition_readiness import PartitionReadinessService
from app.store import store


TODAY = date(2026, 11, 21)
CALENDAR = CalendarVersion(
    calendar_id="CAL-T",
    name="测试日历",
    version="R1",
    effective_from="2026-11-01",
    effective_to="2026-11-30",
    shutdown_dates=frozenset({"2026-11-01", "2026-11-08", "2026-11-15"}),
)
RULE = TableRule(
    rule_id="t1",
    table_name="dw.t",
    pipeline="测试管线",
    calendar_id="CAL-T",
    threshold_days=1,
    completeness_threshold=99.0,
    rule_version="R1",
)


def arrival(
    biz_date: str,
    *,
    rows: int = 1000,
    completeness: float = 99.9,
    arrived_at: str = "2026-11-10T01:00:00",
    evidence: str = "src/_SUCCESS",
) -> ArrivalRecord:
    return ArrivalRecord(
        arrival_id=f"A-{biz_date}-{arrived_at}",
        table_name="dw.t",
        pipeline="测试管线",
        biz_date=biz_date,
        arrived_at=arrived_at,
        row_count=rows,
        completeness=completeness,
        source_batch="B1",
        evidence=evidence,
    )


def validation(status: str, evidence: str = "DQ-1") -> ValidationRecord:
    return ValidationRecord(
        table_name="dw.t",
        biz_date="",
        status=status,
        checked_at="2026-11-11T00:00:00",
        calendar_version="R1",
        evidence=evidence,
    )


class EngineStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PartitionReadinessEngine()

    def assess(self, arrivals, validations, start="2026-11-09", end="2026-11-18"):
        return {
            item.biz_date: item
            for item in self.engine.evaluate_table(
                RULE, CALENDAR, arrivals, validations,
                start_date=start, end_date=end, today=TODAY,
            )
        }

    def test_shutdown_day_without_arrival_is_not_expected(self) -> None:
        result = self.assess([], [])["2026-11-15"]
        self.assertEqual(result.state, ReadinessState.SHUTDOWN)
        self.assertFalse(result.expected)
        self.assertFalse(result.pending)

    def test_missing_arrival_after_threshold_is_pending(self) -> None:
        result = self.assess([], [])["2026-11-12"]
        self.assertEqual(result.state, ReadinessState.MISSING)
        self.assertTrue(result.pending)
        self.assertTrue(result.compensable)
        self.assertFalse(result.readable)

    def test_not_due_date_is_not_pending(self) -> None:
        # 2026-11-21 的确认时点是 T+1=11-22，评估日当天还未到期。
        result = self.assess([], [], start="2026-11-21", end="2026-11-21")["2026-11-21"]
        self.assertEqual(result.state, ReadinessState.NOT_DUE)
        self.assertFalse(result.pending)
        self.assertFalse(result.compensable)

    def test_below_threshold_is_pending(self) -> None:
        result = self.assess(
            [arrival("2026-11-12", completeness=97.5)],
            [validation("已校验")],
        )["2026-11-12"]
        self.assertEqual(result.state, ReadinessState.UNDER_THRESHOLD)
        self.assertTrue(result.pending)

    def test_arrival_without_validation_is_not_readable(self) -> None:
        result = self.assess([arrival("2026-11-12")], [])["2026-11-12"]
        self.assertEqual(result.state, ReadinessState.UNVALIDATED)
        self.assertFalse(result.readable)
        self.assertTrue(result.pending)

    def test_failed_validation_is_pending_not_readable(self) -> None:
        failed = validation("校验失败")
        failed = ValidationRecord(
            table_name="dw.t", biz_date="2026-11-12", status="校验失败",
            checked_at=failed.checked_at, calendar_version="R1",
            evidence="DQ-fail", detail="空值超阈值",
        )
        result = self.assess([arrival("2026-11-12")], [failed])["2026-11-12"]
        self.assertEqual(result.state, ReadinessState.VALIDATION_FAILED)
        self.assertFalse(result.readable)
        self.assertTrue(result.compensable)

    def test_validated_partition_is_readable(self) -> None:
        passed = ValidationRecord(
            table_name="dw.t", biz_date="2026-11-12", status="已校验",
            checked_at="2026-11-13T00:00:00", calendar_version="R1",
            evidence="DQ-pass",
        )
        result = self.assess([arrival("2026-11-12")], [passed])["2026-11-12"]
        self.assertEqual(result.state, ReadinessState.CONFIRMABLE)
        self.assertTrue(result.readable)
        self.assertFalse(result.pending)

    def test_shutdown_day_with_arrival_is_conflict(self) -> None:
        result = self.assess([arrival("2026-11-15", rows=420)], [])["2026-11-15"]
        self.assertEqual(result.state, ReadinessState.SHUTDOWN_CONFLICT)
        self.assertTrue(result.conflict)
        self.assertFalse(result.includable)
        self.assertFalse(result.readable)

    def test_latest_arrival_wins(self) -> None:
        result = self.assess(
            [
                arrival("2026-11-12", completeness=80.0, arrived_at="2026-11-13T01:00:00"),
                arrival("2026-11-12", completeness=99.95, arrived_at="2026-11-13T08:00:00"),
            ],
            [],
        )["2026-11-12"]
        self.assertEqual(result.completeness, 99.95)

    def test_included_shutdown_day_needs_validation(self) -> None:
        assessments = self.engine.evaluate_table(
            RULE, CALENDAR, [arrival("2026-11-15")], [],
            start_date="2026-11-15", end_date="2026-11-15", today=TODAY,
            resolutions={"2026-11-15": "纳入当期"},
        )
        self.assertEqual(assessments[0].state, ReadinessState.INCLUDED_PENDING)
        self.assertTrue(assessments[0].includable)
        self.assertFalse(assessments[0].readable)

    def test_conflict_review_covers_adjacent_dates(self) -> None:
        assessments = self.engine.evaluate_table(
            RULE, CALENDAR,
            [
                arrival("2026-11-14", completeness=99.9),
                arrival("2026-11-15", rows=420, completeness=13.2),
                arrival("2026-11-16", completeness=99.9),
            ],
            [
                ValidationRecord("dw.t", "2026-11-14", "已校验", "", "R1", "dq"),
                ValidationRecord("dw.t", "2026-11-16", "已校验", "", "R1", "dq"),
            ],
            start_date="2026-11-13", end_date="2026-11-17", today=TODAY,
        )
        review = self.engine.conflict_review(assessments, "2026-11-15")
        self.assertTrue(review["has_conflict"])
        self.assertEqual(
            [item["biz_date"] for item in review["adjacent_dates"]],
            ["2026-11-14", "2026-11-15", "2026-11-16"],
        )
        # 停产日未确认，相邻日期不得并入。
        self.assertIn("不得引用该日数据", review["recommendation"])

    def test_plan_batches_chains_validated_inputs(self) -> None:
        rows = [
            {"目标表": "dw.t", "管线": "p", "业务日期": d, "状态": "待补",
             "处理动作": "重跑补数", "到达证据": "—", "待补原因": "缺数",
             "日历版本": "R1", "规则版本": "R1"}
            for d in [
                "2026-11-02", "2026-11-03", "2026-11-04",
                "2026-11-05", "2026-11-06", "2026-11-07",
            ]
        ]
        batches = self.engine.plan_batches(
            rows, max_per_batch=3, validated_dates=["2026-11-02", "2026-11-03"],
        )
        self.assertEqual(len(batches), 2)
        self.assertTrue(batches[0]["input_ready"])
        self.assertEqual(batches[1]["input_partitions"], batches[0]["dates"])
        self.assertFalse(batches[1]["input_ready"])
        self.assertEqual(batches[1]["status"], "等待校验")

    def test_daily_summary_counts_rows(self) -> None:
        summary = self.engine.daily_summary(
            RULE, CALENDAR,
            [arrival("2026-11-14", rows=100), arrival("2026-11-15", rows=40)],
            start_date="2026-11-13", end_date="2026-11-15",
            validated_dates=["2026-11-14"],
        )
        self.assertEqual(summary["day_count"], 3)
        self.assertEqual(summary["total_rows"], 140)
        self.assertEqual(summary["arrived_days"], 2)
        self.assertEqual(summary["validated_days"], 1)


class ServiceFlowTests(unittest.TestCase):
    """用真实的示例数据跑一遍完整链路。"""

    def setUp(self) -> None:
        self._snapshot = {
            name: [dict(row) for row in rows]
            for name, rows in store._tables.items()
        }
        self.service = PartitionReadinessService()

    def tearDown(self) -> None:
        store._tables = self._snapshot
        service_module.readiness_service = PartitionReadinessService()

    def test_pending_list_covers_expected_states(self) -> None:
        rows = {row["业务日期"]: row for row in self.service.pending_list(table_name="dw.dwd_order_wide")}
        self.assertEqual(rows["2026-11-13"]["状态"], "已完成")
        self.assertEqual(rows["2026-11-15"]["状态"], "待确认")
        self.assertEqual(rows["2026-11-17"]["状态"], "待补")
        self.assertNotIn("2026-11-01", rows)  # 停产且无到达，不进入清单

    def test_batch_requires_validated_inputs(self) -> None:
        created = self.service.plan_batches(tables=["dw.dwd_order_wide"], max_per_batch=3)
        self.assertGreaterEqual(len(created["created_batches"]), 2)
        first, second = created["created_batches"][:2]
        self.service.schedule_batch(first["batch_id"])
        with self.assertRaises(ValueError):
            self.service.schedule_batch(second["batch_id"])
        for biz_date in first["dates"]:
            self.service.record_validation(
                "dw.dwd_order_wide", biz_date, status="已校验", evidence="DQ-test",
            )
        scheduled = self.service.schedule_batch(second["batch_id"])
        self.assertEqual(scheduled["input_partitions"], first["dates"])

    def test_completed_batch_keeps_dates_after_calendar_correction(self) -> None:
        created = self.service.plan_batches(tables=["dw.dwd_order_wide"], max_per_batch=3)
        first = created["created_batches"][0]
        self.service.schedule_batch(first["batch_id"])
        for biz_date in first["dates"]:
            self.service.record_validation(
                "dw.dwd_order_wide", biz_date, status="已校验", evidence="DQ-test",
            )
        self.service.complete_batch(first["batch_id"])
        corrected = self.service.correct_calendar(
            "CAL-2026-11-R1",
            shutdown_dates=[
                "2026-11-01", "2026-11-08", "2026-11-15",
                "2026-11-16", "2026-11-22", "2026-11-29",
            ],
            version="R2",
            note="11-16 检修停产",
        )
        self.assertEqual(corrected["version"], "R2")
        retained = {batch["batch_id"] for batch in corrected["retained_batches"]}
        self.assertIn(first["batch_id"], retained)
        for biz_date in first["dates"]:
            row = self.service.pending_rows[f"dw.dwd_order_wide#{biz_date}"]
            self.assertEqual(row["状态"], "已完成")
            self.assertEqual(row["补偿批次"], first["batch_id"])
            self.assertEqual(row["执行日历版本"], "R1")
        self.assertIn(
            "2026-11-16",
            corrected["effective_date_changes"]["dw.dwd_order_wide"]["移除有效日期"],
        )

    def test_rule_change_recomputes_and_retains_ranges(self) -> None:
        result = self.service.change_rule(
            "ads.ads_sales_dashboard",
            threshold_days=2,
            completeness_threshold=96.0,
            reason="阈值放宽",
        )
        self.assertEqual(result["old_rule_version"], "R1")
        self.assertEqual(result["new_rule_version"], "R2")
        self.assertTrue(result["retained_ranges"])
        # 11-21 在 T+2 下尚未到期，重算后不应再出现在待补清单里。
        rows = {row["业务日期"] for row in self.service.pending_list(table_name="ads.ads_sales_dashboard")}
        self.assertNotIn("2026-11-21", rows)

    def test_shutdown_arrival_submission_reports_conflict_and_summary(self) -> None:
        result = self.service.submit_arrival(
            "dw.dwd_order_wide",
            "2026-11-15",
            arrived_at="2026-11-21T10:00:00",
            row_count=3180000,
            completeness=99.71,
            source_batch="ODS-20261121-01",
            evidence="ods.ods_order_info/_SUCCESS@2026-11-21T10:00",
        )
        self.assertTrue(result["shutdown"])
        self.assertEqual(result["assessment"]["state"], "停产日冲突")
        self.assertTrue(result["conflict"]["has_conflict"])
        summary = result["daily_summary"]
        self.assertEqual(summary["scope_end"], "2026-11-15")
        self.assertLessEqual(summary["scope_start"], "2026-11-15")

        resolved = self.service.resolve_conflict(
            "dw.dwd_order_wide", "2026-11-15", "纳入当期",
        )
        self.assertEqual(resolved["row"]["状态"], "待校验")
        self.assertEqual(resolved["row"]["可纳入当期"], "是")
        self.assertEqual(resolved["row"]["下游可读"], "否")
        self.assertTrue(
            any("计入当期" in item["input_effect"] for item in resolved["neighbour_impact"])
        )

    def test_unvalidated_partitions_never_readable(self) -> None:
        readable = {
            (item["table_name"], item["biz_date"])
            for item in self.service.readable_partitions()
        }
        self.assertIn(("dw.dwd_order_wide", "2026-11-13"), readable)
        self.assertNotIn(("dw.dwd_order_wide", "2026-11-17"), readable)  # 校验失败
        self.assertNotIn(("dw.dwd_order_wide", "2026-11-19"), readable)  # 缺数
        self.assertNotIn(("ads.ads_sales_dashboard", "2026-11-18"), readable)  # 待校验

    def test_compensation_detail_selects_dates_and_summary(self) -> None:
        created = self.service.plan_batches(tables=["dw.dwd_order_wide"], max_per_batch=3)
        batch = created["created_batches"][0]
        detail = self.service.compensation_detail(batch["batch_id"])
        self.assertEqual(detail["selected_dates"], batch["dates"])
        self.assertEqual(len(detail["pending_detail"]), len(batch["dates"]))
        self.assertEqual(detail["daily_summary"]["scope_start"], batch["dates"][0])
        self.assertEqual(detail["daily_summary"]["scope_end"], batch["dates"][-1])
        # 停产冲突不进入补偿任务，但核对视图里仍能看到待确认明细。
        self.assertTrue(
            any(row["业务日期"] == "2026-11-15" for row in detail["confirmation_detail"])
        )


if __name__ == "__main__":
    unittest.main()
