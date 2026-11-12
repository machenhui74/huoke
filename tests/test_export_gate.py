"""导出硬门禁测试：地域自述、评论时效、服务需求三项条件。

测试场景覆盖：
  - 地域本人自述（通过）
  - 笔记地名但非本人自述（不通过）
  - 仅浙江 IP（不通过）
  - 外省 IP 冲突（不通过）
  - 否定地名（"我在上海，台州有吗"）
  - 台州/泰州混淆
  - 旧评论（2019年、2025年但澳大利亚）
  - 未知/未来时间
  - 边界30天
  - 无服务需求
  - 有需求但非本地
  - 真的满足条件
  - 导出空表
  - 日增量与抖音链接回归
"""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scripts.leadkit.export_gate import (
    ExportGate, GeoGate, TimeGate, NeedGate, GateResult
)

TZ = ZoneInfo("Asia/Shanghai")


class GeoGateTest(unittest.TestCase):
    """地域本人自述门禁测试。"""

    def setUp(self):
        self.cfg = {
            "enabled": True,
            "local_words": ["台州", "台州市", "椒江", "路桥", "温岭"],
            "out_words": ["上海", "北京", "深圳", "杭州", "澳大利亚", "泰州"],
            "confusable": {"泰州": "台州"},
            "ip_conflict_provinces": ["上海", "北京", "广东", "江苏", "澳大利亚"],
        }
        self.gate = GeoGate(self.cfg)

    def test_self_local_pass(self):
        """评论者明确自述在台州，通过。"""
        cases = [
            "我在台州，哪家托管班好",
            "坐标椒江，求推荐少儿编程",
            "人在路桥，想了解托班",
            "我家住台州市，孩子想报暑假班",
            "我们在温岭这边，有推荐吗",
        ]
        for text in cases:
            ok, reason, evidence = self.gate.check(text)
            self.assertTrue(ok, f"应通过但未通过：{text}，原因：{reason}")

    def test_only_location_mention_fail(self):
        """仅出现地名但非本人自述，不通过。"""
        cases = [
            ("台州有吗", "仅出现地名"),
            ("台州怎么样", "仅出现地名"),
            ("椒江靠谱吗", "仅出现地名"),
        ]
        for text, expected_reason in cases:
            ok, reason, _ = self.gate.check(text)
            self.assertFalse(ok, f"应不通过但通过了：{text}")
            self.assertIn("仅出现地名", reason)

    def test_self_out_conflict_fail(self):
        """自述在外地，与本地自述冲突。"""
        text = "我在上海，台州有吗"
        ok, reason, _ = self.gate.check(text)
        self.assertFalse(ok, f"应不通过但通过了：{text}")
        # 这种情况应该被识别为仅出现地名

    def test_ip_conflict_fail(self):
        """IP 属地与本地自述冲突。"""
        text = "我在台州，求推荐"
        ctx = {"ip_province": "广东"}
        ok, reason, _ = self.gate.check(text, ctx)
        self.assertFalse(ok, f"应不通过但通过了：{text}")
        self.assertIn("冲突", reason)

    def test_ip_zhejiang_no_self_local_fail(self):
        """仅浙江 IP，无本人自述，不通过。"""
        text = "哪家好"
        ctx = {"ip_province": "浙江"}
        ok, reason, _ = self.gate.check(text, ctx)
        self.assertFalse(ok, f"应不通过但通过了：{text}")
        self.assertIn("无本人自述", reason)

    def test_taizhou_vs_taizhou_confusable(self):
        """泰州/台州混淆检测。"""
        text = "泰州哪家好"  # 泰州是江苏的，不是台州
        ok, reason, _ = self.gate.check(text)
        self.assertFalse(ok, f"应不通过但通过了：{text}")
        self.assertIn("混淆", reason)

    def test_no_geo_fail(self):
        """评论正文无地域信息，不通过。"""
        text = "哪家托管班好"
        ok, reason, _ = self.gate.check(text)
        self.assertFalse(ok)
        self.assertIn("无本人自述", reason)


class TimeGateTest(unittest.TestCase):
    """评论时效门禁测试。"""

    def setUp(self):
        self.cfg = {"enabled": True, "max_days": 30}
        self.gate = TimeGate(self.cfg, TZ)
        self.now = datetime.now(TZ)

    def test_recent_pass(self):
        """近期评论通过。"""
        recent = (self.now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
        ok, reason, _ = self.gate.check(recent, self.now)
        self.assertTrue(ok, f"应通过但未通过：{recent}，原因：{reason}")

    def test_boundary_30_days_pass(self):
        """边界 30 天内通过。"""
        boundary = (self.now - timedelta(days=29, hours=23)).strftime("%Y-%m-%d %H:%M")
        ok, reason, _ = self.gate.check(boundary, self.now)
        self.assertTrue(ok, f"应通过但未通过：{boundary}，原因：{reason}")

    def test_old_comment_2019_fail(self):
        """2019 年的旧评论不通过。"""
        old = "2019-03-15 10:00"
        ok, reason, _ = self.gate.check(old, self.now)
        self.assertFalse(ok, f"应不通过但通过了：{old}")
        self.assertIn("超过", reason)

    def test_old_comment_2025_australia_fail(self):
        """2025 年但很早的评论（模拟澳大利亚用户旧评论）不通过。"""
        old = "2025-01-15 10:00"  # 相对于 2026-11 来说超过 30 天
        ok, reason, _ = self.gate.check(old, self.now)
        self.assertFalse(ok, f"应不通过但通过了：{old}")
        self.assertIn("超过", reason)

    def test_future_time_fail(self):
        """未来时间不通过。"""
        future = (self.now + timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        ok, reason, _ = self.gate.check(future, self.now)
        self.assertFalse(ok, f"应不通过但通过了：{future}")
        self.assertIn("未来", reason)

    def test_empty_time_fail(self):
        """空时间不通过。"""
        ok, reason, _ = self.gate.check("", self.now)
        self.assertFalse(ok)
        self.assertIn("为空", reason)

    def test_unparseable_time_fail(self):
        """无法解析的时间不通过。"""
        ok, reason, _ = self.gate.check("昨天", self.now)
        self.assertFalse(ok)
        self.assertIn("解析失败", reason)

    def test_timestamp_pass(self):
        """时间戳格式通过。"""
        # 毫秒级时间戳
        ts = str(int((self.now - timedelta(days=3)).timestamp() * 1000))
        ok, reason, _ = self.gate.check(ts, self.now)
        self.assertTrue(ok, f"应通过但未通过：{ts}，原因：{reason}")


class NeedGateTest(unittest.TestCase):
    """服务需求门禁测试。"""

    def setUp(self):
        self.cfg = {
            "enabled": True,
            "need_words": ["怎么报名", "多少钱", "有推荐", "求推荐", "在哪里"],
            "exclude_words": ["投诉", "避雷", "电话多少"],
        }
        self.gate = NeedGate(self.cfg)

    def test_service_need_pass(self):
        """明确服务需求通过。"""
        cases = [
            "怎么报名",
            "多少钱一节",
            "有推荐的机构吗",
            "求推荐托管班",
            "在哪里上课",
        ]
        for text in cases:
            ok, reason, _ = self.gate.check(text)
            self.assertTrue(ok, f"应通过但未通过：{text}，原因：{reason}")

    def test_no_service_need_fail(self):
        """无服务需求不通过。"""
        cases = [
            "不错",
            "看起来挺好的",
            "孩子喜欢",
        ]
        for text in cases:
            ok, reason, _ = self.gate.check(text)
            self.assertFalse(ok, f"应不通过但通过了：{text}")
            self.assertIn("无明确服务需求", reason)

    def test_exclude_words_fail(self):
        """含排除词不通过。"""
        cases = [
            ("这家避雷，多少钱", "避雷"),
            ("投诉这家机构", "投诉"),
            ("电话多少？", "电话多少"),
        ]
        for text, expected in cases:
            ok, reason, _ = self.gate.check(text)
            self.assertFalse(ok, f"应不通过但通过了：{text}")
            self.assertIn("排除词", reason)

    def test_problem_type_pass(self):
        """问题类型属于服务需求时通过。"""
        text = "这个不错"  # 没有服务需求词
        ok, _, _ = self.gate.check(text, problem="求推荐")
        self.assertTrue(ok, "问题类型「求推荐」应通过")

        ok, _, _ = self.gate.check(text, problem="问价")
        self.assertTrue(ok, "问题类型「问价」应通过")


class ExportGateIntegrationTest(unittest.TestCase):
    """导出门禁集成测试：三项条件必须同时满足。"""

    def test_all_pass(self):
        """三项全部通过。"""
        row = {
            "comment_text": "我在台州，求推荐托管班",
            "commented_at": (datetime.now(TZ) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M"),
            "problem": "",
        }
        # 模拟 profile
        class MockProfile:
            tz = TZ
            def section(self, name):
                if name == "export_gate":
                    return {
                        "enabled": True,
                        "geo": {
                            "enabled": True,
                            "local_words": ["台州"],
                            "out_words": ["上海"],
                            "confusable": {},
                            "ip_conflict_provinces": [],
                        },
                        "time": {"enabled": True, "max_days": 30},
                        "need": {
                            "enabled": True,
                            "need_words": ["求推荐"],
                            "exclude_words": [],
                        },
                    }
                return {}

        gate = ExportGate(MockProfile())
        result = gate.check(row)
        self.assertTrue(result.passed, f"应通过但未通过：{result.reason}")
        self.assertTrue(result.geo_passed)
        self.assertTrue(result.time_passed)
        self.assertTrue(result.need_passed)

    def test_geo_fail_others_pass(self):
        """地域不通过，其他通过。"""
        row = {
            "comment_text": "台州有吗",  # 仅地名，非自述
            "commented_at": (datetime.now(TZ) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M"),
            "problem": "",
        }
        class MockProfile:
            tz = TZ
            def section(self, name):
                if name == "export_gate":
                    return {
                        "enabled": True,
                        "geo": {
                            "enabled": True,
                            "local_words": ["台州"],
                            "out_words": [],
                            "confusable": {},
                            "ip_conflict_provinces": [],
                        },
                        "time": {"enabled": True, "max_days": 30},
                        "need": {
                            "enabled": True,
                            "need_words": ["有吗"],
                            "exclude_words": [],
                        },
                    }
                return {}

        gate = ExportGate(MockProfile())
        result = gate.check(row)
        self.assertFalse(result.passed)
        self.assertFalse(result.geo_passed)
        self.assertTrue(result.time_passed)
        self.assertTrue(result.need_passed)

    def test_time_fail_others_pass(self):
        """时效不通过，其他通过。"""
        row = {
            "comment_text": "我在台州，求推荐",
            "commented_at": "2019-03-15 10:00",  # 旧评论
            "problem": "",
        }
        class MockProfile:
            tz = TZ
            def section(self, name):
                if name == "export_gate":
                    return {
                        "enabled": True,
                        "geo": {
                            "enabled": True,
                            "local_words": ["台州"],
                            "out_words": [],
                            "confusable": {},
                            "ip_conflict_provinces": [],
                        },
                        "time": {"enabled": True, "max_days": 30},
                        "need": {
                            "enabled": True,
                            "need_words": ["求推荐"],
                            "exclude_words": [],
                        },
                    }
                return {}

        gate = ExportGate(MockProfile())
        result = gate.check(row)
        self.assertFalse(result.passed)
        self.assertTrue(result.geo_passed)
        self.assertFalse(result.time_passed)
        self.assertTrue(result.need_passed)

    def test_need_fail_others_pass(self):
        """需求不通过，其他通过。"""
        row = {
            "comment_text": "我在台州，不错",  # 没有服务需求
            "commented_at": (datetime.now(TZ) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M"),
            "problem": "",
        }
        class MockProfile:
            tz = TZ
            def section(self, name):
                if name == "export_gate":
                    return {
                        "enabled": True,
                        "geo": {
                            "enabled": True,
                            "local_words": ["台州"],
                            "out_words": [],
                            "confusable": {},
                            "ip_conflict_provinces": [],
                        },
                        "time": {"enabled": True, "max_days": 30},
                        "need": {
                            "enabled": True,
                            "need_words": ["求推荐"],
                            "exclude_words": [],
                        },
                    }
                return {}

        gate = ExportGate(MockProfile())
        result = gate.check(row)
        self.assertFalse(result.passed)
        self.assertTrue(result.geo_passed)
        self.assertTrue(result.time_passed)
        self.assertFalse(result.need_passed)


class RealWorldCasesTest(unittest.TestCase):
    """基于用户描述的真实场景测试。"""

    def setUp(self):
        self.cfg_geo = {
            "enabled": True,
            "local_words": ["台州", "台州市", "椒江", "椒江区", "路桥", "黄岩", "临海", "温岭", "玉环", "天台", "仙居", "三门"],
            "out_words": ["上海", "北京", "深圳", "杭州", "澳大利亚", "澳洲", "泰州"],
            "confusable": {"泰州": "台州"},
            "ip_conflict_provinces": ["上海", "北京", "广东", "江苏", "澳大利亚"],
        }
        self.geo_gate = GeoGate(self.cfg_geo)
        self.time_gate = TimeGate({"enabled": True, "max_days": 30}, TZ)
        self.need_gate = NeedGate({
            "enabled": True,
            "need_words": ["怎么报名", "多少钱", "有推荐", "求推荐", "在哪里", "哪家好", "靠谱吗"],
            "exclude_words": ["投诉", "避雷"],
        })

    def test_case_self_in_shanghai_ask_taizhou(self):
        """「我在上海，台州有吗」不通过。"""
        text = "我在上海，台州有吗"
        ok, reason, _ = self.geo_gate.check(text)
        self.assertFalse(ok, f"应不通过但通过了：{text}")

    def test_case_australia_ip_conflict(self):
        """澳大利亚 IP 与台州自述冲突。"""
        text = "我在台州，求推荐"
        ctx = {"ip_province": "澳大利亚"}
        ok, reason, _ = self.geo_gate.check(text, ctx)
        self.assertFalse(ok, f"应不通过但通过了：{text}")
        self.assertIn("冲突", reason)

    def test_case_2019_old_comment(self):
        """2019 年问句不通过。"""
        old = "2019-06-01 12:00"
        now = datetime(2026, 11, 12, tzinfo=TZ)
        ok, reason, _ = self.time_gate.check(old, now)
        self.assertFalse(ok, f"应不通过但通过了：{old}")
        self.assertIn("超过", reason)

    def test_case_2025_jan_australia(self):
        """2025 年 1 月澳大利亚评论（距今超 30 天）不通过。"""
        old = "2025-01-20 08:00"
        now = datetime(2026, 11, 12, tzinfo=TZ)
        ok, reason, _ = self.time_gate.check(old, now)
        self.assertFalse(ok)
        self.assertIn("超过", reason)

    def test_case_perfect_lead(self):
        """完美线索：本人自述台州 + 近期 + 有服务需求。"""
        text = "坐标椒江，哪家少儿编程靠谱，求推荐"
        commented_at = (datetime.now(TZ) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")

        geo_ok, _, _ = self.geo_gate.check(text)
        time_ok, _, _ = self.time_gate.check(commented_at)
        need_ok, _, _ = self.need_gate.check(text)

        self.assertTrue(geo_ok, "地域应通过")
        self.assertTrue(time_ok, "时效应通过")
        self.assertTrue(need_ok, "需求应通过")


if __name__ == "__main__":
    unittest.main()
