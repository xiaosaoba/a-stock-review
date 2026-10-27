# -*- coding: utf-8 -*-
"""
选股技能框架：把第三方战法/选股条件封装为可配置、可扩展的"技能"，
与每日复盘报告一起输出。

当前内置技能：
  yijin2_daban —— 一进二打板战法
    来源：https://www.toutiao.com/article/7595364057306169919/
    规则：首板股筛选次日"一进二"候选
      4 步筛选（缺一不可）：
        S1 封板质量：封板时间早（10点前最佳）、一次封死不炸板、封单按市值分档达标、量能匹配
        S2 题材强度：属于主线/强分支，同题材板块联动（≥2只同题材涨停）
        S3 股价位置：低位启动或平台突破，避开累计涨幅>50%的高位股
        S4 市值流动性：市值 20-100 亿（放宽≤200亿提示），成交额≥1亿
      3 大信号（次日盘中确认，仅提示不筛选）：
        竞价弱转强、分时强度、介入时机
      风控纪律：止损/止盈/仓位
      避坑：伪龙头/量化席位/情绪退潮/过度追高
"""
import datetime

from data_fetcher import fetch_stock_extra
from stock_selector import MAINLINE_KEYWORDS, classify_reason


# ============================================================
# 技能注册表
# ============================================================
SKILL_DEFS = {
    "yijin2_daban": {
        "id": "yijin2_daban",
        "name": "一进二打板战法",
        "version": "1.0.0",
        "source": "https://www.toutiao.com/article/7595364057306169919/",
        "summary": "首板涨停后，次日冲击第二个涨停时介入。4 步筛选 + 3 大信号 + 风控纪律。",
        "target": "当日 1 连板（首板）个股，筛出符合标准的明日「一进二」候选",
        "filters": {
            "S1封板质量": {
                "desc": "封板时间≤10:00 最佳；一次封死不炸板；封单按市值分档；量能 1.5-3 倍、换手 5%-15%",
                "params": {
                    "first_time_ok": "10:00", "broken_max": 1,
                    "seal_small_min": 30000000, "seal_mid_min": 100000000,
                    "cap_small": 50, "cap_mid": 100,
                    "vol_ratio_min": 1.5, "vol_ratio_max": 3.0,
                    "turnover_min": 5.0, "turnover_max": 15.0,
                },
            },
            "S2题材强度": {
                "desc": "涨停题材命中主线/强分支，且同题材板块联动（同主线涨停≥2家）",
                "params": {"min_resonance": 2},
            },
            "S3股价位置": {
                "desc": "低位启动或平台突破；避开 60 日累计涨幅>50% 的高位股",
                "params": {"max_pct60": 50.0, "break_ratio": 0.95},
            },
            "S4市值流动性": {
                "desc": "市值 20-100 亿（100-200 亿提示）；当日成交额≥1 亿（3-6 亿最佳）",
                "params": {"cap_min": 20, "cap_max": 100, "cap_warn_max": 200, "amount_min": 100000000},
            },
        },
        "signals": (
            "次日盘中 3 大信号（确认介入，非收盘筛选）："
            "①竞价弱转强：高开 3%-5%、竞价成交额达首板全天 8%-12%、量比>3、9:20 后抛压减少；"
            "②分时强度：开盘直线拉升斜率>60°、回踩不破分时均线、封板前 5 分钟量达前 1 小时均量 3 倍；"
            "③介入时机：激进=竞价 9:25 挂涨停价博弈秒板；稳健=突破首板最高价放量介入或二板炸板 10 分钟内回封打板。"
        ),
        "risk": (
            "风控纪律：止损=破 5 日线或亏损 5% 无条件离场，二板炸板 10 分钟不回封次日竞价止损；"
            "止盈=沿 5 日线持股，收盘跌破且次日未收回离场，缩量加速后首次放量分歧减半仓；"
            "仓位=单票≤20%，情绪高潮期（涨停>80家）降至 10%，冰点期（涨停<30家）可提至 30%。"
        ),
        "pitfalls": (
            "避坑：①无板块支撑的孤龙不做（二板晋级率<20%）；②回避华泰总部/中金上海等量化席位主导的首板；"
            "③情绪退潮期坚决不做一进二；④首板前累计涨幅>50% 的高位股放弃。"
        ),
        "disclaimer": "本技能基于公开战法量化实现，仅供参考，不构成投资建议。",
    },
}


def _fmt_amount(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "--"
    if v >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return str(v)


def _first_time_sec(t):
    try:
        hh, mm, ss = t.split(":")
        return int(hh) * 3600 + int(mm) * 60 + int(ss)
    except Exception:
        return 10 ** 9


def _check_s1(item, ex):
    """封板质量"""
    reasons = []
    ok = True
    # 封板时间
    ft = _first_time_sec(item.get("first_limit_time") or "")
    if ft <= 10 * 3600:
        reasons.append("首封≤10:00 达标")
    elif ft <= 14 * 3600:
        reasons.append(f"首封{ft // 3600}:{ft % 3600 // 60:02d} 中等（需结合其他条件）")
    else:
        ok = False
        reasons.append(f"首封{ft // 3600}:{ft % 3600 // 60:02d} 过晚，跟风资金次日承接弱")
    # 炸板
    bc = int(item.get("broken_count") or 0)
    if bc <= 1:
        reasons.append(f"炸板{bc}次 达标" if bc == 0 else f"炸板{bc}次 偏弱")
    else:
        ok = False
        reasons.append(f"炸板{bc}次 过多，封板不坚决")
    # 封单分档
    seal = float(item.get("seal_amount") or 0)
    cap = float((ex or {}).get("mkt_cap") or 0)
    if cap < 50:
        need = 30000000
    elif cap < 100:
        need = 100000000
    else:
        need = 100000000
    if seal >= need:
        reasons.append(f"封单{_fmt_amount(seal)} ≥{_fmt_amount(need)} 达标")
    else:
        ok = False
        reasons.append(f"封单{_fmt_amount(seal)} <{_fmt_amount(need)} 偏小")
    # 量能/换手
    v5 = (ex or {}).get("vol_ratio_5d")
    to = float(item.get("turnover_rate") or 0)
    if v5 is not None:
        if 1.5 <= v5 <= 3.0:
            reasons.append(f"量能{v5:.1f}倍 达标")
        else:
            ok = False
            reasons.append(f"量能{v5:.1f}倍 超出 1.5-3 倍区间")
    if 5.0 <= to <= 15.0:
        reasons.append(f"换手{to:.1f}% 达标")
    elif 3.0 <= to < 5.0 or 15.0 < to <= 20.0:
        reasons.append(f"换手{to:.1f}% 临界")
    else:
        ok = False
        reasons.append(f"换手{to:.1f}% 不达标")
    return ok, reasons


def _check_s2(item, zt_main_counts):
    """题材强度：主线 + 板块联动"""
    reason = item.get("reason") or ""
    mains = classify_reason(reason)
    reasons = []
    ok = False
    if not mains:
        reasons.append("无题材数据，无法确认主线属性")
        return False, reasons
    best = mains[0]
    cnt = zt_main_counts.get(best, 0)
    if cnt >= 2:
        ok = True
        reasons.append(f"题材「{best}」全市场涨停 {cnt} 家，板块联动成立")
    else:
        reasons.append(f"题材「{best}」仅 {cnt} 家涨停，联动不足")
    return ok, reasons


def _check_s3(item, ex):
    """股价位置：低位/平台突破，避开高位"""
    reasons = []
    ok = False
    pct60 = (ex or {}).get("pct_60")
    high60 = (ex or {}).get("high_60")
    close = float(item.get("price") or 0)
    if pct60 is None or not high60:
        reasons.append("无历史K线，位置无法判定")
        return False, reasons
    if pct60 > 50:
        reasons.append(f"60日涨幅 {pct60:.0f}% >50%，高位股放弃")
        return False, reasons
    if close >= high60 * 0.95:
        reasons.append(f"平台突破（现价贴近60日高点 {high60:.2f}）")
        ok = True
    elif pct60 <= 30:
        reasons.append(f"低位启动（60日涨幅仅 {pct60:.0f}%）")
        ok = True
    else:
        reasons.append(f"位置中性（60日涨幅 {pct60:.0f}%）")
        ok = True
    return ok, reasons


def _check_s4(item, ex):
    """市值流动性"""
    reasons = []
    ok = True
    cap = float((ex or {}).get("mkt_cap") or 0)
    amount = float((ex or {}).get("amount") or 0)
    if cap <= 0:
        reasons.append("无市值数据")
        ok = False
    elif 20 <= cap <= 100:
        reasons.append(f"市值 {cap:.0f}亿 达标")
    elif 100 < cap <= 200:
        reasons.append(f"市值 {cap:.0f}亿 偏大（100-200亿区间，流动性提示）")
        ok = False
    else:
        reasons.append(f"市值 {cap:.0f}亿 超出 20-100 亿区间")
        ok = False
    if amount >= 100000000:
        best = "3-6亿最佳" if 300000000 <= amount <= 600000000 else ""
        reasons.append(f"成交额 {_fmt_amount(amount)}{('（' + best + '）') if best else ''}")
    else:
        reasons.append(f"成交额 {_fmt_amount(amount)} <1亿，流动性不足")
        ok = False
    return ok, reasons


# ============================================================
# 评分机制：四步各维度连续打分，总分 0-100，按总分排序
# ============================================================
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _score_s1(item, ex):
    """S1 封板质量 0-30"""
    sc = 0.0
    ft = _first_time_sec(item.get("first_limit_time") or "")
    if ft <= 9 * 3600 + 35 * 60:
        sc += 10
    elif ft <= 10 * 3600:
        sc += 8
    elif ft <= 14 * 3600:
        sc += 5
    else:
        sc += 2
    bc = int(item.get("broken_count") or 0)
    sc += 5 if bc == 0 else (3 if bc == 1 else 0)
    seal = float(item.get("seal_amount") or 0)
    cap = float((ex or {}).get("mkt_cap") or 0)
    ratio = (seal / (cap * 1e8)) * 100 if cap > 0 else 0  # 封单/市值 %
    if ratio >= 3:
        sc += 10
    elif ratio >= 2:
        sc += 8
    elif ratio >= 1:
        sc += 6
    elif ratio >= 0.5:
        sc += 4
    else:
        sc += 2
    v5 = (ex or {}).get("vol_ratio_5d")
    if v5 is not None:
        if 1.5 <= v5 <= 3.0:
            sc += max(2.5, 5 - abs(v5 - 2.0))  # 2 倍最佳
        else:
            sc += 2
    to = float(item.get("turnover_rate") or 0)
    if 8 <= to <= 12:
        sc += 5
    elif 5 <= to < 8 or 12 < to <= 15:
        sc += 4
    elif 3 <= to < 5 or 15 < to <= 20:
        sc += 3
    else:
        sc += 1
    return round(_clamp(sc, 0, 30), 1)


def _score_s2(item, zt_main_counts, best_main):
    """S2 题材强度 0-25"""
    mains = classify_reason(item.get("reason") or "")
    if not mains:
        return 3.0
    sc = 15.0 if best_main in mains else 12.0
    cnt = max(zt_main_counts.get(m, 0) for m in mains)
    if cnt >= 10:
        sc += 10
    elif cnt >= 5:
        sc += 8
    elif cnt >= 2:
        sc += 6
    else:
        sc += 3
    return round(_clamp(sc, 0, 25), 1)


def _score_s3(item, ex):
    """S3 股价位置 0-20"""
    pct60 = (ex or {}).get("pct_60")
    if pct60 is None:
        return 6.0
    if pct60 <= 15:
        sc = 16.0
    elif pct60 <= 30:
        sc = 14.0
    elif pct60 <= 40:
        sc = 10.0
    elif pct60 <= 50:
        sc = 6.0
    else:
        sc = 2.0
    high60 = (ex or {}).get("high_60")
    close = float(item.get("price") or 0)
    if high60 and close >= high60 * 0.95:
        sc += 4.0  # 平台突破加分
    return round(_clamp(sc, 0, 20), 1)


def _score_s4(item, ex):
    """S4 市值流动性 0-25"""
    cap = float((ex or {}).get("mkt_cap") or 0)
    amount = float((ex or {}).get("amount") or 0)
    if cap <= 0:
        sc = 0.0
    elif 20 <= cap <= 50:
        sc = 15.0
    elif 50 <= cap <= 80:
        sc = 13.0
    elif 80 <= cap <= 100:
        sc = 10.0
    elif 100 < cap <= 200:
        sc = 6.0
    elif cap < 20:
        sc = 8.0
    else:
        sc = 3.0
    if 300000000 <= amount <= 600000000:
        sc += 10.0
    elif 100000000 <= amount < 300000000 or 600000000 < amount <= 1000000000:
        sc += 8.0
    elif amount > 1000000000:
        sc += 6.0
    elif amount > 0:
        sc += 3.0
    return round(_clamp(sc, 0, 25), 1)


def run_skill(skill_id, md):
    """
    执行选股技能，返回结构化的荐股结果（与复盘报告一起输出）。
    md: fetch_market_data 返回的市场数据（含 zt_pool/reason）
    """
    if skill_id not in SKILL_DEFS:
        return {"ok": False, "error": f"未知技能 {skill_id}"}
    skill = SKILL_DEFS[skill_id]

    # 目标：当日 1 连板
    zt = md.get("zt_pool") or []
    first_boards = [s for s in zt if int(s.get("lianban") or 0) == 1]
    if not first_boards:
        return {
            "ok": False, "skill": {k: skill[k] for k in ("id", "name", "version", "source", "summary", "target")},
            "note": "当日无 1 连板个股，无法执行一进二筛选",
        }

    # 增强数据：市值/成交额/量能/位置
    codes = [s["code"] for s in first_boards]
    extra = fetch_stock_extra(codes)

    # 同主线涨停家数（板块联动）
    zt_main_counts = {}
    for s in zt:
        for m in classify_reason(s.get("reason") or ""):
            zt_main_counts[m] = zt_main_counts.get(m, 0) + 1
    best_main = max(zt_main_counts, key=zt_main_counts.get) if zt_main_counts else None

    candidates, watchlist, filtered = [], [], []
    for s in first_boards:
        ex = extra.get(s["code"], {})
        checks = {}
        all_ok = True
        for name, fn in (("S1封板质量", _check_s1), ("S2题材强度", _check_s2),
                         ("S3股价位置", _check_s3), ("S4市值流动性", _check_s4)):
            ok, rs = fn(s, ex) if name != "S2题材强度" else fn(s, zt_main_counts)
            checks[name] = {"ok": ok, "detail": "；".join(rs)}
            if not ok:
                all_ok = False
        scores = {
            "S1封板质量": _score_s1(s, ex),
            "S2题材强度": _score_s2(s, zt_main_counts, best_main),
            "S3股价位置": _score_s3(s, ex),
            "S4市值流动性": _score_s4(s, ex),
        }
        total = round(sum(scores.values()), 1)
        item = {
            "name": s.get("name"), "code": s.get("code"),
            "industry": s.get("industry"), "reason": s.get("reason", ""),
            "first_limit_time": s.get("first_limit_time"),
            "seal_amount_fmt": s.get("seal_amount_fmt"),
            "turnover_rate": s.get("turnover_rate"),
            "mkt_cap": round(float(ex.get("mkt_cap") or 0), 1),
            "amount_fmt": _fmt_amount(ex.get("amount") or 0),
            "vol_ratio_5d": ex.get("vol_ratio_5d"),
            "pct_60": ex.get("pct_60"),
            "ma5": ex.get("ma5"),
            "checks": checks,
            "scores": scores,
            "score": total,
        }
        npass = sum(1 for c in checks.values() if c["ok"])
        if npass == 4:
            candidates.append(item)
        elif npass == 3:
            watchlist.append(item)
        else:
            fail = [n for n, c in checks.items() if not c["ok"]]
            filtered.append({**item, "fail": fail, "fail_detail": "；".join(checks[f]["detail"] for f in fail)})

    # 排序：总分降序 -> 封单降序
    def sort_c(x):
        return (-x.get("score", 0), -float(x.get("seal_amount") and 0 or 0) or 0)
    candidates.sort(key=sort_c)
    watchlist.sort(key=sort_c)
    filtered.sort(key=sort_c)

    return {
        "ok": True,
        "skill": {k: skill[k] for k in ("id", "name", "version", "source", "summary", "target")},
        "filters": {k: v["desc"] for k, v in skill["filters"].items()},
        "date": md.get("date"),
        "total_first": len(first_boards),
        "candidates": candidates,
        "watchlist_count": len(watchlist),
        "watchlist": watchlist,
        "filtered_count": len(filtered),
        "filtered": filtered[:15],
        "signals": skill["signals"],
        "risk": skill["risk"],
        "pitfalls": skill["pitfalls"],
        "disclaimer": skill["disclaimer"],
    }


if __name__ == "__main__":
    import json
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config  # noqa
    from data_fetcher import fetch_market_data
    md = fetch_market_data(datetime.date.today().strftime("%Y-%m-%d"))
    res = run_skill("yijin2_daban", md)
    print(json.dumps({k: res[k] for k in ("ok", "total_first", "filtered_count")}, ensure_ascii=False))
    print("候选:", [(c["name"], c["mkt_cap"], c["pct_60"]) for c in res.get("candidates", [])])
    print("过滤数:", len(res.get("filtered", [])))
    with open("skill_rec_out.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
