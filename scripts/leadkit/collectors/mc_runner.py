"""在 MediaCrawler 自己的 Python 环境里运行的启动器（由 leadctl 通过 uv 调起，不要直接用）。

为什么要有它：上游把「请求间隔」「是否附着现有 Chrome」等放在 config/base_config.py 里，
命令行参数覆盖不到。直接改上游文件会让升级和补丁都变得脆弱，
所以这里在导入后、启动前，用环境变量里的 JSON 对 config 做运行时覆盖，上游源码保持原样。
"""
import asyncio
import importlib.util
import json
import os
import runpy
import sys


def main() -> None:
    mc_dir = os.getcwd()  # 调用方保证 cwd = MediaCrawler 根目录
    sys.path.insert(0, mc_dir)
    overrides = json.loads(os.environ.get("LEADKIT_MC_OVERRIDES", "{}"))

    import config  # noqa: E402  上游的全局配置模块

    for key, value in overrides.items():
        setattr(config, key, value)
    # 留一行可被日志审计的痕迹，事后能确认当次到底用了什么限速
    print(f"LEADKIT_OVERRIDES_APPLIED {json.dumps(overrides, ensure_ascii=False)}", flush=True)

    # 节奏随机化：把固定间隔换成对数正态随机间隔（只会更慢）。
    # pacing.py 只用标准库，这里按文件路径加载——本进程跑在 MediaCrawler 的虚拟环境里，没有 leadkit 包。
    pacing_cfg = json.loads(os.environ.get("LEADKIT_PACING", "null"))
    if pacing_cfg:
        spec = importlib.util.spec_from_file_location("leadkit_pacing", os.path.join(os.path.dirname(__file__), "pacing.py"))
        pacing = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pacing)
        base = pacing_cfg.pop("base")
        pacer = pacing.install(asyncio, base, pacing_cfg, emit=lambda m: print(m, flush=True))
        print(f"LEADKIT_PACING_APPLIED base={base} " + ("on" if pacer else "off"), flush=True)

    sys.argv = ["main.py", *sys.argv[1:]]
    runpy.run_path(os.path.join(mc_dir, "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
