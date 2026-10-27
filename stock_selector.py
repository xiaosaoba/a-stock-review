# -*- coding: utf-8 -*-
"""
选股策略模块（规则引擎，确定性可复现）

用户定义的选股策略：
1. 当日 1 连板中，属于当日最强主线的个股
2. 当日 2 连板中，属于当日最强主线的个股

最强主线判定：按每只涨停股的"题材"（问财涨停原因）做主线聚类，
覆盖涨停家数最多的主线为当日最强主线；并列时取连板高度之和更高者，
再并列取首封时间更早者。
"""
import datetime


# 主线题材词典：主线名 -> 关键词（匹配涨停原因子串，命中任一即归类）
MAINLINE_KEYWORDS = {
    "机器人/具身智能": [
        "机器人", "具身智能", "腱绳", "灵巧手", "触觉", "减速器", "丝杠",
        "执行器", "伺服", "关节", "PEEK", "数据采集手套",
    ],
    "AI算力/CPO": [
        "算力", "CPO", "光模块", "光通信", "液冷", "数据中心", "AI服务器",
        "铜缆", "算力中心",
    ],
    "低空经济": ["低空", "eVTOL", "飞行汽车", "无人机"],
    "商业航天": ["航天", "卫星", "火箭"],
    "固态电池/锂电": ["固态电池", "锂电", "电池"],
    "并购重组": ["重组", "并购", "借壳", "股权转让", "收购", "控制权"],
    "消费/商贸零售": [
        "零售", "商贸", "食品", "乳业", "白酒", "零食", "免税", "旅游",
        "烘焙", "餐饮", "啤酒", "洗染", "日化",
    ],
    "医药/SPD": ["医药", "创新药", "SPD", "医疗", "药房", "中药", "医美"],
    "半导体/芯片": ["芯片", "半导体", "光刻", "存储", "封测", "晶圆"],
}


def classify_reason(reason):
    """把涨停原因归类到主线，返回主线名列表（一股可属多个主线）"""
    hits = []
    if not reason:
        return hits
    for mainline, kws in MAINLINE_KEYWORDS.items():
        for kw in kws:
            if kw in reason:
                hits.append(mainline)
                break
    return hits


def _first_time_sec(t):
    """首封时间 '09:25:01' -> 秒（越小越早），异常返回极大值"""
    try:
        hh, mm, ss = t.split(":")
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    except Exception:
        return 10 ** 9


def _sort_key(item):
    """候选排序：封单额降序 -> 首封时间升序 -> 换手率降序"""
    return (
        -float(item.get("seal_amount") or 0),
        _first_time_sec(item.get("first_limit_time") or ""),
        -float(item.get("turnover_rate") or 0),
    )


# ============================================================
# 评分机制：主线候选 0-100，按总分排序
# 25 主线热度 + 15 连板高度 + 25 封单 + 20 首封 + 15 换手
# ============================================================
def score_candidate(item, main_count, all_zt_count):
    sc = 0.0
    # 主线热度（相对覆盖）
    if all_zt_count > 0:
        sc += 25.0 * min(1.0, main_count / max(20, all_zt_count * 0.5))
    # 连板高度
    lb = int(item.get("lianban") or 0)
    sc += 15.0 if lb >= 2 else 10.0
    # 封单额
    seal = float(item.get("seal_amount") or 0)
    if seal >= 500000000:
        sc += 25
    elif seal >= 300000000:
        sc += 24
    elif seal >= 100000000:
        sc += 22
    elif seal >= 50000000:
        sc += 19
    elif seal >= 30000000:
        sc += 17
    elif seal >= 10000000:
        sc += 12
    else:
        sc += 8
    # 首封时间
    ft = _first_time_sec(item.get("first_limit_time") or "")
    if ft <= 9 * 3600 + 30 * 60:
        sc += 20
    elif ft <= 10 * 3600:
        sc += 16
    elif ft <= 11 * 3600 + 30 * 60:
        sc += 12
    elif ft <= 14 * 3600:
        sc += 8
    else:
        sc += 4
    # 换手
    to = float(item.get("turnover_rate") or 0)
    if 8 <= to <= 12:
        sc += 15
    elif 5 <= to < 8 or 12 < to <= 15:
        sc += 12
    elif 3 <= to < 5 or 15 < to <= 20:
        sc += 8
    else:
        sc += 4
    return round(min(100.0, sc), 1)


def _pick_reps(stocks, top=5):
    """代表个股：按连板高度降序、封单降序取前几名"""
    ordered = sorted(
        stocks,
        key=lambda s: (-int(s.get("lianban") or 0), -float(s.get("seal_amount") or 0)),
    )[:top]
    return "、".join(f"{s.get('name')}({s.get('lianban')}板)" for s in ordered)


def select_stocks(md):
    """
    按用户规则输出明日选股候选
    返回：{ok, mainline, candidates_1b, candidates_2b, rules, note}
    """
    zt = md.get("zt_pool") or []
    per = []  # [{item, mains, reason}]
    main_stats = {}
    for it in zt:
        reason = it.get("reason") or ""
        mains = classify_reason(reason)
        per.append({"item": it, "mains": mains, "reason": reason})
        for m in mains:
            st = main_stats.setdefault(m, {
                "count": 0, "lb_sum": 0, "stocks": [],
            })
            st["count"] += 1
            st["lb_sum"] += int(it.get("lianban") or 0)
            st["stocks"].append(it)

    if not main_stats:
        return {
            "ok": False,
            "mainline": None,
            "candidates_1b": [],
            "candidates_2b": [],
            "rules": "1、当日1连板中属于当日最强主线；2、当日2连板中属于当日最强主线",
            "note": "今日涨停股无可用题材数据，无法判定最强主线",
        }

    # 最强主线：覆盖家数 > 连板高度和 > 首封最早
    def main_sort_key(m):
        st = main_stats[m]
        earliest = min(
            (_first_time_sec(s.get("first_limit_time") or "") for s in st["stocks"]),
            default=10 ** 9,
        )
        return (st["count"], st["lb_sum"], -earliest)

    main = max(main_stats, key=main_sort_key)
    st = main_stats[main]
    mainline = {
        "name": main,
        "zt_count": st["count"],
        "lb_max": max(int(s.get("lianban") or 0) for s in st["stocks"]),
        "reps": _pick_reps(st["stocks"]),
        "all_zt_count": len(zt),
    }

    cand_1b = [p["item"] for p in per if int(p["item"].get("lianban") or 0) == 1 and main in p["mains"]]
    cand_2b = [p["item"] for p in per if int(p["item"].get("lianban") or 0) == 2 and main in p["mains"]]
    cand_1b.sort(key=_sort_key)
    cand_2b.sort(key=_sort_key)

    all_zt_count = len(zt)

    def brief(items):
        out = []
        for x in items:
            out.append({
                "name": x.get("name"),
                "code": x.get("code"),
                "lianban": int(x.get("lianban") or 0),
                "first_limit_time": x.get("first_limit_time"),
                "seal_amount_fmt": x.get("seal_amount_fmt"),
                "seal_amount": x.get("seal_amount"),
                "turnover_rate": x.get("turnover_rate"),
                "industry": x.get("industry"),
                "reason": x.get("reason", ""),
                "score": score_candidate(x, st["count"], all_zt_count),
            })
        out.sort(key=lambda y: -y["score"])
        return out

    return {
        "ok": True,
        "mainline": mainline,
        "candidates_1b": brief(cand_1b),
        "candidates_2b": brief(cand_2b),
        "rules": "1、当日1连板中属于当日最强主线；2、当日2连板中属于当日最强主线",
        "note": "最强主线=按涨停题材聚类覆盖家数最多者；候选按 0-100 评分排序（主线热度25+连板15+封单25+首封20+换手15），仅供参考，不构成投资建议",
    }


if __name__ == "__main__":
    # 自测
    import json
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config  # noqa
    from data_fetcher import fetch_market_data
    import datetime as dt
    md = fetch_market_data(dt.date.today().strftime("%Y-%m-%d"))
    sel = select_stocks(md)
    print(json.dumps(sel, ensure_ascii=False, indent=2)[:3000])
