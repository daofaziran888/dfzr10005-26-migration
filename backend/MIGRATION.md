# 存量保留规则 → 强类型保留模型 迁移说明

## 背景与目标

历史保留规则把「保留期限 / 过期动作 / 合规依据」存成任意字符串，同一含义同时
存在 `30天` 与 `1个月`，生命周期执行时无法可靠比较；合规依据也没有独立版本。

本次迁移把存量规则规范化为强类型模型，并满足以下硬约束：

- **强类型**：期限落到整数 `retention_days`（+ 单位枚举）、动作落到枚举
  `DELETE/ARCHIVE/ANONYMIZE`、依据拆出独立 `compliance_version`。
- **原值可追溯**：每条规则的完整原始行写入 `raw_snapshot_json`，不覆盖、不删除。
- **不静默丢弃**：无法识别的值进入待处理结果（`migration_ledger.result=PENDING`），
  携带字段级原因与原值快照，由人工复核。
- **幂等**：`retention_rule.rule_no` 与 `migration_ledger.rule_no` 均为唯一约束；
  重复执行迁移只计数 `skipped`，不生成重复规则。
- **并发安全**：`migration_lock` 单例行锁 + `BEGIN IMMEDIATE`。两个批次同时写
  同一规则时，后到批次收到 `MigrationLocked` 或全部命中跳过，只允许首次成功的
  迁移结果继续。
- **行为不变**：已生效规则的启用/废止/归档状态流转语义与迁移前完全一致；
  生命周期清理、回收站读取的既有字段保持不变，仅追加规范化列。

## 存储与表结构

选用 **SQLite（标准库 sqlite3，无新增依赖）**，本地默认路径
`backend/data/retention.db`（可用环境变量 `RETN_DB_PATH` 覆盖，测试用 `:memory:`）。

| 表 | 作用 |
| --- | --- |
| `retention_rule` | 强类型规则主表，`rule_no` 唯一，含原值快照与迁移状态 |
| `migration_ledger` | 每条规则一条迁移台账（规则编号唯一），记录结果/原因/批次/原值 |
| `migration_batch` | 分批回填的批次记录与计数 |
| `migration_lock` | 单例行锁，保证批次互斥 |
| `dataset_link` | 强类型规则与既有生命周期/回收站记录的关联 |

建表为幂等 DDL（`CREATE TABLE IF NOT EXISTS`），可重复演练。

## 规范化口径

- 期限：`天/日=1`、`周=7`、`个月/月=30`、`季/季度=90`、`年=365`。
  因此 **`30天` 与 `1个月` 都规范化为 `retention_days=30`**；纯数字按天。
- 动作：删除/物理删除/delete/purge → `DELETE`；归档/archive → `ARCHIVE`；
  脱敏/匿名化/anonymize/mask → `ANONYMIZE`。词表外值 → PENDING。
- 依据：显式版本（`…v2.1`、`…@1.0`）解析版本；无版本回填默认 `1.0` 并标记
  `compliance_version_inferred=1`。依据为空 → PENDING。
- 一条记录多个字段异常时，原因全部累计，一次迁移暴露全部问题。

## 上线步骤（分批回填）

```bash
# 0. 隔离环境：依赖安装（仅标准库，requirements 无变化）
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. 隔离演练：临时库、小批次、自动幂等校验，不触碰本地/线上库
make migrate-rehearse

# 2. 数据库结构演练（幂等建表）
make migrate-init

# 3. 分批回填上线（默认每批 500，可调）
make migrate-run BATCH_SIZE=500

# 4. 核对结果与待处理清单
make migrate-status
make migrate-pending
```

服务启动时会自动执行一次幂等迁移（`RETN_AUTO_BOOTSTRAP=1`，默认开启），
因此重启不会丢数据，也不会产生重复规则；存储不可用时自动回退内存仓库，
接口不中断（迁移状态接口返回 `available=false` 与原因）。

## 回退方式

```bash
# 回退全部迁移结果（只删强类型规则与台账，原始样例不动，可重新迁移）
make migrate-rollback
# 或按批次回退
.venv/bin/python -m scripts.migrate_retention rollback --batch-id BATCH-xxxx
# 也可调用接口 POST /api/retention/migration/rollback
```

回退删除的是迁移引擎写入、且在台账中有记录的规则；手工登记且未迁移的规则保留。
由于原值从未被覆盖，回退后可随时重新执行迁移。

## 关键样例核对

**RETN-0002**（`backend/app/legacy_data.py`）：

| 维度 | 原值 | 规范化结果 |
| --- | --- | --- |
| 数据集范围 | dwd.order_detail 订单明细域（含退款/发票子表） | 原样保留 |
| 保留期限 | `1个月` | `retention_days=30`，展示 `1个月` |
| 过期动作 | `归档` | `ARCHIVE` |
| 例外清单 | 税务发票表;风控冻结订单 | 拆分为数组，展示顿号连接 |
| 合规依据 | 《会计档案管理办法》v2.0 | ref=《会计档案管理办法》，version=`2.0` |
| 执行周期 | 每周 | 原样保留 |
| 状态 | 生效中 | `生效中`（流转行为不变） |

**跨模块交叉核对**（`dataset_link` 关联，测试 `test_cross_module_life_0002_and_recy_0002`）：

- `LIFE-0002` 生命周期结果新增 `规范保留天数 = 30`（原 `保留天数` 字段不变）。
- `RECY-0002` 回收记录新增 `规范可恢复期天数 = 30`、`关联规则编号=RETN-0002`、
  `规则状态=生效中`（当前状态落到回收记录清单，原 `可恢复期` 字段不变）。

## 测试

```bash
make test
```

- `backend/tests/test_retention_migration.py`：规范化、原值保留、PENDING、
  RETN-0002 全字段、幂等、并发只允许首次成功、跨模块核对、回退。
- `backend/tests/test_service_behavior.py`：强类型读路径、动作行为不变、
  无法识别值拒收、生命周期/回收站兼容。
