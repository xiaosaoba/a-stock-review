# -*- coding: utf-8 -*-
"""
A股智能复盘系统 - Flask 主应用
架构：Flask + SSE 进度推送 + gunicorn(单worker多线程)
"""
import json
import os
import time
import traceback
from datetime import datetime, date

from flask import Flask, jsonify, render_template, request, Response, stream_with_context

import config
from data_fetcher import fetch_market_data, _normalize_date
from ai_analyzer import analyze
from stock_selector import select_stocks
from stock_skill import run_skill
from stock_signal import evaluate as evaluate_signal

app = Flask(__name__)

os.makedirs(config.REPORTS_DIR, exist_ok=True)


def _report_path(date):
    return os.path.join(config.REPORTS_DIR, f"{_normalize_date(date)}.json")


def _load_report(date):
    path = _report_path(date)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save_report(date, data):
    path = _report_path(date)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/market")
def api_market():
    """获取指定日期市场数据（不生成报告）"""
    d = request.args.get("date") or date.today().strftime("%Y-%m-%d")
    try:
        md = fetch_market_data(d)
        return jsonify({"ok": True, "data": md})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/report", methods=["POST"])
def api_generate_report():
    """生成报告：SSE 流式推送进度"""
    payload = request.get_json(silent=True) or {}
    d = payload.get("date") or date.today().strftime("%Y-%m-%d")

    def gen():
        def emit(event, data):
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        try:
            yield from emit("progress", {"stage": "connecting", "msg": "正在连接行情数据源..."})
            time.sleep(0.3)

            yield from emit("progress", {"stage": "market", "msg": "正在获取大盘指数与涨跌停池数据..."})
            md = fetch_market_data(d)

            yield from emit("progress", {"stage": "market_done", "msg": "市场数据获取完成", "market": md})

            yield from emit("progress", {"stage": "select", "msg": "正在计算选股策略（最强主线 × 1/2连板）..."})
            selection = select_stocks(md)

            yield from emit("progress", {"stage": "skill", "msg": "正在执行选股技能（一进二打板战法）..."})
            skill_rec = run_skill("yijin2_daban", md)

            yield from emit("progress", {"stage": "ai", "msg": "AI 正在分析情绪周期与策略..."})
            ai = analyze(md)

            report = {
                "date": md["date"],
                "market": md,
                "ai": ai,
                "selection": selection,
                "skill_recommend": skill_rec,
                "signal": evaluate_signal({
                    "market": md,
                    "skill_recommend": skill_rec,
                }),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            path = _save_report(md["date"], report)

            yield from emit("done", {"msg": "报告生成完成", "report": report, "path": path})
        except Exception as e:
            traceback.print_exc()
            yield from emit("error", {"msg": f"生成失败: {str(e)}"})

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/report")
def api_get_report():
    """获取已生成的报告"""
    d = request.args.get("date") or date.today().strftime("%Y-%m-%d")
    rep = _load_report(d)
    if rep:
        return jsonify({"ok": True, "data": rep})
    return jsonify({"ok": False, "error": "该日期暂无报告"})


@app.route("/report/<date>.html")
def report_snapshot(date):
    """每日报告快照页：单文件静态 HTML（数据内嵌，可分享到 QQ/微信直接打开）"""
    rep = _load_report(date)
    if not rep:
        return render_template("snapshot.html", rep={"date": date, "ai": {}, "market": {}, "selection": {}, "skill_recommend": {}, "signal": {}})
    return render_template("snapshot.html", rep=rep)


@app.route("/download/<date>.html")
def report_download(date):
    """下载当日报告快照：单文件 HTML 附件（可本地存档/双击打开）"""
    rep = _load_report(date)
    if not rep:
        return jsonify({"ok": False, "error": "该日期暂无报告"}), 404
    html = render_template("snapshot.html", rep=rep)
    resp = Response(html, mimetype="text/html")
    resp.headers["Content-Disposition"] = f'attachment; filename="report_{date}.html"'
    return resp


@app.route("/api/reports")
def api_list_reports():
    """最近报告列表"""
    items = []
    try:
        for fn in os.listdir(config.REPORTS_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(config.REPORTS_DIR, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    rep = json.load(f)
                items.append({
                    "date": rep.get("date", fn[:-5]),
                    "score": (rep.get("ai") or {}).get("score"),
                    "cycle": (rep.get("ai") or {}).get("cycle"),
                    "created_at": rep.get("created_at", ""),
                })
            except Exception:
                continue
    except Exception:
        pass
    items.sort(key=lambda x: x["date"], reverse=True)
    return jsonify({"ok": True, "data": items[:30]})


@app.route("/api/status")
def api_status():
    """系统状态：AI Key 是否配置"""
    return jsonify({
        "ok": True,
        "ai_key_configured": bool(config.AI_API_KEY),
        "ai_model": config.AI_MODEL,
        "ai_base_url": config.AI_BASE_URL,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True)
