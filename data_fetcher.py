# -*- coding: utf-8 -*-
"""
数据获取模块：东方财富 push2/push2ex/push2his + 腾讯财经 API
基于 TRAE 社区《A股智能复盘系统》踩坑经验实现：
  1. 东财涨停池价格单位为"毫"(0.001元)，>1000 自动除以 1000
  2. 跌停池必须用 sort=zdp:asc，否则返回空
  3. 日期统一标准化：2026-07-15 <-> 20260715
  4. 3 次重试 + 随机延迟(3-6s) + 轮换 User-Agent 防限流
  5. 当日指数优先腾讯财经 API（更稳定）
"""
import datetime
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

import config

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


def _headers():
    return {
        "User-Agent": random.choice(UA_LIST),
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }


def _http_get(url, params=None, timeout=None):
    """GET 请求：3 次重试 + 随机延迟 3-6 秒 + UA 轮换"""
    timeout = timeout or config.REQUEST_TIMEOUT
    last_err = None
    for attempt in range(1, config.MAX_RETRY + 1):
        try:
            resp = requests.get(url, params=params, headers=_headers(), timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
        if attempt < config.MAX_RETRY:
            time.sleep(random.uniform(*config.RETRY_DELAY))
    raise RuntimeError(f"请求失败 {url}: {last_err}")


def _normalize_date(date):
    """统一日期格式：'2026-07-15' / '20260715' / '2026/07/15' -> 20260715"""
    if date is None:
        date = datetime.date.today().isoformat()
    s = str(date).replace("-", "").replace("/", "").strip()
    if len(s) == 8 and s.isdigit():
        return s
    raise ValueError(f"无法识别的日期: {date}")


def _is_today(date):
    """判断是否为今日（以交易行为准：今日含盘中）"""
    return _normalize_date(date) == datetime.date.today().strftime("%Y%m%d")


def _parse_price(v):
    """东财毫单位价格 -> 元：值 > 1000 时除以 1000（股票价格不可能超过 1000 元/股的常见判断）"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v > 1000:
        return round(v / 1000.0, 3)
    return round(v, 3)


def _fmt_time(t):
    """东财时间字段 93000 -> 09:30:00"""
    try:
        t = str(int(t)).zfill(6)
        return f"{t[:2]}:{t[2:4]}:{t[4:6]}"
    except Exception:
        return "--"


def _fmt_amount(v):
    """金额格式化：亿/万"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "--"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.2f}万"
    return f"{v:.0f}"


# ============================================================
# 一、大盘指数（5 大指数：上证、深成、创业板、沪深300、中证500）
# ============================================================
INDEX_CFG = [
    {"code": "000001", "secid": "1.000001", "qt": "sh000001", "name": "上证指数"},
    {"code": "399001", "secid": "0.399001", "qt": "sz399001", "name": "深证成指"},
    {"code": "399006", "secid": "0.399006", "qt": "sz399006", "name": "创业板指"},
    {"code": "000300", "secid": "1.000300", "qt": "sh000300", "name": "沪深300"},
    {"code": "000905", "secid": "1.000905", "qt": "sh000905", "name": "中证500"},
]


def _get_index_today_tencent():
    """腾讯财经实时指数（当日，更稳定）"""
    qs = ",".join(x["qt"] for x in INDEX_CFG)
    resp = requests.get(f"https://qt.gtimg.cn/q={qs}", headers=_headers(), timeout=config.REQUEST_TIMEOUT)
    resp.encoding = "gbk"
    result = []
    for line in resp.text.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        payload = line.split('="', 1)[1].rstrip('"')
        parts = payload.split("~")
        if len(parts) < 38:
            continue
        try:
            result.append({
                "code": parts[2],
                "name": parts[1],
                "price": round(float(parts[3]), 2),
                "change": round(float(parts[31]), 2),
                "change_pct": round(float(parts[32]), 2),
                "amount": round(float(parts[37]) * 10000, 2),  # 万元 -> 元
            })
        except (ValueError, IndexError):
            continue
    return result


def _get_index_today_eastmoney():
    """东方财富实时指数（备用）"""
    secids = ",".join(x["secid"] for x in INDEX_CFG)
    data = _http_get(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        params={"fltt": 2, "invt": 2, "secids": secids,
                "fields": "f2,f3,f4,f6,f12,f14"},
    )
    items = (data.get("data") or {}).get("diff") or []
    result = []
    for it in items:
        result.append({
            "code": it.get("f12", ""),
            "name": it.get("f14", ""),
            "price": round((it.get("f2") or 0), 2),
            "change": round((it.get("f4") or 0), 2),
            "change_pct": round((it.get("f3") or 0), 2),
            "amount": round((it.get("f6") or 0), 2),
        })
    return result


def _get_index_history(date):
    """历史日期指数：东财 push2his K线"""
    result = []
    for idx in INDEX_CFG:
        try:
            data = _http_get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={
                    "secid": idx["secid"], "klt": 101, "fqt": 1,
                    "beg": date, "end": date,
                    "fields1": "f1,f2,f3,f4,f5",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57",
                },
            )
            klines = (data.get("data") or {}).get("klines") or []
            if not klines:
                continue
            k = klines[-1].split(",")  # 日期,开,收,高,低,量,额
            if len(k) < 7:
                continue
            close, prev_close = float(k[2]), float(k[1])
            result.append({
                "code": idx["code"],
                "name": idx["name"],
                "price": round(close, 2),
                "change": round(close - prev_close, 2),
                "change_pct": round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
                "amount": round(float(k[6]), 2),
            })
        except Exception:
            continue
    return result


def get_index_quotes(date):
    """获取指定日期 5 大指数行情"""
    date = _normalize_date(date)
    if _is_today(date):
        try:
            quotes = _get_index_today_tencent()
            if len(quotes) == 5:
                return quotes
        except Exception:
            pass
        try:
            quotes = _get_index_today_eastmoney()
            if len(quotes) >= 3:
                return quotes
        except Exception:
            pass
    try:
        quotes = _get_index_history(date)
        if quotes:
            return quotes
    except Exception:
        pass
    raise RuntimeError("指数行情获取失败（当日/历史均失败）")


# ============================================================
# 二、涨停池 / 跌停池 / 炸板池
# ============================================================
def _get_pool(endpoint, date, sort):
    """通用：东财 push2ex 池数据"""
    data = _http_get(
        f"https://push2ex.eastmoney.com/{endpoint}",
        params={
            "ut": config.EASTMONEY_UT,
            "dpt": "wz.ztzt",
            "Pageindex": 0,
            "pagesize": 1000,
            "sort": sort,
            "date": date,
        },
    )
    pool = ((data.get("data") or {}).get("pool")) or []
    return pool


def get_zt_pool(date):
    """涨停池：sort=fbt:asc"""
    return _get_pool("getTopicZTPool", date, "fbt:asc")


def get_dt_pool(date):
    """跌停池：必须 sort=zdp:asc，否则返回空"""
    return _get_pool("getTopicDTPool", date, "zdp:asc")


def get_zb_pool(date):
    """炸板池"""
    return _get_pool("getTopicZBPool", date, "zbc:asc")


def _parse_pool_item(it):
    """解析池数据：价格毫转元、时间格式化"""
    return {
        "code": it.get("c", ""),
        "name": it.get("n", ""),
        "price": _parse_price(it.get("p", 0)),
        "change_pct": round(float(it.get("zdp", 0) or 0), 2),
        "first_limit_time": _fmt_time(it.get("fbt")),
        "last_limit_time": _fmt_time(it.get("lbt")),
        "broken_count": int(it.get("zbc", 0) or 0),
        "seal_amount": it.get("fund", 0) or 0,          # 封单资金（元）
        "seal_amount_fmt": _fmt_amount(it.get("fund", 0) or 0),
        "turnover_rate": round(float(it.get("hs", 0) or 0), 2),
        "lianban": int(it.get("lbc", 0) or 0),          # 连板数
        "industry": it.get("hybk", ""),
    }


def get_zt_list(date):
    """涨停池解析列表"""
    return [_parse_pool_item(x) for x in get_zt_pool(date)]


def get_dt_list(date):
    """跌停池解析列表"""
    return [_parse_pool_item(x) for x in get_dt_pool(date)]


def get_zb_list(date):
    """炸板池解析列表"""
    return [_parse_pool_item(x) for x in get_zb_pool(date)]


def build_lianban_tiers(zt_list):
    """连板梯队：按连板数分组，取每组前排"""
    tiers = {}
    for item in zt_list:
        lb = item["lianban"]
        tiers.setdefault(lb, []).append(item)
    result = []
    for lb in sorted(tiers.keys(), reverse=True):
        items = sorted(tiers[lb], key=lambda x: x.get("first_limit_time", "--"))
        result.append({
            "lianban": lb,
            "count": len(items),
            "stocks": items[:15],
        })
    return result


# ============================================================
# 三、涨跌家数（全市场统计：腾讯 rank 接口分页统计，带缓存）
# ============================================================
_UP_DOWN_CACHE = {"ts": 0, "data": None}
_UP_DOWN_CACHE_TTL = 180  # 秒：盘中缓存 3 分钟，避免重复拉全市场


def get_updown_counts(force=False):
    """
    全市场涨/跌/平家数。
    腾讯 getBoardRankList 接口（count 上限 200）分页统计，约 23 页。
    带 180 秒缓存。失败返回 {"up": None, "down": None, "flat": None}。
    """
    now = time.time()
    if not force and _UP_DOWN_CACHE["data"] and (now - _UP_DOWN_CACHE["ts"]) < _UP_DOWN_CACHE_TTL:
        return _UP_DOWN_CACHE["data"]

    url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
    count = 200
    up = down = flat = 0
    offset = 0
    page = 0
    try:
        while page < 30:
            params = {
                "board_code": "aStock",
                "sort_type": "price",
                "direct": "down",
                "offset": offset,
                "count": count,
            }
            data = _http_get(url, params=params)
            lst = (data.get("data") or {}).get("rank_list") or []
            if not lst:
                break
            for it in lst:
                try:
                    zdf = float(it.get("zdf", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if zdf > 0:
                    up += 1
                elif zdf < 0:
                    down += 1
                else:
                    flat += 1
            page += 1
            offset += count
            if len(lst) < count:
                break
            time.sleep(0.25)
        result = {"up": up, "down": down, "flat": flat}
        if page > 0:
            _UP_DOWN_CACHE["data"] = result
            _UP_DOWN_CACHE["ts"] = now
        return result
    except Exception:
        return {"up": None, "down": None, "flat": None}


# ============================================================
# 四、行业 / 概念板块排行
# ============================================================
def _get_sector(fs, top=20):
    data = _http_get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={
            "pn": 1, "pz": top, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": fs,
            "fields": "f2,f3,f8,f12,f14,f62,f104,f105,f106",
        },
    )
    diff = ((data.get("data") or {}).get("diff")) or []
    result = []
    for it in diff:
        result.append({
            "code": it.get("f12", ""),
            "name": it.get("f14", ""),
            "change_pct": round(float(it.get("f3", 0) or 0), 2),
            "main_inflow": it.get("f62", 0) or 0,
            "up_count": int(it.get("f104", 0) or 0),
            "down_count": int(it.get("f105", 0) or 0),
        })
    return result


def _get_sector_tencent(board_type, top=15):
    """腾讯板块排行备用源：board_type=hy(行业)/gn(概念)"""
    url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank"
    params = {
        "board_type": board_type,
        "sort_type": "price",
        "direct": "down",
        "offset": 0,
        "count": top,
    }
    data = _http_get(url, params=params)
    lst = (data.get("data") or {}).get("rank_list") or []
    result = []
    for it in lst:
        result.append({
            "code": it.get("code", ""),
            "name": it.get("name", ""),
            "change_pct": round(float(it.get("zdf", 0) or 0), 2),
            "main_inflow": 0,
            "up_count": 0,
            "down_count": 0,
            "leader": (it.get("lzg") or {}).get("name", ""),
        })
    return result


def get_sector_rank():
    """行业板块 + 概念板块涨幅 Top（东财优先，腾讯备用）"""
    industry, concept = [], []
    try:
        industry = _get_sector("m:90+t:2", 15)
    except Exception:
        industry = []
    try:
        concept = _get_sector("m:90+t:3", 15)
    except Exception:
        concept = []
    if not industry:
        try:
            industry = _get_sector_tencent("hy", 15)
        except Exception:
            pass
    if not concept:
        try:
            concept = _get_sector_tencent("gn", 15)
        except Exception:
            pass
    return {"industry": industry, "concept": concept}


# ============================================================
# 五、汇总
# ============================================================
def fetch_zt_reasons(date):
    """用问财获取涨停原因（题材标签），返回 {code: 涨停原因}；未配置 Key 或失败返回 {}"""
    reasons = {}
    key = getattr(config, "IWENCAI_API_KEY", "")
    base = getattr(config, "IWENCAI_BASE_URL", "https://openapi.iwencai.com")
    if not key:
        return reasons
    try:
        query = "今日涨停的A股" if _is_today(date) else f"{date}涨停的A股"
        payload = {
            "query": query,
            "page": "1",
            "limit": "100",
            "is_cache": "1",
            "expand_index": "true",
        }
        resp = requests.post(
            base.rstrip("/") + "/v1/query2data",
            json=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "X-Claw-Call-Type": "normal",
                "X-Claw-Skill-Id": "hithink-astock-selector",
                "X-Claw-Skill-Version": "1.0.0",
                "X-Claw-Plugin-Id": "none",
                "X-Claw-Plugin-Version": "none",
                "X-Claw-Trace-Id": uuid.uuid4().hex,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return reasons
        body = resp.json()
        reason_key = f"涨停原因[{date}]"
        for d in body.get("datas", []):
            code = str(d.get("股票代码", "")).split(".")[0]
            reason = d.get(reason_key, "") or ""
            if code and reason:
                reasons[code] = str(reason)
    except Exception:
        pass
    return reasons


def _tx_code(code):
    """A股代码 -> 腾讯代码（sh/sz 前缀）"""
    c = str(code)
    if c.startswith(("6", "9", "5", "4")):
        return "sh" + c
    return "sz" + c


def _kline_metrics(kl):
    """60日K线指标：前5日均量/5日线/10日线/60日高低/区间涨幅"""
    if not kl or len(kl) < 6:
        return None
    try:
        closes = [float(x[2]) for x in kl]
        vols = [float(x[5]) for x in kl]
        highs = [float(x[3]) for x in kl]
        lows = [float(x[4]) for x in kl]
    except Exception:
        return None
    cur = closes[-1]
    prev5 = vols[-6:-1]
    vol5 = sum(prev5) / len(prev5) if prev5 else 0
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else sum(closes) / len(closes)
    high60 = max(highs[-60:])
    low60 = min(lows[-60:])
    base = closes[-60] if len(closes) >= 60 else closes[0]
    pct60 = (cur / base - 1) * 100 if base else 0
    pct20 = (cur / closes[-21] - 1) * 100 if len(closes) >= 21 else None
    return {
        "vol5": vol5, "ma5": round(ma5, 2), "ma10": round(ma10, 2),
        "high_60": high60, "low_60": low60,
        "pct_60": round(pct60, 1), "pct_20": round(pct20, 1) if pct20 is not None else None,
    }


def _fetch_kline(code):
    """腾讯前复权日K线（80日），失败返回 None"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={_tx_code(code)},day,,,80,qfq"
        r = requests.get(url, headers=_headers(), timeout=15)
        j = r.json()
        d = j.get("data", {}).get(_tx_code(code), {})
        kl = d.get("qfqday") or d.get("day") or []
        return code, _kline_metrics(kl)
    except Exception:
        return code, None


def fetch_stock_extra(codes):
    """
    批量获取个股增强数据（腾讯接口）：
    - 实时：总市值/流通市值、当日成交额、换手率、量比
    - 历史：前5日均量（量能倍数）、5日/10日均线、60日高低、区间涨幅
    返回 {code: {...}}；失败字段缺失
    """
    if not codes:
        return {}
    codes = [str(c) for c in codes]
    extra = {}

    # 1. 实时行情（腾讯 qt.gtimg.cn，GBK）
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        try:
            qs = ",".join(_tx_code(c) for c in batch)
            r = requests.get(f"https://qt.gtimg.cn/q={qs}", headers=_headers(), timeout=15)
            r.encoding = "gbk"
            for line in r.text.strip().split(";"):
                line = line.strip()
                if "=" not in line:
                    continue
                code = line.split("=")[0].split("_")[-1]
                if code.startswith(("sh", "sz")):
                    code = code[2:]  # 去掉前缀，与 K 线 key 一致
                body = line.split('="')[1].rstrip('"')
                parts = body.split("~")
                if len(parts) < 50:
                    continue
                try:
                    mkt = max(float(parts[44] or 0), float(parts[45] or 0))  # 亿
                except (ValueError, IndexError):
                    mkt = 0.0
                extra[code] = {
                    "name": parts[1] if len(parts) > 1 else "",
                    "close": float(parts[3] or 0),
                    "amount": float(parts[37] or 0) * 1e4,       # 元
                    "turnover": float(parts[38] or 0),            # %
                    "vol_ratio": float(parts[43] or 0),           # 量比
                    "mkt_cap": mkt,                               # 亿
                    "today_vol": float(parts[6] or 0),            # 手
                }
        except Exception:
            continue

    # 2. 历史K线（并发，每只 1 次请求）
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, km in ex.map(_fetch_kline, codes):
            if km:
                base = extra.setdefault(code, {})
                base.update(km)
                if "today_vol" in base and km.get("vol5"):
                    base["vol_ratio_5d"] = round(base["today_vol"] / km["vol5"], 2)
    return extra


def fetch_market_data(date):
    """
    获取指定日期完整市场数据
    date: '2026-07-15' / '20260715' / None(今日)
    """
    date = _normalize_date(date)
    is_today = _is_today(date)

    indexes = get_index_quotes(date)
    zt_list = get_zt_list(date)
    dt_list = get_dt_list(date)
    zb_list = get_zb_list(date)
    sectors = get_sector_rank()

    # 涨跌家数：仅当日有精确全市场统计；历史日期置空（报告注明口径）
    updown = get_updown_counts() if is_today else {"up": None, "down": None, "flat": None}

    # 总成交额：5 大指数成交额合计（近似全市场）
    total_amount = sum(x.get("amount", 0) or 0 for x in indexes)

    # 连板梯队
    tiers = build_lianban_tiers(zt_list)

    # 最高连板
    max_lianban = max([x["lianban"] for x in zt_list], default=0)

    # 炸板率 = 炸板数 / (涨停数 + 炸板数)
    zb_count = len(zb_list)
    zt_count = len(zt_list)
    broken_rate = round(zb_count / (zt_count + zb_count) * 100, 1) if (zt_count + zb_count) > 0 else 0.0

    # 行业板块平均涨幅（用于 AI 分析）
    industry_avg = 0.0
    if sectors["industry"]:
        industry_avg = round(sum(x["change_pct"] for x in sectors["industry"]) / len(sectors["industry"]), 2)

    # 涨停题材（问财涨停原因）附加到涨停池，AI 据此判断市场主线
    if is_today:
        zt_reasons = fetch_zt_reasons(date)
        if zt_reasons:
            for item in zt_list:
                item["reason"] = zt_reasons.get(item["code"], "")

    return {
        "date": date,
        "is_today": is_today,
        "indexes": indexes,
        "zt_pool": zt_list,
        "zt_count": zt_count,
        "dt_pool": dt_list,
        "dt_count": len(dt_list),
        "zb_count": zb_count,
        "broken_rate": broken_rate,
        "updown": updown,
        "sectors": sectors,
        "industry_avg": industry_avg,
        "total_amount": total_amount,
        "lianban_tiers": tiers,
        "max_lianban": max_lianban,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    # 本地自测
    import json
    d = fetch_market_data(datetime.date.today().strftime("%Y-%m-%d"))
    print(json.dumps({
        "date": d["date"],
        "indexes": d["indexes"],
        "zt_count": d["zt_count"],
        "dt_count": d["dt_count"],
        "zb_count": d["zb_count"],
        "broken_rate": d["broken_rate"],
        "updown": d["updown"],
        "max_lianban": d["max_lianban"],
        "industry_top": d["sectors"]["industry"][:5],
        "tiers": d["lianban_tiers"][:3],
    }, ensure_ascii=False, indent=2))
