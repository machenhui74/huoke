"""跨平台兼容：uv 定位、时区回落、全局参数、命令预览、uv sync 失败提示。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError

import _helpers
from leadkit import cli, profile as profile_mod, tools
from leadkit.collectors.mediacrawler import MediaCrawlerCollector
from leadkit.guard import CollectPlan
from leadkit.paths import Workspace
from leadkit.profile import ProfileError, load_platform_map
from leadkit.setup import sync_failure_hint


def _fake_uv(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / tools._exe("uv")
    f.write_text("", encoding="utf-8")
    return f


class FindUvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        # 隔离环境：HOME 指向临时目录、PATH 里没有 uv、没有 LEADKIT_UV
        env = {k: v for k, v in os.environ.items() if k not in (tools.ENV_UV, "LOCALAPPDATA")}
        self.patches = [mock.patch.dict(os.environ, env, clear=True),
                        mock.patch.object(Path, "home", return_value=self.home),
                        mock.patch("leadkit.tools.shutil.which", return_value=None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_not_found(self):
        self.assertIsNone(tools.find_uv())

    def test_installed_but_off_path(self):
        """复现 Windows 报告：uv 在 ~/.local/bin 但不在 PATH，必须能找到并标明不在 PATH。"""
        exe = _fake_uv(self.home / ".local" / "bin")
        got = tools.find_uv()
        self.assertEqual((got.path, got.source, got.off_path), (str(exe), "fallback", True))

    def test_path_beats_fallback(self):
        _fake_uv(self.home / ".local" / "bin")
        with mock.patch("leadkit.tools.shutil.which", return_value="/usr/bin/uv"):
            got = tools.find_uv()
        self.assertEqual((got.path, got.source), ("/usr/bin/uv", "path"))

    def test_env_var_wins(self):
        _fake_uv(self.home / ".local" / "bin")
        custom = _fake_uv(self.home / "custom")
        with mock.patch.dict(os.environ, {tools.ENV_UV: str(custom)}):
            self.assertEqual(tools.find_uv().source, "env")

    def test_bad_env_var_falls_back(self):
        exe = _fake_uv(self.home / ".local" / "bin")
        with mock.patch.dict(os.environ, {tools.ENV_UV: str(self.home / "nope")}):
            self.assertEqual(tools.find_uv().path, str(exe))

    def test_localappdata_candidate_on_windows(self):
        exe = _fake_uv(self.home / "AppData" / "Programs" / "uv")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.home / "AppData")}):
            self.assertEqual(tools.find_uv().path, str(exe))


class TimezoneTest(unittest.TestCase):
    def setUp(self):
        profile_mod.get_tz.cache_clear()

    def tearDown(self):
        profile_mod.get_tz.cache_clear()

    def test_fixed_offset_fallback_for_utc8_zones(self):
        with mock.patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("x")):
            tz = profile_mod.get_tz("Asia/Shanghai")
        self.assertEqual(tz.utcoffset(None).total_seconds(), 8 * 3600)

    def test_other_zones_refuse_to_guess(self):
        """缺时区库时，非 +8 时区不能静默回落成 UTC+8：日配额和冷却会算错。"""
        with mock.patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("x")):
            with self.assertRaises(ProfileError) as ctx:
                profile_mod.get_tz("America/New_York")
        self.assertIn("tzdata", str(ctx.exception))

    def test_result_is_cached(self):
        with mock.patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("x")) as zi:
            profile_mod.get_tz("Asia/Shanghai")
            profile_mod.get_tz("Asia/Shanghai")
        self.assertEqual(zi.call_count, 1)  # 只解析一次，也就只提示一次


class CliArgsTest(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def test_global_flags_before_or_after_subcommand(self):
        """agent 常把 --json 写在子命令前面，两种位置都要认。"""
        self.assertTrue(self.parse("--json", "pool", "stats").json)
        self.assertTrue(self.parse("pool", "stats", "--json").json)
        self.assertFalse(self.parse("pool", "stats").json)

    def test_subcommand_defaults_do_not_clobber_top_level(self):
        a = self.parse("--profile", "x", "--workdir", "/w", "-v", "pool", "stats")
        self.assertEqual((a.profile, a.workdir, a.verbose), ("x", "/w", True))
        self.assertEqual(self.parse("pool", "stats", "--profile", "y").profile, "y")

    def test_default_profile(self):
        self.assertEqual(self.parse("doctor").profile, cli.DEFAULT_PROFILE)


class CommandPreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mc = Path(self.tmp.name) / "mc"
        self.col = MediaCrawlerCollector(Workspace(Path(self.tmp.name) / "ws"), _helpers.profile(), self.mc)
        self.plan = CollectPlan("xhs", ["a"], 5, 3)
        self.pmap = load_platform_map("xhs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_interpreter_never_prints_none(self):
        with mock.patch("leadkit.collectors.mediacrawler.find_uv", return_value=None):
            cmd = self.col.build_command(self.plan, self.pmap, Path("/b"))
            text = self.col.describe(self.plan, Path("/b"), self.pmap)
        self.assertIn("未找到", cmd[0])
        self.assertNotIn("None", " ".join(cmd) + text)

    def test_no_interpreter_is_refused(self):
        with mock.patch("leadkit.collectors.mediacrawler.find_uv", return_value=None):
            self.assertTrue(any("uv" in b for b in self.col.preflight(self.plan, self.pmap)))
            with self.assertRaises(RuntimeError):
                self.col.run(self.plan, self.pmap, "b", Path(self.tmp.name) / "b")

    def test_off_path_uv_is_used_by_absolute_path(self):
        found = tools.Found("C:/Users/x/.local/bin/uv.exe", "fallback")
        with mock.patch("leadkit.collectors.mediacrawler.find_uv", return_value=found):
            cmd = self.col.build_command(self.plan, self.pmap, Path("/b"))
        self.assertEqual(cmd[:3], [found.path, "run", "python"])

    def test_venv_python_preferred_over_uv(self):
        """有 .venv 就直接用它：终止信号才能直达爬虫本体，而不是只杀掉 uv。"""
        py = self.mc / (".venv/Scripts/python.exe" if tools.IS_WIN else ".venv/bin/python")
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
        with mock.patch("leadkit.collectors.mediacrawler.find_uv", return_value=tools.Found("/x/uv", "path")):
            cmd = self.col.build_command(self.plan, self.pmap, Path("/b"))
        self.assertEqual(cmd[0], str(py))
        self.assertNotIn("uv", cmd[:2])


class SyncHintTest(unittest.TestCase):
    def test_winerror_183_names_the_package(self):
        tail = (r"error: failed to build jieba==0.42.1 ... [WinError 183] 当文件已存在时，无法创建该文件。: "
                r"'C:\Users\cool3\AppData\Local\uv\cache\builds-v0\jieba-0.42.1.dist-info'")
        hint = sync_failure_hint(tail)
        self.assertIn("uv cache clean jieba", hint)

    def test_unknown_failure_has_no_hint(self):
        self.assertEqual(sync_failure_hint("error: network unreachable"), "")


if __name__ == "__main__":
    unittest.main()
