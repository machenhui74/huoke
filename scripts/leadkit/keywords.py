"""关键词扩写：品类 + 地区 → 10~18 条家长真实会搜的短语，供人工挑选。

纯本地：不联网、不调 API、不启动采集。
一次采集的词数有上限（见 guard.py 的 max_keywords），清单里的词多了就用待采队列分批采，不要整表一次开爬。
地区词表来自 profile 的 [geo]；地区不在词表里时不编造行政区，只用通用后缀补足。
"""
from __future__ import annotations

from pathlib import Path

from .logger import get_logger
from .profile import Profile

LOG = get_logger("keywords")


def _uniq(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _lexicon(profile: Profile) -> dict[str, list[str]]:
    g = profile.section("geo")
    return {"core": list(g.get("core", [])), "main": list(g.get("main", [])), "city": list(g.get("city", []))}


def _in_lexicon(place: str, lex: dict[str, list[str]]) -> bool:
    return any(key and key in place for key in lex["core"] + lex["main"] + lex["city"])


def _short_district(name: str, lex: dict[str, list[str]]) -> str:
    """椒江区 → 椒江。只有去掉「区」后的写法本身也在词表里才缩写，
    否则「工业园区」会被削成不成词的「工业园」，词表里没有的「黄岩区」也不会被编出来。"""
    known = set(lex["core"] + lex["main"] + lex["city"])
    short = name[:-1]
    return short if name.endswith("区") and len(name) > 2 and short in known else name


def _target_district(place: str, lex: dict[str, list[str]]) -> str | None:
    """用户写下的区就是这一刀的主地名；只写「台州」时才回到词表核心区。"""
    hits = [n for n in sorted(set(lex["core"] + lex["main"]), key=len, reverse=True) if n and n in place]
    return _short_district(hits[0], lex) if hits else None


def layout(place: str, lex: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """主地名在前，市名居中，其余区放最后（返回 (地名序列, 附近区)）。

    例：台州椒江 → 主词椒江，路桥/黄岩收尾；对不上词表的地区只保留原串。
    """
    if not _in_lexicon(place, lex):
        return [place], []
    target = _target_district(place, lex)
    if target is None:
        target = next(w for w in lex["core"] if not w.endswith("区"))
    districts: list[str] = []
    for name in lex["core"] + lex["main"]:
        short = _short_district(name, lex)
        if short not in districts:
            districts.append(short)
    nearby = [n for n in districts if n != target]
    short_city = [w for w in lex["city"] if not w.endswith("市")]
    long_city = [w for w in lex["city"] if w.endswith("市")]
    long_target = [n for n in lex["core"] + lex["main"] if n.endswith("区") and _short_district(n, lex) == target]
    return _uniq([target, place, *short_city, *long_target, *long_city, *nearby]), nearby


def _add(out: list[str], seen: set[str], text: str) -> None:
    text = "".join(text.split())
    if text and text not in seen:
        seen.add(text)
        out.append(text)


def expand(category: str, place: str, profile: Profile) -> tuple[list[str], list[str]]:
    """返回 (关键词列表, 附近区名单)。附近区的词排在末尾，且不建议进本次采集。"""
    kw = profile.section("keywords")
    t_min, t_max = kw.get("min", 10), kw.get("max", 18)
    demands, primary_tpl, extras = kw["demands"], kw["primary"], kw.get("extras", [])
    lex = _lexicon(profile)
    places, nearby_names = layout(place, lex)
    primary = places[0]
    out: list[str] = []
    seen: set[str] = set()

    for tmpl in primary_tpl:
        _add(out, seen, tmpl.format(p=primary, c=category))
        if len(out) >= t_max:
            return out, nearby_names
    nearby_set = set(nearby_names)
    others = [a for a in places[1:] if a not in nearby_set]
    nearby = [a for a in places[1:] if a in nearby_set]

    for i, alias in enumerate(others):
        _add(out, seen, f"{alias}{category}{demands[i % len(demands)]}")
        if len(out) >= t_max:
            return out, nearby_names
    for i, alias in enumerate(others):
        if len(out) >= t_max - min(2, len(nearby)):
            break
        _add(out, seen, f"{alias}{category}{demands[(i + 3) % len(demands)]}")
    # 非本次目标的区最多 2 条，固定排在末尾
    for i, alias in enumerate(nearby[:2]):
        if len(out) >= t_max:
            break
        _add(out, seen, f"{alias}{category}{demands[i % len(demands)]}")
    if len(places) == 1 or len(out) < t_min:
        for tmpl in extras:
            if len(out) >= t_max or (len(places) > 1 and len(out) >= t_min):
                break
            _add(out, seen, tmpl.format(p=primary, c=category))
    LOG.info("扩词完成: 品类=%s 地区=%s 共 %d 条（附近区 %s）", category, place, len(out), nearby_names)
    return out[:t_max], nearby_names


def format_lines(phrases: list[str], nearby: list[str]) -> list[str]:
    """附近区的词留在末尾并加注释，提醒不要直接拿去采集。"""
    note = "\t# 附近区，仅备选；本次不要采集"
    return [p + note if any(p.startswith(n) for n in nearby) else p for p in phrases]


def write_proposal(category: str, place: str, lines: list[str], out_dir: Path, out: str = "") -> Path:
    """把清单落盘，方便开采前打开挑选，而不是只在终端看一眼。"""
    safe = lambda s: "".join(ch for ch in s if ch not in "/\\:\0")  # 品类/地区原词保留，只去路径分隔符
    path = Path(out) if out else out_dir / f"{safe(category)}_{safe(place)}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [f"# 品类={category} 地区={place}", "# 单次词数有上限，不要整表开采。可用 leadctl queue add --from-proposal 本文件 入队，再 collect --next 分批采。"]
    path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    LOG.info("关键词清单已写入 %s", path)
    return path
