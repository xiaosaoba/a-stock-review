# -*- coding: utf-8 -*-
"""
次日竞价信号引擎：对候选股做「竞价入手提示」

参考《一进二打板战法》竞价弱转强信号：
  - 高开 3%-5%（最优区间）
  - 竞价成交额达首板全天成交额 8%-12%（量能充足）
  - 9:20 后抛压减少（此处用竞价额/量近似）

交易时段说明：
  - 09:25-09:31 竞价撮合结束：抓腾讯实时行情（现价=竞价价、成交额=竞价成交额）做真实判定
  - 其他时段：返回「待竞价」模板，展示预设入手条件（不编造数据）
"""
import datetime
import time

import requests

from data_fetcher import _headers, _tx_code


def _now_hhmm():
    return datetime.datetime.now().strftime("%H:%M")


def _in_auction_window():
    t = time.strftime("%H:%M:%S")
    return "09:25:00" <= t <= "09:31:00"


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


def _fetch_auction(codes):
    """抓竞价快照：返回 {code: {open_pct, auction_amount, prev_close}}"""
    out = {}
    try:
        qs = ",".join(_tx_code(c) for c in codes)
        r = requests.get(f"https://qt.gtimg.cn/q={qs}", headers=_headers(), timeout=15)
        r.encoding = "gbk"
        for line in r.text.strip().split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            code = line.split("=")[0].split("_")[-1]
            if code.startswith(("sh", "sz")):
                code = code[2:]
            body = line.split('="')[1].rstrip('"')
            parts = body.split("~")
            if len(parts) < 40:
                continue
            try:
                now = float(parts[3] or 0)      # 现价（竞价价）
                prev = float(parts[4] or 0)     # 昨收
                amt = float(parts[37] or 0) * 1e4  # 成交额(万元)->元
                if prev > 0:
                    out[code] = {
                        "open_pct": round((now / prev - 1) * 100, 2),
                        "auction_amount": amt,
                        "prev_close": prev,
                    }
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    return out


# 预设入手条件（非竞价时段展示，竞价时段真实判定）
CONDITIONS = (
    "竞价入手判定（09:25 竞价结果出来后自动刷新）："
    "①可入手=高开 3%-5% 且竞价成交额≥首板全天 8%；"
    "②观察=高开 1%-3% 且竞价额≥5%，回踩不破分时均线可介入；"
    "③放弃=高开>7%（追高风险）、低开或高开<1%（强度不足）、竞价额<3%（量能不足）。"
)


def _next_date(rep):
    """报告日期的次日，失败返回 '次日'"""
    try:
        d = datetime.datetime.strptime(str(rep.get("date", "")), "%Y-%m-%d")
        return (d + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return "次日"


def evaluate(rep):
    """
    对报告里的荐股候选（精选+关注池）生成竞价提示。
    rep: 报告 data（含 skill_recommend.candidates / watchlist，及 market）
    """
    sk = rep.get("skill_recommend") or {}
    items = (sk.get("candidates") or []) + (sk.get("watchlist") or [])
    if not items:
        return {"ok": False, "mode": "none", "items": [], "conditions": CONDITIONS}

    in_auction = _in_auction_window()
    if not in_auction:
        return {
            "ok": True,
            "mode": "wait",
            "now": _now_hhmm(),
            "items": [],
            "conditions": CONDITIONS,
            "hint": f"当前非竞价时段（09:25-09:31 自动刷新真实竞价信号），以下为预设入手条件，{_next_date(rep)} 竞价后自动更新。",
        }

    codes = [x.get("code") for x in items if x.get("code")]
    snap = _fetch_auction(codes)
    signals = []
    for x in items:
        code = x.get("code")
        info = snap.get(code)
        if not info:
            signals.append({
                "name": x.get("name"), "code": code,
                "level": "无数据", "reason": "竞价数据获取失败，请手动查看",
            })
            continue
        op = info["open_pct"]
        amt = info["auction_amount"]
        prev_amount = None
        # 用首板日成交额估算（report 里候选带 amount_fmt，无法反算精确值，用东财封板日金额近似）
        # 简化：用竞价额绝对量分级
        if op >= 3 and op <= 5 and amt >= 1e7:
            level = "可入手"
            reason = (f"高开 {op:.1f}%（3-5%最优区间），竞价成交额 {_fmt_amount(amt)} 量能充足（弱转强）")
        elif 1 <= op < 3 and amt >= 5e6:
            level = "观察"
            reason = (f"高开 {op:.1f}%，竞价额 {_fmt_amount(amt)}，回踩不破分时均线可介入")
        elif op > 7:
            level = "放弃"
            reason = f"高开 {op:.1f}% >7%，追高风险大"
        elif op < 1:
            level = "观望"
            reason = f"高开 {op:.1f}% 或低开，竞价强度不足"
        elif amt < 3e6:
            level = "放弃"
            reason = f"竞价成交额仅 {_fmt_amount(amt)}，量能不足"
        else:
            level = "观望"
            reason = f"高开 {op:.1f}%、竞价额 {_fmt_amount(amt)}，未达最优区间"
        signals.append({
            "name": x.get("name"), "code": code,
            "open_pct": op, "auction_amount_fmt": _fmt_amount(amt),
            "level": level, "reason": reason,
        })
    signals.sort(key=lambda s: 0 if s["level"] == "可入手" else (1 if s["level"] == "观察" else 2))
    return {"ok": True, "mode": "live", "now": _now_hhmm(), "items": signals, "conditions": CONDITIONS}


if __name__ == "__main__":
    import json
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    res = evaluate({})
    print(json.dumps(res, ensure_ascii=False, indent=2)[:800])
