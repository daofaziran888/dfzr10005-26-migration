"""接口出入参模型：列表分页、动作结果与各模块的明细结构。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    """登记或修改一条业务记录时提交的字段集合。"""

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class TypedRetentionRule(BaseModel):
    """强类型保留规则：期限、动作、依据版本均为可比较/可枚举的强类型字段。"""

    id: int | None = None
    rule_no: str = Field(..., description="规则编号，唯一")
    dataset_scope: str = Field(..., description="数据集范围")
    retention_days: int | None = Field(None, description="规范化后的整数保留天数")
    duration_value: int | None = None
    duration_unit: str | None = Field(None, description="DAY/WEEK/MONTH/QUARTER/YEAR")
    expire_action: str | None = Field(None, description="DELETE/ARCHIVE/ANONYMIZE")
    exceptions: list[str] = Field(default_factory=list)
    compliance_ref: str | None = None
    compliance_version: str | None = Field(None, description="合规依据独立版本号")
    compliance_version_inferred: bool = False
    execution_cycle: str | None = None
    status: str
    migration_status: str = Field(..., description="MIGRATED/PENDING/FAILED")
    raw_snapshot: dict[str, Any] = Field(default_factory=dict, description="原值快照，用于追溯")


class MigrationSummary(BaseModel):
    """迁移结果汇总。"""

    migrated: int = 0
    pending: int = 0
    failed: int = 0
    lock_holder: str | None = None
    batches: int = 0



class LifecycleEntry(BaseModel):
    """生命周期策略明细结构。"""

    field_0: str | None = None  # 策略编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 创建日期
    field_3: str | None = None  # 保留天数
    field_4: str | None = None  # 归档路径
    field_5: str | None = None  # 清理方式
    field_6: str | None = None  # 最后操作
    field_7: str | None = None  # 策略状态

class CostEntry(BaseModel):
    """成本分析报告明细结构。"""

    field_0: str | None = None  # 报告编号
    field_1: str | None = None  # 分析范围
    field_2: str | None = None  # 存储总量
    field_3: str | None = None  # 冷数据占比
    field_4: str | None = None  # 优化建议
    field_5: str | None = None  # 预计节省
    field_6: str | None = None  # 执行状态
    field_7: str | None = None  # 生成时间

class HeatmapEntry(BaseModel):
    """热度记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 查询次数
    field_3: str | None = None  # 最近查询
    field_4: str | None = None  # 热度等级
    field_5: str | None = None  # 访问来源
    field_6: str | None = None  # 分区热度
    field_7: str | None = None  # 采样时间

class DagEntry(BaseModel):
    """依赖图节点明细结构。"""

    field_0: str | None = None  # 节点编号
    field_1: str | None = None  # 节点名称
    field_2: str | None = None  # 上游节点
    field_3: str | None = None  # 下游节点
    field_4: str | None = None  # 执行条件
    field_5: str | None = None  # 超时分钟
    field_6: str | None = None  # 失败策略
    field_7: str | None = None  # 节点状态

class SlaEntry(BaseModel):
    """SLA规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 监控对象
    field_2: str | None = None  # 产出时间
    field_3: str | None = None  # 延迟阈值
    field_4: str | None = None  # 告警级别
    field_5: str | None = None  # 通知渠道
    field_6: str | None = None  # 升级策略
    field_7: str | None = None  # 规则状态

class ResourceEntry(BaseModel):
    """资源池配置明细结构。"""

    field_0: str | None = None  # 配置编号
    field_1: str | None = None  # 资源类型
    field_2: str | None = None  # 最小配额
    field_3: str | None = None  # 最大配额
    field_4: str | None = None  # 弹性策略
    field_5: str | None = None  # 扩缩容阈值
    field_6: str | None = None  # 冷却时间
    field_7: str | None = None  # 配置状态

class ScoreEntry(BaseModel):
    """健康度报告明细结构。"""

    field_0: str | None = None  # 报告编号
    field_1: str | None = None  # 评估范围
    field_2: str | None = None  # 存储健康分
    field_3: str | None = None  # 计算健康分
    field_4: str | None = None  # 质量健康分
    field_5: str | None = None  # 综合评分
    field_6: str | None = None  # 改进建议
    field_7: str | None = None  # 评估时间

class DriftEntry(BaseModel):
    """漂移记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 漂移字段
    field_3: str | None = None  # 历史均值
    field_4: str | None = None  # 当前数值
    field_5: str | None = None  # 偏差比例
    field_6: str | None = None  # 漂移方向
    field_7: str | None = None  # 检测时间

class CatalogEntry(BaseModel):
    """资产记录明细结构。"""

    field_0: str | None = None  # 资产编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 资产分类
    field_3: str | None = None  # 业务域
    field_4: str | None = None  # 负责人
    field_5: str | None = None  # 数据等级
    field_6: str | None = None  # 更新频率
    field_7: str | None = None  # 盘点时间

class RetentionEntry(BaseModel):
    """保留规则明细结构。"""

    field_0: str | None = None  # 规则编号
    field_1: str | None = None  # 数据集范围
    field_2: str | None = None  # 保留期限
    field_3: str | None = None  # 过期动作
    field_4: str | None = None  # 例外清单
    field_5: str | None = None  # 合规依据
    field_6: str | None = None  # 执行周期
    field_7: str | None = None  # 规则状态

class DedupEntry(BaseModel):
    """去重任务明细结构。"""

    field_0: str | None = None  # 任务编号
    field_1: str | None = None  # 目标表名
    field_2: str | None = None  # 去重字段
    field_3: str | None = None  # 去重算法
    field_4: str | None = None  # 保留策略
    field_5: str | None = None  # 影响行数
    field_6: str | None = None  # 执行批次
    field_7: str | None = None  # 任务状态

class TierEntry(BaseModel):
    """分层定义明细结构。"""

    field_0: str | None = None  # 分层编号
    field_1: str | None = None  # 分层名称
    field_2: str | None = None  # 层级别名
    field_3: str | None = None  # 数据粒度
    field_4: str | None = None  # 存储格式
    field_5: str | None = None  # 压缩策略
    field_6: str | None = None  # 访问模式
    field_7: str | None = None  # 分层状态

class BaselineEntry(BaseModel):
    """基线配置明细结构。"""

    field_0: str | None = None  # 基线编号
    field_1: str | None = None  # 基线名称
    field_2: str | None = None  # 承诺时间
    field_3: str | None = None  # 预警时间
    field_4: str | None = None  # 关键路径
    field_5: str | None = None  # 依赖范围
    field_6: str | None = None  # 监控状态
    field_7: str | None = None  # 最近产出

class RecycleEntry(BaseModel):
    """回收记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 表名称
    field_2: str | None = None  # 删除时间
    field_3: str | None = None  # 回收原因
    field_4: str | None = None  # 数据大小
    field_5: str | None = None  # 可恢复期
    field_6: str | None = None  # 恢复状态
    field_7: str | None = None  # 清理时间

class AuditEntry(BaseModel):
    """变更记录明细结构。"""

    field_0: str | None = None  # 记录编号
    field_1: str | None = None  # 变更对象
    field_2: str | None = None  # 变更类型
    field_3: str | None = None  # 变更前值
    field_4: str | None = None  # 变更后值
    field_5: str | None = None  # 操作人
    field_6: str | None = None  # 操作时间
    field_7: str | None = None  # 复核状态

class BenchmarkEntry(BaseModel):
    """性能报告明细结构。"""

    field_0: str | None = None  # 报告编号
    field_1: str | None = None  # 查询语句
    field_2: str | None = None  # 执行耗时
    field_3: str | None = None  # 扫描行数
    field_4: str | None = None  # 索引命中
    field_5: str | None = None  # 资源消耗
    field_6: str | None = None  # 优化建议
    field_7: str | None = None  # 分析时间
