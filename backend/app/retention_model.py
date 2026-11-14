"""强类型保留模型的领域定义与规范化引擎。

历史规则把「保留期限 / 过期动作 / 合规依据」都存成任意字符串（同时存在
“30天”和“1个月”），生命周期执行时无法可靠比较。这里统一把它们解析成：

- 期限：retention_days（整数天数，可直接比较）+ duration_unit（单位枚举）+ duration_value
- 动作：expire_action（DELETE / ARCHIVE / ANONYMIZE 枚举）
- 依据：compliance_ref（依据标识）+ compliance_version（独立版本号）

无法识别的值不会被静默丢弃：parse_* 返回 ParseError，由迁移引擎写入
migration_ledger 的 PENDING 结果并保留原值快照。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DurationUnit(str, Enum):
    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"
    QUARTER = "QUARTER"
    YEAR = "YEAR"


class ExpireAction(str, Enum):
    DELETE = "DELETE"          # 删除
    ARCHIVE = "ARCHIVE"        # 归档
    ANONYMIZE = "ANONYMIZE"    # 脱敏 / 匿名化


class MigrationStatus(str, Enum):
    MIGRATED = "MIGRATED"      # 规范化成功并已回填
    PENDING = "PENDING"        # 存在无法识别的值，等待人工处理
    FAILED = "FAILED"          # 写入阶段失败（可重试）


class RuleStatus(str, Enum):
    DRAFT = "草稿"
    ACTIVE = "生效中"
    EXPIRED = "已到期"
    REVOKED = "已废止"


# 中文单位 -> 枚举与换算天数。月按 30 天、季按 90 天、年按 365 天归一，
# 这样“30天”和“1个月”得到同一个 retention_days，生命周期可直接比较。
_UNIT_SPECS: dict[str, tuple[DurationUnit, int]] = {
    "天": (DurationUnit.DAY, 1),
    "日": (DurationUnit.DAY, 1),
    "周": (DurationUnit.WEEK, 7),
    "个月": (DurationUnit.MONTH, 30),
    "月": (DurationUnit.MONTH, 30),
    "季": (DurationUnit.QUARTER, 90),
    "季度": (DurationUnit.QUARTER, 90),
    "年": (DurationUnit.YEAR, 365),
}

_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>天|日|周|个月|月|季度|季|年)\s*$"
)
# “30 天”这种带空格的、以及纯数字默认按天处理。
_PLAIN_NUMBER_RE = re.compile(r"^\s*(?P<value>\d+)\s*$")

# 过期动作别名 -> 枚举。匹配时去掉空白并忽略大小写。
_ACTION_ALIASES: dict[str, ExpireAction] = {
    "删除": ExpireAction.DELETE,
    "删除数据": ExpireAction.DELETE,
    "物理删除": ExpireAction.DELETE,
    "delete": ExpireAction.DELETE,
    "purge": ExpireAction.DELETE,
    "归档": ExpireAction.ARCHIVE,
    "归档存储": ExpireAction.ARCHIVE,
    "冷归档": ExpireAction.ARCHIVE,
    "archive": ExpireAction.ARCHIVE,
    "脱敏": ExpireAction.ANONYMIZE,
    "匿名化": ExpireAction.ANONYMIZE,
    "匿名": ExpireAction.ANONYMIZE,
    "anonymize": ExpireAction.ANONYMIZE,
    "mask": ExpireAction.ANONYMIZE,
}

# 合规依据里夹带版本时的识别，例如 《个保法》v2.1 / GDPR-2016/679@1.0
_VERSION_RE = re.compile(r"[vV@](?P<version>\d+(?:\.\d+){0,2})\s*$")
_DEFAULT_COMPLIANCE_VERSION = "1.0"


class ParseError(ValueError):
    """规范化失败：携带字段名与原值，便于写入待处理台账。"""

    def __init__(self, field_name: str, raw: Any, reason: str) -> None:
        self.field_name = field_name
        self.raw = raw
        self.reason = reason
        super().__init__(f"字段「{field_name}」值 {raw!r} 无法规范化：{reason}")


@dataclass(frozen=True)
class NormalizedDuration:
    value: int
    unit: DurationUnit
    days: int

    def display(self) -> str:
        unit_label = {
            DurationUnit.DAY: "天",
            DurationUnit.WEEK: "周",
            DurationUnit.MONTH: "个月",
            DurationUnit.QUARTER: "季度",
            DurationUnit.YEAR: "年",
        }[self.unit]
        return f"{self.value}{unit_label}"


@dataclass(frozen=True)
class NormalizedCompliance:
    ref: str
    version: str
    version_inferred: bool


def parse_duration(raw: Any) -> NormalizedDuration:
    """把任意字符串期限解析成可比较的整数天数。

    支持 “30天 / 1个月 / 12 周 / 2年 / 90”（纯数字按天）。
    无法识别（空值、英文自然语言、乱码样例）时抛 ParseError。
    """
    if raw is None:
        raise ParseError("保留期限", raw, "值为空")
    text = str(raw).strip()
    if not text:
        raise ParseError("保留期限", raw, "值为空")

    match = _DURATION_RE.match(text)
    if match:
        number = float(match.group("value"))
        unit, factor = _UNIT_SPECS[match.group("unit")]
        if number != int(number):
            raise ParseError("保留期限", raw, f"小数期限 {number} 无法落到整天")
        value = int(number)
        return NormalizedDuration(value=value, unit=unit, days=value * factor)

    plain = _PLAIN_NUMBER_RE.match(text)
    if plain:
        value = int(plain.group("value"))
        return NormalizedDuration(value=value, unit=DurationUnit.DAY, days=value)

    raise ParseError("保留期限", raw, "不符合「数值+单位（天/周/个月/季/年）」格式")


def parse_action(raw: Any) -> ExpireAction:
    """把任意字符串过期动作解析成枚举；无法识别时抛 ParseError。"""
    if raw is None:
        raise ParseError("过期动作", raw, "值为空")
    key = str(raw).strip().lower().replace(" ", "")
    if not key:
        raise ParseError("过期动作", raw, "值为空")
    # 中文 key 不受 lower/去空格影响；英文别名统一小写。
    for candidate in (str(raw).strip(), key):
        if candidate in _ACTION_ALIASES:
            return _ACTION_ALIASES[candidate]
    raise ParseError("过期动作", raw, "不在 删除/归档/脱敏（DELETE/ARCHIVE/ANONYMIZE）词表内")


def parse_compliance(raw: Any) -> NormalizedCompliance:
    """拆出合规依据标识与独立版本号。

    依据字符串里显式带版本（…v2.1、…@1.0）时解析版本；否则回填默认 1.0
    并标记 version_inferred=True，原值仍完整保留在 raw_snapshot 里。
    """
    if raw is None or not str(raw).strip():
        raise ParseError("合规依据", raw, "值为空")
    text = str(raw).strip()
    match = _VERSION_RE.search(text)
    if match:
        version = match.group("version")
        ref = text[: match.start()].rstrip(" -@vV") or text
        return NormalizedCompliance(ref=ref, version=version, version_inferred=False)
    return NormalizedCompliance(
        ref=text, version=_DEFAULT_COMPLIANCE_VERSION, version_inferred=True
    )


@dataclass
class NormalizedRule:
    """规范化结果：强类型字段 + 原值快照 + 待处理原因。"""

    rule_no: str
    dataset_scope: str
    retention_days: int | None = None
    duration_value: int | None = None
    duration_unit: DurationUnit | None = None
    expire_action: ExpireAction | None = None
    exceptions: list[str] = field(default_factory=list)
    compliance_ref: str | None = None
    compliance_version: str | None = None
    compliance_version_inferred: bool = False
    execution_cycle: str | None = None
    status: RuleStatus = RuleStatus.DRAFT
    raw_snapshot: dict[str, Any] = field(default_factory=dict)
    pending_reasons: list[str] = field(default_factory=list)

    @property
    def is_pending(self) -> bool:
        return bool(self.pending_reasons)


def split_exceptions(raw: Any) -> list[str]:
    """例外清单历史上可能是逗号/顿号/分号分隔的字符串，统一拆成数组。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text:
        return []
    parts = re.split(r"[,，;；、]\s*", text)
    return [part.strip() for part in parts if part.strip()]


def normalize_legacy(legacy: dict[str, Any]) -> NormalizedRule:
    """对一条存量规则做完整规范化；任何字段失败都累计进 pending_reasons，
    而不是提前抛出，保证一次迁移能暴露一条记录的全部问题。"""
    rule_no = str(legacy.get("规则编号") or "").strip()
    result = NormalizedRule(
        rule_no=rule_no,
        dataset_scope=str(legacy.get("数据集范围") or "").strip(),
        exceptions=split_exceptions(legacy.get("例外清单")),
        execution_cycle=(str(legacy.get("执行周期")).strip() if legacy.get("执行周期") else None),
        status=RuleStatus(legacy.get("status") or RuleStatus.DRAFT.value),
        raw_snapshot={key: legacy.get(key) for key in (
            "规则编号", "数据集范围", "保留期限", "过期动作", "例外清单",
            "合规依据", "执行周期", "status",
        )},
    )

    try:
        duration = parse_duration(legacy.get("保留期限"))
        result.retention_days = duration.days
        result.duration_value = duration.value
        result.duration_unit = duration.unit
    except ParseError as exc:
        result.pending_reasons.append(str(exc))

    try:
        result.expire_action = parse_action(legacy.get("过期动作"))
    except ParseError as exc:
        result.pending_reasons.append(str(exc))

    try:
        compliance = parse_compliance(legacy.get("合规依据"))
        result.compliance_ref = compliance.ref
        result.compliance_version = compliance.version
        result.compliance_version_inferred = compliance.version_inferred
    except ParseError as exc:
        result.pending_reasons.append(str(exc))

    if not rule_no:
        result.pending_reasons.append(ParseError("规则编号", rule_no, "值为空").__str__())
    return result
