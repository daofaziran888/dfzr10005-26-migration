"""服务层测试：迁移后已生效规则的行为不能改变，生命周期/回收站读取兼容。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["RETN_AUTO_BOOTSTRAP"] = "0"

from app import retention_bootstrap  # noqa: E402
from app.retention_bootstrap import bootstrap, reset_context  # noqa: E402
from app.services.lifecycle import LifecycleService  # noqa: E402
from app.services.recycle import RecycleService  # noqa: E402
from app.services.retention import RetentionService  # noqa: E402


class ServiceBehaviorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reset_context()
        # 内存 SQLite 完成装配 + 迁移，进程内隔离，不落盘。
        cls.ctx = bootstrap(db_path=":memory:", run_migration=True, batch_size=3)
        retention_bootstrap._context = cls.ctx

    @classmethod
    def tearDownClass(cls) -> None:
        reset_context()

    def test_retention_list_uses_typed_model(self) -> None:
        service = RetentionService()
        items, total = service.list_entries(page=1, size=50)
        self.assertEqual(total, 8)
        rule_2 = next(row for row in items if row["规则编号"] == "RETN-0002")
        self.assertEqual(rule_2["规范保留天数"], 30)
        # 旧口径列仍然存在，兼容既有页面
        self.assertIn("保留期限", rule_2)
        self.assertIn("过期动作", rule_2)

    def test_active_rule_transitions_unchanged(self) -> None:
        """迁移前 RETN-0002 为生效中；废止/归档等动作语义与迁移前保持一致。"""
        service = RetentionService()
        items, _ = service.list_entries(page=1, size=50)
        rule_2 = next(row for row in items if row["规则编号"] == "RETN-0002")
        self.assertEqual(rule_2["规则状态"], "生效中")

        updated, message = service.run_action(rule_2["id"], "废止规则")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["规则状态"], "生效中")  # 废止动作目标状态沿用旧映射
        self.assertIn("废止", message)

        bad, msg = service.run_action(rule_2["id"], "不存在的动作")
        self.assertIsNone(bad)
        self.assertIn("不属于", msg)

    def test_create_entry_rejects_unparseable(self) -> None:
        service = RetentionService()
        entry, problems = service.create_entry({
            "规则编号": "RETN-NEW", "数据集范围": "tmp.x",
            "保留期限": "soon", "过期动作": "归档", "合规依据": "《x》v1.0",
        })
        self.assertIsNone(entry)
        self.assertTrue(problems)  # 不静默丢弃，明确返回原因

    def test_lifecycle_enriched_with_normalized_days(self) -> None:
        service = LifecycleService()
        items, total = service.list_entries(page=1, size=20)
        self.assertEqual(total, 3)
        life_2 = next(row for row in items if row["策略编号"] == "LIFE-0002")
        self.assertEqual(life_2["规范保留天数"], 30)  # 与 RETN-0002 对齐
        # 原字段保持不变
        self.assertEqual(life_2["保留天数"], "表生命周期管理样例2")

    def test_recycle_list_carries_rule_status_and_window(self) -> None:
        service = RecycleService()
        items, total = service.list_entries(page=1, size=20)
        self.assertEqual(total, 3)
        recy_2 = next(row for row in items if row["记录编号"] == "RECY-0002")
        self.assertEqual(recy_2["规范可恢复期天数"], 30)
        self.assertEqual(recy_2["关联规则编号"], "RETN-0002")
        self.assertEqual(recy_2["规则状态"], "生效中")  # 当前状态落到回收记录清单
        # 原可恢复期字段保持不变
        self.assertEqual(recy_2["可恢复期"], "数据回收站样例2")

    def test_lifecycle_actions_still_work(self) -> None:
        service = LifecycleService()
        entry, message = service.run_action(1, "启用策略")
        self.assertIsNotNone(entry)
        self.assertIn("启用", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
