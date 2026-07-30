#!/usr/bin/env python3
"""Railway 部署入口 - 同时提供 WebUI 和 MCP Server"""
import os
from fastapi import FastAPI
import uvicorn

from mcp_server import mcp

app = FastAPI(title="Social MCP Server")

@app.get("/")
async def root():
    return {"status": "ok", "message": "Social MCP Server is running (Douyin + Xiaohongshu)"}

# 挂载 fastmcp 的 ASGI 应用到 /mcp (Streamable HTTP)
app.mount("/mcp", mcp.http_app())

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
