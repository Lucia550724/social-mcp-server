#!/usr/bin/env python3
"""
Social MCP Server - 独立入口 (Streamable HTTP)
直接运行在 Railway PORT 上
"""
import os
import sys
import subprocess
import shutil
from pathlib import Path

# ── 启动前确保 ffmpeg 可用 ──────────────────────────────
def ensure_ffmpeg():
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    print("ffmpeg not found, attempting to install...")
    methods = [
        ["apt-get", "update", "-qq"],
        ["apt-get", "install", "-y", "-qq", "ffmpeg"],
        ["apt", "update", "-qq"],
        ["apt", "install", "-y", "-qq", "ffmpeg"],
        [sys.executable, "-m", "pip", "install", "ffmpeg-static"],
    ]
    for i in range(0, len(methods), 2):
        if i + 1 < len(methods):
            try:
                subprocess.run(methods[i], capture_output=True, timeout=30)
                subprocess.run(methods[i + 1], capture_output=True, timeout=60)
                if shutil.which("ffprobe"):
                    print("ffmpeg installed successfully!")
                    return
            except Exception:
                continue
    print("WARNING: Could not install ffmpeg - video frame extraction will not work")

ensure_ffmpeg()

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR / "douyin-video" / "scripts"))
sys.path.insert(0, str(BASE_DIR))

import uvicorn
from mcp_server import mcp

# 创建 Streamable HTTP 应用
app = mcp.http_app(path="/", transport="streamable-http")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"Starting Social MCP Server (Streamable HTTP) on {host}:{port}")
    print(f"  支持的平台: 抖音(Douyin) + 小红书(Xiaohongshu)")
    print(f"  可用工具: parse_share_link, analyze_share_images, analyze_share_video, extract_share_text, get_share_download_link")
    uvicorn.run(app, host=host, port=port)
