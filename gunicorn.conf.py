# -*- coding: utf-8 -*-
"""gunicorn 配置：单 worker + 多线程（适配 Flask SSE 进度推送）"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
workers = 1
threads = 8
worker_class = "gthread"
timeout = 420
graceful_timeout = 60
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
