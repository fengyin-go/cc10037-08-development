"""分区齐备核对 API：待补清单、补偿批次、停产冲突与日历/规则变更。

这些接口承载的是按目标表判断「应产日期是否齐备」的业务链路，
不并入通用资源路由，避免和单表登记/动作混在一起。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.partition_readiness import ReadinessError, readiness_service

router = APIRouter(prefix="/api/readiness", tags=["分区齐备核对"])


def _fail(exc: ReadinessError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/overview")
def overview() -> dict[str, Any]:
    """应产规则、日历、待补汇总和补偿批次的整体视图。"""
    return readiness_service.snapshot()


@router.get("/pending")
def pending_partitions(
    table: str | None = Query(default=None),
    status: str | None = Query(default=None),
    conflict_only: bool = Query(default=False),
) -> dict[str, Any]:
    """待补分区清单：按表、状态或停产冲突筛选。"""
    rows = readiness_service.pending_list(
        table_name=table, status=status, conflict_only=conflict_only
    )
    return {
        "items": rows,
        "total": len(rows),
        "summary": readiness_service.pending_summary(),
    }


@router.post("/pending/rebuild")
def rebuild_pending(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """按最新日历和规则口径重算待补清单。"""
    body = body or {}
    tables = body.get("tables") or None
    try:
        return readiness_service.rebuild_pending(
            tables=tables, reason=body.get("reason", "手工重算待补清单")
        )
    except ReadinessError as exc:
        raise _fail(exc)


@router.get("/conflict")
def conflict_review(
    table: str = Query(...),
    biz_date: str = Query(...),
) -> dict[str, Any]:
    """停产日与实到明细冲突的复核视图，含前后相邻日期。"""
    try:
        return readiness_service.conflict_review(table, biz_date)
    except ReadinessError as exc:
        raise _fail(exc)


@router.post("/conflict/resolve")
def resolve_conflict(body: dict[str, Any]) -> dict[str, Any]:
    """确认停产日额外到达是否纳入当期，或继续等待停产口径修订。"""
    try:
        return readiness_service.resolve_conflict(
            table_name=body.get("table", ""),
            biz_date=body.get("biz_date", ""),
            resolution=body.get("resolution", ""),
            operator=body.get("operator", "数据工程组"),
        )
    except ReadinessError as exc:
        raise _fail(exc)


@router.post("/batches/plan")
def plan_batches(body: dict[str, Any] | None = None) -> dict[str, Any]:
    """读取待补清单，为可确认的应产日期生成补偿批次。"""
    body = body or {}
    try:
        return readiness_service.plan_batches(
            tables=body.get("tables") or None,
            max_per_batch=int(body.get("max_per_batch", 3)),
            reason=body.get("reason", "待补分区清单补偿"),
        )
    except ReadinessError as exc:
        raise _fail(exc)


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    batches = readiness_service.batch_list()
    return {"items": batches, "total": len(batches)}


@router.post("/batches/{batch_id}/schedule")
def schedule_batch(batch_id: str) -> dict[str, Any]:
    """调度补偿批次；输入分区必须全部实际校验通过。"""
    try:
        return readiness_service.schedule_batch(batch_id)
    except ReadinessError as exc:
        raise _fail(exc)


@router.post("/batches/{batch_id}/complete")
def complete_batch(batch_id: str) -> dict[str, Any]:
    """完成补偿，写入日期保留在该批次。"""
    try:
        return readiness_service.complete_batch(batch_id)
    except ReadinessError as exc:
        raise _fail(exc)


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str) -> dict[str, Any]:
    """核对补偿任务：选择的日期、待确认明细和日汇总范围。"""
    try:
        return readiness_service.compensation_detail(batch_id)
    except ReadinessError as exc:
        raise _fail(exc)


@router.get("/readable")
def readable_partitions(
    table: str | None = Query(default=None),
) -> dict[str, Any]:
    """下游可读取的日分区：只包含校验通过的日期。"""
    items = readiness_service.readable_partitions(table)
    return {"items": items, "total": len(items)}


@router.post("/validations")
def record_validation(body: dict[str, Any]) -> dict[str, Any]:
    """登记日分区校验结果，并联动刷新待补清单和批次就绪状态。"""
    try:
        return readiness_service.record_validation(
            table_name=body.get("table", ""),
            biz_date=body.get("biz_date", ""),
            status=body.get("status", "已校验"),
            evidence=body.get("evidence", ""),
            detail=body.get("detail", ""),
            calendar_version=body.get("calendar_version"),
        )
    except ReadinessError as exc:
        raise _fail(exc)


@router.post("/arrivals")
def submit_arrival(body: dict[str, Any]) -> dict[str, Any]:
    """补交一份当天到达明细，并返回冲突判断与日汇总核对。"""
    try:
        return readiness_service.submit_arrival(
            table_name=body.get("table", ""),
            biz_date=body.get("biz_date", ""),
            arrived_at=body.get("arrived_at", ""),
            row_count=int(body.get("row_count", 0)),
            completeness=float(body.get("completeness", 0)),
            source_batch=body.get("source_batch", ""),
            evidence=body.get("evidence", ""),
        )
    except ReadinessError as exc:
        raise _fail(exc)


@router.post("/calendar/correct")
def correct_calendar(body: dict[str, Any]) -> dict[str, Any]:
    """更正生产日历并重算受影响的待补数据。"""
    try:
        return readiness_service.correct_calendar(
            calendar_id=body.get("calendar_id", ""),
            shutdown_dates=body.get("shutdown_dates", []),
            version=body.get("version"),
            note=body.get("note", ""),
            tables=body.get("tables") or None,
        )
    except ReadinessError as exc:
        raise _fail(exc)


@router.post("/rules/change")
def change_rule(body: dict[str, Any]) -> dict[str, Any]:
    """变更管线阈值，重算受影响表的待补数据并保留已跑日期范围。"""
    try:
        return readiness_service.change_rule(
            table_name=body.get("table", ""),
            threshold_days=(
                int(body["threshold_days"])
                if body.get("threshold_days") is not None else None
            ),
            completeness_threshold=(
                float(body["completeness_threshold"])
                if body.get("completeness_threshold") is not None else None
            ),
            rule_version=body.get("rule_version"),
            reason=body.get("reason", "规则口径变更"),
        )
    except ReadinessError as exc:
        raise _fail(exc)
