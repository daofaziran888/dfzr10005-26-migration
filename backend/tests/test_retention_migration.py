"""强类型保留模型与迁移引擎测试（仅依赖标准库 unittest，隔离内存库）。

覆盖：
- 规范化引擎：30天 vs 1个月 等价、单位换算、动作别名、依据版本、待处理原因
- 迁移：原值保留、PENDING 不丢失、RETN-0002 全字段核对
- 幂等：重复迁移不产生重复规则
- 并发：两批次同时写同一规则，只有首次成功
- 交叉核对：LIFE-0002 保留天数 / RECY-0002 可恢复期对齐
- 行为不变：已生效规则状态流转动作与迁移前一致
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 关掉自动引导，测试全部手动装配 :memory: 库。
os.environ["RETN_AUTO_BOOTSTRAP"] = "0"

from app.legacy_data import DATASET_LINKS, LEGACY_RETENTION_ROWS  # noqa: E402
from app.retention_db import connect, init_schema  # noqa: E402
from app.retention_migration import (  # noqa: E402
    MigrationLocked,
    RetentionMigration,
)
from app.retention_model import (  # noqa: E402
    DurationUnit,
    ExpireAction,
    ParseError,
    RuleStatus,
    normalize_legacy,
    parse_action,
    parse_compliance,
    parse_duration,
)
from app.retention_repo import RetentionRepository  # noqa: E402


class NormalizerTest(unittest.TestCase):
    def test_duration_30_days_equals_1_month(self) -> None:
        days = parse_duration("30天")
        month = parse_duration("1个月")
        self.assertEqual(days.days, 30)
        self.assertEqual(month.days, 30)
        self.assertEqual(days.days, month.days)  # 历史两种写法现在可可靠比较

    def test_duration_units(self) -> None:
        self.assertEqual(parse_duration("2年").days, 730)
        self.assertEqual(parse_duration("12个月").days, 360)
        self.assertEqual(parse_duration("12 周").days, 84)
        self.assertEqual(parse_duration("45 天").unit, DurationUnit.DAY)
        self.assertEqual(parse_duration("90").days, 90)  # 纯数字按天
        self.assertEqual(parse_duration("1季度").days, 90)

    def test_duration_unparseable(self) -> None:
        with self.assertRaises(ParseError):
            parse_duration("until further notice")
        with self.assertRaises(ParseError):
            parse_duration("")
        with self.assertRaises(ParseError):
            parse_duration(None)

    def test_action_aliases(self) -> None:
        self.assertEqual(parse_action("归档"), ExpireAction.ARCHIVE)
        self.assertEqual(parse_action("ARCHIVE"), ExpireAction.ARCHIVE)
        self.assertEqual(parse_action("物理删除"), ExpireAction.DELETE)
        self.assertEqual(parse_action("匿名化"), ExpireAction.ANONYMIZE)
        with self.assertRaises(ParseError):
            parse_action("移至冷备")

    def test_compliance_version(self) -> None:
        explicit = parse_compliance("《会计档案管理办法》v2.0")
        self.assertEqual(explicit.version, "2.0")
        self.assertFalse(explicit.version_inferred)
        gdpr = parse_compliance("GDPR-2016/679@1.0")
        self.assertEqual(gdpr.version, "1.0")
        inferred = parse_compliance("内部线索管理制度")
        self.assertEqual(inferred.version, "1.0")
        self.assertTrue(inferred.version_inferred)
        with self.assertRaises(ParseError):
            parse_compliance("")

    def test_normalize_collects_all_problems(self) -> None:
        bad = {
            "规则编号": "X-1", "数据集范围": "s", "保留期限": "bad",
            "过期动作": "未知", "合规依据": "", "status": "草稿",
        }
        result = normalize_legacy(bad)
        self.assertTrue(result.is_pending)
        self.assertEqual(len(result.pending_reasons), 3)  # 三个坏字段全部暴露
        self.assertEqual(result.raw_snapshot["保留期限"], "bad")  # 原值保留


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        init_schema(self.conn)
        self.repo = RetentionRepository(self.conn)
        self.repo.seed_links(DATASET_LINKS)
        self.migration = RetentionMigration(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_full_migration_counts_and_pending(self) -> None:
        report = self.migration.run_batch(LEGACY_RETENTION_ROWS, batch_size=3)
        self.assertEqual(report.total_seen, len(LEGACY_RETENTION_ROWS))
        # RETN-0005/0006/0007 各有一个无法识别字段 -> 3 条 PENDING，其余 5 条 MIGRATED
        self.assertEqual(report.migrated_count, 5)
        self.assertEqual(report.pending_count, 3)
        pending_nos = {item["规则编号"] for item in self.migration.pending_entries()}
        self.assertEqual(pending_nos, {"RETN-0005", "RETN-0006", "RETN-0007"})

    def test_retn_0002_full_fields(self) -> None:
        """题目指定样例：数据集范围/期限/动作/例外/依据/执行周期逐项核对。"""
        self.migration.run_batch(LEGACY_RETENTION_ROWS)
        rule = self.repo.get_by_rule_no("RETN-0002")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["数据集范围"], "dwd.order_detail 订单明细域（含退款/发票子表）")
        self.assertEqual(rule["规范保留天数"], 30)          # “1个月” -> 30 天
        self.assertEqual(rule["规范期限"], "1个月")
        self.assertEqual(rule["过期动作枚举"], "ARCHIVE")
        self.assertEqual(rule["规范过期动作"], "归档")
        self.assertIn("税务发票表", rule["例外清单"])
        self.assertIn("风控冻结订单", rule["例外清单"])
        self.assertEqual(rule["依据版本"], "2.0")
        self.assertEqual(rule["合规依据"], "《会计档案管理办法》")
        self.assertEqual(rule["执行周期"], "每周")
        self.assertEqual(rule["规则状态"], "生效中")
        self.assertEqual(rule["迁移状态"], "MIGRATED")
        # 原值完整可追溯
        self.assertEqual(rule["原值快照"]["保留期限"], "1个月")
        self.assertEqual(rule["原值快照"]["过期动作"], "归档")

    def test_raw_snapshot_preserved_for_pending(self) -> None:
        self.migration.run_batch(LEGACY_RETENTION_ROWS)
        rule = self.repo.get_by_rule_no("RETN-0005")
        self.assertEqual(rule["迁移状态"], "PENDING")
        self.assertEqual(rule["原值快照"]["保留期限"], "until further notice")
        # 同一条里可识别的字段仍被规范化
        self.assertEqual(rule["过期动作枚举"], "DELETE")
        # 待处理清单带原因
        pending = self.migration.pending_entries()
        entry = next(item for item in pending if item["规则编号"] == "RETN-0005")
        self.assertTrue(any("保留期限" in reason for reason in entry["待处理原因"]))

    def test_idempotent_rerun_no_duplicates(self) -> None:
        first = self.migration.run_batch(LEGACY_RETENTION_ROWS, batch_size=4)
        second = self.migration.run_batch(LEGACY_RETENTION_ROWS, batch_size=4)
        self.assertEqual(first.migrated_count + first.pending_count, len(LEGACY_RETENTION_ROWS))
        self.assertEqual(second.skipped_count, len(LEGACY_RETENTION_ROWS))
        rule_count = self.conn.execute("SELECT COUNT(*) AS c FROM retention_rule").fetchone()["c"]
        self.assertEqual(rule_count, len(LEGACY_RETENTION_ROWS))  # 没有重复规则
        ledger_count = self.conn.execute("SELECT COUNT(*) AS c FROM migration_ledger").fetchone()["c"]
        self.assertEqual(ledger_count, len(LEGACY_RETENTION_ROWS))  # 没有重复台账

    def test_concurrent_batches_only_first_wins(self) -> None:
        """两个批次（独立连接）同时写同一规则：只允许首次成功的结果继续。

        场景一（确定性）：A 已持锁开跑，B 立刻尝试 -> 必须收到 MigrationLocked，
        不能偷偷产生第二份结果。
        场景二：A 完成后 B 再跑 -> 命中唯一约束全部 SKIPPED，规则仍只有一份。
        """
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="retn-concurrent-")
        db_path = str(Path(tmp_dir) / "concurrent.db")
        conn_a = connect(db_path)
        conn_b = connect(db_path)
        conn_a.execute("PRAGMA busy_timeout = 800")
        conn_b.execute("PRAGMA busy_timeout = 800")
        init_schema(conn_a)
        RetentionRepository(conn_a).seed_links(DATASET_LINKS)
        RetentionRepository(conn_b).seed_links(DATASET_LINKS)
        migration_a = RetentionMigration(conn_a)
        migration_b = RetentionMigration(conn_b)

        # 场景一：A 持锁进行中，B 被拒绝。
        conn_a.execute("BEGIN IMMEDIATE")
        conn_a.execute("INSERT INTO migration_lock (id, holder) VALUES (1, 'BATCH-A-FIRST')")
        with self.assertRaises(MigrationLocked):
            migration_b.run_batch(LEGACY_RETENTION_ROWS, batch_size=100)
        # B 被拒期间不得写入任何规则
        self.assertEqual(
            conn_b.execute("SELECT COUNT(*) AS c FROM retention_rule").fetchone()["c"], 0
        )
        conn_a.execute("DELETE FROM migration_lock WHERE id = 1")
        conn_a.execute("COMMIT")

        # 场景二：A 正常完成首次迁移。
        report_a = migration_a.run_batch(LEGACY_RETENTION_ROWS, batch_size=100)
        self.assertEqual(
            report_a.migrated_count + report_a.pending_count, len(LEGACY_RETENTION_ROWS)
        )
        # B 随后执行（相当于两个批次最终都尝试写同一规则）-> 全部跳过，不重复。
        report_b = migration_b.run_batch(LEGACY_RETENTION_ROWS, batch_size=100)
        self.assertEqual(report_b.skipped_count, len(LEGACY_RETENTION_ROWS))

        rule_count = conn_a.execute("SELECT COUNT(*) AS c FROM retention_rule").fetchone()["c"]
        ledger_count = conn_a.execute("SELECT COUNT(*) AS c FROM migration_ledger").fetchone()["c"]
        self.assertEqual(rule_count, len(LEGACY_RETENTION_ROWS))
        self.assertEqual(ledger_count, len(LEGACY_RETENTION_ROWS))
        self.assertEqual(
            conn_a.execute("SELECT COUNT(*) AS c FROM migration_lock").fetchone()["c"], 0
        )
        conn_a.close(); conn_b.close()

    def test_cross_module_life_0002_and_recy_0002(self) -> None:
        """LIFE-0002 保留天数 与 RECY-0002 可恢复期都对齐 RETN-0002 的 30 天。"""
        self.migration.run_batch(LEGACY_RETENTION_ROWS)
        life_days = self.repo.link_lifecycle_days(2)   # LIFE-0002 的内存 id
        self.assertEqual(life_days, 30)
        recycle_link = self.repo.link_recycle_days(2)  # RECY-0002 的内存 id
        self.assertEqual(recycle_link["retention_days"], 30)
        self.assertEqual(recycle_link["rule_no"], "RETN-0002")
        self.assertEqual(recycle_link["rule_status"], RuleStatus.ACTIVE.value)

    def test_rollback_restores_clean_state(self) -> None:
        self.migration.run_batch(LEGACY_RETENTION_ROWS, batch_size=3)
        result = self.migration.rollback()
        self.assertEqual(result["rules_removed"], len(LEGACY_RETENTION_ROWS))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM retention_rule").fetchone()["c"], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM migration_ledger").fetchone()["c"], 0
        )
        # 回退后可重新迁移
        again = self.migration.run_batch(LEGACY_RETENTION_ROWS)
        self.assertEqual(again.migrated_count, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
