# -*- coding: utf-8 -*-
"""
AI 分析模块：调用大模型生成情绪评分、情绪周期、核心观点、明日策略
- 默认走火山方舟（豆包）OpenAI 兼容接口，可通过 .env 切换 DeepSeek 等
- 未配置 API Key 时自动降级为规则引擎评分，保证系统始终可用
"""
import json
import time

import requests

import config


def _call_llm(system_prompt, user_prompt):
    """调用 OpenAI 兼容接口（火山方舟 / DeepSeek / Kimi）"""
    url = config.AI_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 8000,
        "stream": False,
    }
    # 部分模型（如 kimi-k3-1-pro）仅接受 temperature=1，默认不传由服务端决定
    t = getattr(config, "AI_TEMPERATURE", "")
    if t:
        payload["temperature"] = float(t)
    resp = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {config.AI_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=config.AI_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM 接口返回 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return content


def _extract_json(text):
    """从 LLM 输出中稳健提取 JSON（兼容 ```json 包裹）"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ============================================================
# 规则引擎（无 API Key 时的降级评分与建议）
# ============================================================
def _rule_score(md):
    """基于市场数据计算情绪评分 1-10"""
    score = 5.0
    zt, dt = md.get("zt_count", 0), md.get("dt_count", 0)
    up = (md.get("updown") or {}).get("up") or 0
    down = (md.get("updown") or {}).get("down") or 0
    max_lb = md.get("max_lianban", 0)
    broken = md.get("broken_rate", 0.0)

    if zt >= 80:
        score += 1.5
    elif zt >= 50:
        score += 1.0
    elif zt >= 30:
        score += 0.5
    if dt == 0:
        score += 0.5
    elif dt <= 10:
        score += 0.2
    elif dt > 30:
        score -= 1.0
    if up and down and (up + down) > 0:
        ratio = up / (up + down)
        score += (ratio - 0.5) * 2
    if max_lb >= 6:
        score += 0.5
    elif max_lb >= 4:
        score += 0.3
    if broken > 40:
        score -= 0.5
    elif broken > 25:
        score -= 0.2
    return round(max(1, min(10, score)), 1)


def _rule_cycle(score):
    if score >= 8:
        return "高潮期"
    if score >= 6.5:
        return "活跃期"
    if score >= 4.5:
        return "混沌期"
    return "冰点期"


def _fallback_report(md):
    """无 Key 时的规则降级报告"""
    score = _rule_score(md)
    cycle = _rule_cycle(score)
    zt, dt = md.get("zt_count", 0), md.get("dt_count", 0)
    max_lb = md.get("max_lianban", 0)
    up = (md.get("updown") or {}).get("up")
    down = (md.get("updown") or {}).get("down")
    ind_top = md.get("sectors", {}).get("industry", [])[:3]
    ind_names = "、".join(x["name"] for x in ind_top) or "暂无"

    updown_txt = f"上涨 {up} 家 / 下跌 {down} 家" if (up is not None) else "涨跌家数（历史日期无实时口径）"
    views = [
        f"{md.get('date', '')} 涨停 {zt} 家、跌停 {dt} 家，最高 {max_lb} 连板，炸板率 {md.get('broken_rate', 0)}%，{updown_txt}。",
        f"领涨行业板块：{ind_names}。短线情绪评分 {score} 分，处于「{cycle}」。",
    ]
    return {
        "score": score,
        "cycle": cycle,
        "cycle_reason": (
            f"基于涨停 {zt} 家、跌停 {dt} 家、最高 {max_lb} 连板、炸板率 {md.get('broken_rate', 0)}%"
            f"及涨跌家数结构综合判断（规则引擎评分，未配置 AI Key）。"
        ),
        "views": views,
        "board": f"行业板块方面，{ind_names} 涨幅居前，注意板块轮动与持续性。",
        "strategy": {
            "opportunity": "重点关注低位首板与 1 进 2 换手板，回避缩量加速品种。",
            "risk": f"警惕高位连板股断板引发的情绪退潮；跌停家数若超预期需降低仓位。",
            "advice": "控制仓位、去弱留强，优先做前排换手板，谨慎追高。",
        },
    }


# ============================================================
# 主入口
# ============================================================
def analyze(market_data):
    """生成 AI 复盘报告（JSON 结构）"""
    if not config.AI_API_KEY:
        return _fallback_report(market_data)

    system_prompt = (
        "你是一名资深 A 股短线复盘分析师，精通情绪周期理论、连板梯队、龙头战法。"
        "请根据提供的市场数据，输出专业、客观、可操作的复盘报告。"
        "必须只输出 JSON，不要输出任何其他文字。JSON 结构："
        '{"score": 1-10的整数或一位小数, "cycle": "情绪周期阶段", "cycle_reason": "判断理由", '
        '"views": ["核心观点1", "核心观点2", "核心观点3"], "board": "板块分析文字", '
        '"strategy": {"opportunity": "机会方向", "risk": "风险警示", "advice": "核心建议"}}'
        "重要：判断市场主线板块时，必须优先依据【涨停明细】中每只股票的“题材”（涨停原因，如人形机器人、机器人零部件），"
        "将题材相同的涨停股聚类成主线题材（例如多只涨停股的题材都含“机器人”即为机器人主线），"
        "不要只按静态行业分类（如“汽车零部”）下结论——静态行业常无法反映真实炒作主线。"
        "板块分析需点明真正的主线题材及其代表个股。"
    )

    md = market_data
    updown = md.get("updown") or {}
    up_txt = updown.get("up") if updown.get("up") is not None else "无实时口径"
    down_txt = updown.get("down") if updown.get("down") is not None else "无实时口径"

    ind_rank = "\n".join(
        f"{i + 1}. {x['name']} {x['change_pct']:+.2f}%（上涨{x['up_count']}家/下跌{x['down_count']}家）"
        for i, x in enumerate(md.get("sectors", {}).get("industry", [])[:10])
    ) or "无数据"
    con_rank = "\n".join(
        f"{i + 1}. {x['name']} {x['change_pct']:+.2f}%"
        for i, x in enumerate(md.get("sectors", {}).get("concept", [])[:10])
    ) or "无数据"

    zt_txt = "\n".join(
        f"{x['name']}({x['code']}) {x['lianban']}板 封单{x['seal_amount_fmt']} 首封{x['first_limit_time']} "
        f"行业:{x['industry']} 题材:{x.get('reason', '')}"
        for x in md.get("zt_pool", [])[:40]
    ) or "无"
    dt_txt = "\n".join(
        f"{x['name']}({x['code']}) {x['change_pct']}%"
        for x in md.get("dt_pool", [])[:20]
    ) or "无"
    tier_txt = "\n".join(
        f"{t['lianban']}连板 {t['count']}只：" + "、".join(s['name'] for s in t['stocks'][:8])
        for t in md.get("lianban_tiers", [])[:6]
    ) or "无"

    idx_txt = "\n".join(
        f"{x['name']} {x['price']} {x['change_pct']:+.2f}% 成交{x.get('amount', 0) / 1e8:.1f}亿"
        for x in md.get("indexes", [])
    ) or "无"

    user_prompt = f"""
【数据日期】{md.get('date')}（{'当日实时' if md.get('is_today') else '历史日'}）
【大盘指数】
{idx_txt}
【涨跌家数】上涨 {up_txt} 家 / 下跌 {down_txt} 家（历史日期可能无精确口径）
【涨停家数】{md.get('zt_count')} 家，【跌停家数】{md.get('dt_count')} 家，【炸板】{md.get('zb_count')} 家（炸板率 {md.get('broken_rate')}%）
【最高连板】{md.get('max_lianban')} 板
【连板梯队】
{tier_txt}
【行业板块涨幅榜】
{ind_rank}
【概念板块涨幅榜】
{con_rank}
【涨停明细（前40）】
{zt_txt}
【跌停明细（前20）】
{dt_txt}
【全市场成交额约】{md.get('total_amount', 0) / 1e8:.0f} 亿

请输出复盘报告 JSON。
"""

    try:
        raw = _call_llm(system_prompt, user_prompt)
        return _extract_json(raw)
    except Exception as e:
        # LLM 失败：记录错误并降级为规则引擎
        fallback = _fallback_report(market_data)
        fallback["ai_error"] = str(e)[:200]
        return fallback


if __name__ == "__main__":
    from data_fetcher import fetch_market_data
    import datetime
    md = fetch_market_data(datetime.date.today().strftime("%Y-%m-%d"))
    print(json.dumps(analyze(md), ensure_ascii=False, indent=2))
