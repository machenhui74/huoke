"""「明确咨询抬入 ready」：问价 / 问约课 / 问几岁能上。默认关闭，只在开启它的 profile 里生效。"""
import unittest

import _helpers
from leadkit.profile import list_profiles, load_profile
from leadkit.scoring import Scorer

KW = "台州感统训练"


class InquiryPromotionTest(unittest.TestCase):
    def setUp(self):
        self.sc = Scorer(load_profile("gantong_taizhou"))

    def status(self, text):
        return self.sc.score(text, KW)["status"]

    def test_every_builtin_profile_passes_its_own_cases(self):
        """新增 profile 或改词表后，自带用例必须全过，否则等于把判断标准悄悄改了。"""
        for name in list_profiles():
            with self.subTest(profile=name):
                self.assertEqual(Scorer(load_profile(name)).self_check(), [])

    def test_user_confirmed_inquiries_are_ready(self):
        """用户逐句确认「这些就是比较明显的意向客户」。"""
        for text in ["体验课怎么约", "怎么预约", "免费体验吗？", "多少一个月",
                     "2-3岁的多少一节，怎么包课，有什么活动？有没有体育课", "几岁可以？", "多大孩子能上？", "在哪里，多少一个月"]:
            with self.subTest(text=text):
                got = self.sc.score(text, KW)
                self.assertEqual((got["status"], got["strength"]), ("ready", "高"))
                self.assertGreaterEqual(got["intent_score"], 70)
                self.assertIn("明确咨询", got["tags"])

    def test_things_that_must_not_be_promoted(self):
        cases = {
            "在哪里": "needs_review",                        # 只问位置：没有本地地名，只进复核，不标红
            "这家避雷，多少一个月": "low_archive",              # 投诉口吻
            "我家娃多动，注意力不集中，多少一个月": "needs_review",  # 特殊需求只进复核
            "欢迎咨询，多少一个月，私信我": "excluded",          # 广告引流
            "被家长的话暖到心坎里了，多少一个月": "low_archive",   # 机构口吻
            "这一天收费怎么也得150": "low_archive",             # 吐槽不是问价
            "2岁不到可以吗": "needs_review",                   # 泛咨询仍待人工判断
            "真不错": "low_archive",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.status(text), want)

    def test_short_inquiries_that_used_to_be_dropped(self):
        """真实评论里曾被低分归档的短咨询：联系方式、位置、课程时间、有没有这种课、报了哪家。"""
        for text in ["有联系电话吗", "台州的在哪里", "暑假班上多久啊", "路桥有吗", "温岭有吗 3岁不到的宝宝[doge]",
                     "有普通的感统课吗", "吾悦有普通的体能课吗", "中秋节这两天营业了吗？", "现在还有这种活动吗",
                     "怎么拼", "儿童乐园对外开放吗", "你好 请问你最后报了哪里啊"]:
            with self.subTest(text=text):
                self.assertEqual(self.status(text), "ready")

    def test_question_floor_keeps_weak_questions_in_review(self):
        """兜底：带疑问形态、又沾上品类/地名/年龄的短评论至少进复核。"""
        for text in ["在哪嘞", "哪里哪里？", "2岁宝宝能玩吗", "2周女宝合适吗", "四岁可以不"]:
            with self.subTest(text=text):
                self.assertEqual(self.status(text), "needs_review")

    def test_merchant_statements_and_phone_numbers_are_not_promoted(self):
        """商家的陈述句、贴了手机号/座机的引流评论，不能被当成客户在问。"""
        for text in ["联系电话13800138000，欢迎来玩", "联系电话：0576-88888888 台州在哪里", "即日起对外开放",
                     "联系电话见主页", "有吗", "星辉的园内环境也太棒了吧", "主要是给中小学生，那学习的是什么语言呢"]:
            with self.subTest(text=text):
                self.assertEqual(self.status(text), "low_archive")

    def test_question_floor_off_means_plain_chatter_stays_dropped(self):
        prof = load_profile("gantong_taizhou")
        prof.data["scoring"]["question_floor"] = 0
        self.assertEqual(Scorer(prof).score("2岁宝宝能玩吗", KW)["status"], "low_archive")

    def test_switch_off_restores_old_behaviour(self):
        """inquiry_ready_score = 0 即关闭；此时问价只能进复核，不会标红。"""
        prof = load_profile("gantong_taizhou")
        prof.data["scoring"]["inquiry_ready_score"] = 0
        off = Scorer(prof)
        self.assertEqual(off.score("多少一个月", KW)["status"], "needs_review")
        # 关闭后约课问法只靠旧的「买方保底」抬进复核，不会到 ready
        self.assertEqual(off.score("体验课怎么约", KW)["status"], "needs_review")

    def test_education_profile_is_untouched(self):
        """教培画像没开这个开关，行为必须和以前完全一致。"""
        edu = Scorer(_helpers.profile())
        self.assertEqual(edu.inquiry_ready, 0)
        self.assertEqual(edu.score("怎么收费", "椒江托管班")["status"], "needs_review")
        self.assertEqual(edu.score("多大可以托", "椒江托管班")["status"], "needs_review")

    def test_promotion_never_lowers_a_higher_score(self):
        got = self.sc.score("我家娃想报感统训练，求推荐，多少钱一节", KW)
        self.assertGreater(got["intent_score"], 70)          # 本来就高的不被压回 70
        self.assertEqual(got["status"], "ready")


if __name__ == "__main__":
    unittest.main()
