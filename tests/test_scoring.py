"""打分引擎与关键词扩写。"""
import unittest

import _helpers
from leadkit import keywords
from leadkit.scoring import Scorer


class ScoringTest(unittest.TestCase):
    def setUp(self):
        self.prof = _helpers.profile()
        self.scorer = Scorer(self.prof)

    def test_profile_selfcheck_cases_pass(self):
        """profile 里人工看过的句子必须全部通过——改词表的回归保护。"""
        self.assertEqual(self.scorer.self_check(), [])

    def test_ad_is_hard_excluded(self):
        got = self.scorer.score("欢迎咨询，私信我领资料", "椒江托管班")
        self.assertEqual(got["status"], "excluded")
        self.assertIn("广告/引流", got["exclude_reason"])

    def test_special_need_never_ready(self):
        """感统等特殊需求封顶，只能进复核。"""
        got = self.scorer.score("宝妈求推荐椒江感统训练哪家好多少钱", "椒江感统训练")
        self.assertNotEqual(got["status"], "ready")
        self.assertLessEqual(got["intent_score"], 65)

    def test_mixed_geo_goes_to_human(self):
        got = self.scorer.score("椒江还是杭州好？求推荐", "")
        self.assertFalse(got["geo_hit"])
        self.assertIn("地名混杂需人工", got["tags"])

    def test_street_false_positive(self):
        """「三甲医院」不能当成三甲街道。"""
        got = self.scorer.score("三甲医院旁边的托管班怎么样", "")
        self.assertNotIn("三甲", got["geo_evidence"])

    def test_empty_comment(self):
        for text in ("", "[笑哭]", "好"):
            self.assertEqual(self.scorer.score(text)["status"], "excluded")


class TemplateProfileTest(unittest.TestCase):
    """证明「换行业不改代码」：模板 profile 直接加载，同一套引擎照常工作。"""

    def setUp(self):
        from leadkit.profile import load_profile
        self.prof = load_profile(str(_helpers.ROOT / "profiles" / "_template.toml"))
        self.scorer = Scorer(self.prof)

    def test_template_loads_and_selfchecks(self):
        self.assertEqual(self.scorer.self_check(), [])

    def test_template_keywords_use_its_own_geo(self):
        phrases, nearby = keywords.expand("装修", "苏州姑苏", self.prof)
        self.assertEqual(phrases[0], "姑苏装修")
        self.assertTrue(set(nearby) <= {"工业园区", "吴中", "相城"})

    def test_education_words_do_not_leak_in(self):
        """教育 profile 的词不应影响家装 profile 的结果。"""
        got = self.scorer.score("幼小衔接托管班怎么报名")
        self.assertNotEqual(got["status"], "ready")


class KeywordsTest(unittest.TestCase):
    def test_count_and_primary(self):
        prof = _helpers.profile()
        phrases, nearby = keywords.expand("托管班", "台州椒江", prof)
        self.assertTrue(10 <= len(phrases) <= 18)
        self.assertEqual(phrases[0], "椒江托管班")
        self.assertEqual(set(nearby), {"路桥", "黄岩"})

    def test_unknown_place_not_invented(self):
        """地区不在词表里：不编造行政区，也要给出足够多的词。"""
        phrases, nearby = keywords.expand("托管班", "苏州", _helpers.profile())
        self.assertEqual(nearby, [])
        self.assertGreaterEqual(len(phrases), 8)
        self.assertTrue(all("苏州" in p for p in phrases))


if __name__ == "__main__":
    unittest.main()
