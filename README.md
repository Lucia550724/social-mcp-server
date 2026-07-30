# Social MCP Server 🌐

统一的社交媒体 MCP 服务器，支持 **抖音 (Douyin)** 和 **小红书 (Xiaohongshu/RedNote)**。

一个 URL 搞定所有平台的内容解析！自动识别链接来源，提取结构化数据。

## 功能

### 抖音
- 视频信息解析（标题、作者、音乐等）
- 无水印视频下载链接
- 视频画面分析（ffmpeg 抽帧 + 千问VL）
- 图文/视频封面图片分析
- 语音转文字（ASR）

### 小红书
- 笔记内容解析（标题、正文、标签、作者）
- 互动数据（点赞、收藏、评论数）
- 图片提取与分析
- 视频链接提取与分析

## 可用工具

| 工具 | 说明 |
|:---|:---|
| `parse_share_link` | 🎯 核心工具！自动识别平台，返回结构化内容 |
| `analyze_share_images` | 🖼️ 分析图片内容（画面描述+文字提取） |
| `analyze_share_video` | 🎬 视频抽帧+画面分析 |
| `extract_share_text` | 📝 提取文字内容（抖音ASR/小红书正文） |
| `get_share_download_link` | ⬇️ 获取下载链接 |

## 部署

### Railway

1. Fork 此仓库
2. 在 Railway 创建新项目，连接此仓库
3. 设置环境变量：
   - `DASHSCOPE_API_KEY` - 阿里云百炼 API Key（用于图像/视频分析）
   - `XHS_COOKIE` - (可选) 小红书 Cookie，提高解析成功率
   - `API_KEY` - (兼容) 硅基流动 API Key

### 本地开发

```bash
pip install -r requirements.txt
python run_mcp.py
```

## 配置

### 橘瓣 (OrangeChat)

在 MCP 配置中添加：
- 名称: `社交平台解析`
- 类型: `Streamable HTTP`
- URL: `https://your-deployment.up.railway.app/`

## License

MIT
