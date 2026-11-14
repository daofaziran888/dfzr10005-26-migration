"""数据保留策略接口：维护保留规则，覆盖启用规则、废止规则、归档规则等动作。

迁移相关接口（/migration/*）只做运维编排，业务判断仍在 service 层：
- GET  /migration/status   迁移汇总（成功/待处理/锁/批次）
- GET  /migration/pending  待处理结果清单（无法识别的值，不静默丢弃）
- GET  /migration/batches  分批回填记录
- POST /migration/rollback 回退（可带 batch_id）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.retention import RetentionService

router = APIRouter(prefix="/api/retention", tags=["数据保留策略"])

service = RetentionService()

# 强类型列在前，旧口径列（保留期限/过期动作）保留以兼容既有导出与页面。
LIST_FIELDS = [
    "规则编号", "数据集范围", "规范保留天数", "规范期限", "规范过期动作",
    "例外清单", "合规依据", "依据版本", "执行周期", "迁移状态", "规则状态",
    "保留期限", "过期动作",
]
STATUSES = ["草稿", "生效中", "已到期", "已废止"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、生效中、已到期、已废止"),
    migration_status: str | None = Query(default=None, description="MIGRATED / PENDING"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤数据保留策略列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, page=page, size=size
    )
    if migration_status:
        items = [row for row in items if row.get("迁移状态") == migration_status]
        total = len(items)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/migration/status")
def migration_status() -> dict[str, Any]:
    """迁移汇总：规范化成功数、待处理数、当前锁持有者与批次数。"""
    return service.migration_summary()


@router.get("/migration/pending")
def migration_pending() -> dict[str, Any]:
    """待处理清单：每条含原值快照与无法规范化的具体原因。"""
    items = service.pending_entries()
    return {"total": len(items), "items": items}


@router.get("/migration/batches")
def migration_batches() -> dict[str, Any]:
    """分批回填执行记录。"""
    items = service.migration_batches()
    return {"total": len(items), "items": items}


@router.post("/migration/rollback", response_model=ActionResult)
def migration_rollback(payload: EntryPayload) -> ActionResult:
    """按批次回退；不传 batch_id 时回退全部迁移结果（原值快照始终保留）。"""
    from app.retention_bootstrap import get_retention_context

    ctx = get_retention_context()
    if not ctx.available or ctx.migration is None:
        return ActionResult(ok=False, message="强类型存储不可用，无需回退")
    batch_id = payload.values.get("batch_id")
    result = ctx.migration.rollback(batch_id=batch_id)
    return ActionResult(ok=True, message="迁移已回退", entry=result)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条保留规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"保留规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条保留规则，缺字段或无法规范化时说明原因而不是静默丢弃。"""
    entry, problems = service.create_entry(payload.values)
    if problems:
        return ActionResult(ok=False, message=f"登记失败：{'；'.join(map(str, problems))}")
    return ActionResult(ok=True, message="保留规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条保留规则执行启用规则、废止规则、归档规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据保留策略清单：返回当前过滤条件下的全量数据（含原值快照）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "retention", "total": total, "items": items}
