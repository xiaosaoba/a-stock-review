# -*- coding: utf-8 -*-
"""全局配置：从环境变量 / .env 读取"""
import os


def _load_dotenv(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

# ---- AI 配置（火山方舟 / DeepSeek 兼容 OpenAI 接口）----
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
AI_API_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "doubao-seed-1-6-250615")
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "120"))
AI_TEMPERATURE = os.environ.get("AI_TEMPERATURE", "").strip()

# 问财（Iwencai）涨停题材
IWENCAI_BASE_URL = os.environ.get("IWENCAI_BASE_URL", "https://openapi.iwencai.com")
IWENCAI_API_KEY = os.environ.get("IWENCAI_API_KEY", "").strip()

# ---- 数据源配置 ----
EASTMONEY_UT = "7eea3edcaed734bea9cbfc24409ed989"  # 东财通用 token
REQUEST_TIMEOUT = 15
MAX_RETRY = 3
RETRY_DELAY = (3, 6)  # 随机延迟秒数

REPORTS_DIR = os.environ.get("REPORTS_DIR", "reports")
PORT = int(os.environ.get("PORT", "5000"))
