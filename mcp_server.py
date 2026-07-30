#!/usr/bin/env python3
"""
Social MCP Server - Unified server for Douyin & Xiaohongshu
Supports: parse_share_link, analyze_share_images, analyze_share_video,
          extract_share_text, get_share_download_link
"""
import os
import re
import json
import sys
import subprocess
import tempfile
import shutil
import base64
import urllib.request
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent / "douyin-video" / "scripts"))

from fastmcp import FastMCP
from douyin_downloader import get_video_info, extract_text, HEADERS as DY_HEADERS
import requests

mcp = FastMCP("Social MCP Server")

DASHSCOPE_VISION_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DASHSCOPE_VISION_MODEL = "qwen3-vl-plus"
DASHSCOPE_MULTIMODAL_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-conversation"

# ── Xiaohongshu constants ──────────────────────────────
XHS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

# ── Image analysis prompt ───────────────────────────────
IMAGE_ANALYZE_PROMPT = (
    "请综合描述这张图片的完整内容，包括两个方面：\n"
    "1. 【画面描述】图片中有什么物体、人物、场景、颜色、风格，整体的构图和氛围\n"
    "2. 【文字提取】图片中出现的所有文字内容，包括标题、正文、贴纸、水印、品牌标识等\n"
    "请用中文详细回答，先描述画面再给出文字内容"
)

VIDEO_ANALYZE_PROMPT = (
    "请分析这组视频截图，详细描述：\n"
    "1. 画面中的场景和人物/动物\n"
    "2. 任何文字内容（标题、字幕、贴纸、水印）\n"
    "3. 风格、氛围和主题\n"
    "4. 动作、表情、互动\n"
    "请用中文详细描述"
)


# ══════════════════════════════════════════════════════════
#  Helper functions
# ══════════════════════════════════════════════════════════

def _api_key() -> str:
    return os.getenv("DASHSCOPE_API_KEY") or os.getenv("API_KEY") or ""


def _find_bin(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    for p in ["/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/usr/local/opt/ffmpeg/bin"]:
        candidate = Path(p) / name
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"{name} not found.")


def _detect_platform(url: str) -> str:
    """Auto-detect platform from a share URL."""
    if not url:
        return "unknown"
    url_lower = url.lower()
    if any(d in url_lower for d in ["douyin.com", "iesdouyin.com", "douyin"]):
        return "douyin"
    if any(x in url_lower for x in ["xiaohongshu.com", "xhslink.com", "xhslink.cn", "rednote"]):
        return "xiaohongshu"
    return "unknown"


# ══════════════════════════════════════════════════════════
#  Xiaohongshu parser (no-browser, __INITIAL_STATE__)
# ══════════════════════════════════════════════════════════

def _xhs_http_get(url: str, cookie: str = "", timeout: int = 20) -> tuple[str, str]:
    """Fetch Xiaohongshu page HTML with mobile UA."""
    headers = {"User-Agent": XHS_UA, "Referer": "https://www.xiaohongshu.com/"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), resp.geturl()


def _xhs_balanced_json(s: str) -> str | None:
    """Find balanced JSON object starting from the first '{'."""
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[:i + 1]
    return None


def _xhs_extract_initial_state(html: str) -> dict | None:
    """Extract window.__INITIAL_STATE__ JSON from page HTML."""
    idx = html.find("__INITIAL_STATE__")
    if idx == -1:
        return None
    brace = html.find("{", idx)
    if brace == -1:
        return None
    raw = _xhs_balanced_json(html[brace:])
    if not raw:
        return None
    raw = re.sub(r"([:,\[]\s*)undefined\b", r"\1null", raw)
    try:
        return json.loads(raw)
    except Exception:
        return None


def _xhs_find_note(state: dict) -> dict | None:
    """Locate the note detail object within __INITIAL_STATE__."""
    try:
        note_map = state["note"]["noteDetailMap"]
        for v in note_map.values():
            if isinstance(v, dict) and v.get("note"):
                return v["note"]
    except Exception:
        pass

    found = [None]
    def walk(o):
        if found[0] is not None:
            return
        if isinstance(o, dict):
            if "interactInfo" in o and ("desc" in o or "title" in o):
                found[0] = o
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(state)
    return found[0]


def _xhs_parse_note(note: dict) -> dict:
    """Extract structured data from note dict."""
    img_list = note.get("imageList") or []
    images = []
    for img in img_list:
        u = img.get("urlDefault") or img.get("url") or ""
        if not u:
            for info in (img.get("infoList") or []):
                if info.get("url"):
                    u = info["url"]
                    break
        if u:
            images.append(u)
    interact = note.get("interactInfo") or {}
    user = note.get("user") or {}
    
    video_url = None
    video = note.get("video") or {}
    stream = (video.get("media") or {}).get("stream") or {}
    for codec in ("h264", "h265", "h266", "av1"):
        for item in (stream.get(codec) or []):
            if item.get("masterUrl"):
                video_url = item["masterUrl"]
                break
        if video_url:
            break
    
    return {
        "title": (note.get("title") or "").strip(),
        "desc": (note.get("desc") or "").strip().replace("\xa0", " "),
        "author": (user.get("nickName") or user.get("nickname") or "").strip(),
        "tags": [t.get("name", "") for t in (note.get("tagList") or []) if t.get("name")],
        "likes": interact.get("likedCount", 0),
        "collects": interact.get("collectedCount", 0),
        "comments": interact.get("commentCount", 0),
        "images": images,
        "image_count": len(images),
        "video_url": video_url,
        "note_type": "video" if (note.get("type") == "video" or video_url) else "image",
        "platform": "xiaohongshu",
    }


def _xhs_parse_meta(html: str) -> dict | None:
    """Fallback: parse og:meta tags."""
    def meta(prop):
        m = re.search(
            rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]+content=["\'](.*?)["\']',
            html, re.IGNORECASE)
        return (m.group(1).strip() if m else "")
    title = meta("og:title")
    desc = meta("og:description")
    if not title and not desc:
        return None
    return {"title": title, "desc": desc, "author": "", "tags": [],
            "likes": 0, "collects": 0, "comments": 0, "images": [],
            "image_count": 0, "video_url": None, "note_type": "image",
            "platform": "xiaohongshu"}


def parse_xiaohongshu_note(url: str) -> dict:
    """Parse Xiaohongshu note from share link using __INITIAL_STATE__ extraction."""
    cookie = os.getenv("XHS_COOKIE", "")
    try:
        html, final_url = _xhs_http_get(url, cookie)
    except Exception as e:
        return {"status": "error", "error": f"请求失败: {e}", "platform": "xiaohongshu"}

    if "请通过小红书" in html or "verify" in final_url.lower():
        return {"status": "error", "error": "被风控拦截，请设置 XHS_COOKIE 环境变量", "platform": "xiaohongshu"}

    state = _xhs_extract_initial_state(html)
    data = None
    if state:
        note = _xhs_find_note(state)
        if note:
            data = _xhs_parse_note(note)
    if not data or not (data.get("desc") or data.get("title")):
        data = _xhs_parse_meta(html) or data
    if not data or not (data.get("desc") or data.get("title")):
        return {"status": "error", "error": "无法解析笔记内容", "platform": "xiaohongshu"}

    result = {"status": "success", "platform": "xiaohongshu", "url": final_url}
    result.update(data)
    return result


# ══════════════════════════════════════════════════════════
#  Douyin parser (existing logic, extracted)
# ══════════════════════════════════════════════════════════

def _douyin_rich_parse(share_text: str) -> dict:
    """Parse Douyin content from share link."""
    urls = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', share_text)
    if not urls:
        raise ValueError("未找到有效的分享链接")
    share_url = urls[0]
    share_response = requests.get(share_url, headers=DY_HEADERS)
    video_id = share_response.url.split("?")[0].strip("/").split("/")[-1]
    response = requests.get(f"https://www.iesdouyin.com/share/video/{video_id}", headers=DY_HEADERS)
    response.raise_for_status()
    match = re.search(r"window\\._ROUTER_DATA\\s*=\\s*(.*?)</script>", response.text, re.DOTALL)
    if not match:
        raise ValueError("从HTML中解析视频信息失败")
    json_data = json.loads(match.group(1).strip())
    ld = json_data["loaderData"]
    key = "video_(id)/page" if "video_(id)/page" in ld else ("note_(id)/page" if "note_(id)/page" in ld else None)
    if not key:
        raise Exception("无法从JSON中解析内容")
    item = ld[key]["videoInfoRes"]["item_list"][0]
    desc = re.sub(r'[\\\\/:*?"<>|]', '_', item.get("desc", "").strip() or f"douyin_{video_id}")

    result = {
        "status": "success",
        "platform": "douyin",
        "video_id": video_id,
        "title": desc,
        "content_type": "video",
        "images": [],
        "image_count": 0,
        "video_url": None,
        "cover_url": None,
        "author": None,
        "music": None,
    }

    if "author" in item:
        a = item["author"]
        av = a.get("avatar_thumb", {}).get("url_list")
        result["author"] = {
            "nickname": a.get("nickname", ""),
            "avatar": av[0] if av else None,
            "unique_id": a.get("unique_id", ""),
        }
    if isinstance(item.get("video"), dict):
        cv = item["video"].get("cover")
        if isinstance(cv, dict):
            cl = cv.get("url_list")
            if cl:
                result["cover_url"] = cl[0]
        pa = item["video"].get("play_addr")
        if isinstance(pa, dict):
            vl = pa.get("url_list")
            if vl:
                result["video_url"] = vl[0].replace("playwm", "play")
    imgs = item.get("images")
    if isinstance(imgs, list):
        result["content_type"] = "image_post"
        urls = []
        for img in imgs:
            if isinstance(img, dict):
                u = img.get("url_list", img.get("display_url", []))
                if isinstance(u, list) and u:
                    urls.append(u[0])
                elif isinstance(u, str):
                    urls.append(u)
        if urls:
            result["images"] = urls
            result["image_count"] = len(urls)
    mus = item.get("music")
    if isinstance(mus, dict):
        ct = mus.get("cover_thumb", {})
        cl = ct.get("url_list") if isinstance(ct, dict) else None
        result["music"] = {"title": mus.get("title", ""), "author": mus.get("author", ""), "cover": cl[0] if cl else None}

    result["url"] = share_url
    return result


# ══════════════════════════════════════════════════════════
#  Video download & frame extraction (shared)
# ══════════════════════════════════════════════════════════

def _download_video(video_url: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    p = Path(tmp.name)
    r = requests.get(video_url, headers=DY_HEADERS, stream=True, timeout=30)
    r.raise_for_status()
    with open(p, "wb") as f:
        for c in r.iter_content(8192):
            if c:
                f.write(c)
    return p


def _extract_frames(video_path: Path, num_frames: int = 5) -> list[Path]:
    ffprobe = _find_bin("ffprobe")
    ffmpeg = _find_bin("ffmpeg")
    frames = []
    dur_str = subprocess.check_output(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        timeout=15
    ).decode().strip()
    dur = float(dur_str) if dur_str else 30
    if dur <= 0:
        dur = 30
    interval = max(dur / (num_frames + 1), 1)
    for i in range(num_frames):
        fp = video_path.with_name(f"frame_{i}.jpg")
        subprocess.run(
            [ffmpeg, "-y", "-ss", str(interval * (i + 1)), "-i", str(video_path),
             "-vframes", "1", "-q:v", "2", str(fp)],
            capture_output=True, timeout=30,
        )
        if fp.exists():
            frames.append(fp)
    if not frames:
        raise Exception("未能提取到任何视频帧")
    return frames


def _image_to_base64(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"


def _call_vlm(image_urls: list[str], prompt: str, api_key: str) -> str:
    """Call DashScope VLM (qwen3-vl-plus) with image URLs."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    content = [{"type": "text", "text": prompt}]
    for img_url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": img_url}})
    resp = requests.post(
        DASHSCOPE_VISION_URL,
        headers=headers,
        json={
            "model": DASHSCOPE_VISION_MODEL,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 4096,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_vlm_video(frame_b64s: list[str], prompt: str, api_key: str, fps: float = 2.0) -> str:
    """Call DashScope native multimodal API with video frames (tells model these are sequential).

    使用 DashScope 原生 API（非 OpenAI 兼容），以 video 类型传入帧列表 + fps，
    让模型理解这些是连续的视频帧，而非独立图片。
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": DASHSCOPE_VISION_MODEL,
        "input": {
            "messages": [{
                "role": "user",
                "content": [
                    {"video": frame_b64s, "fps": fps},
                    {"text": prompt},
                ]
            }]
        },
        "parameters": {"max_tokens": 4096},
    }
    resp = requests.post(DASHSCOPE_MULTIMODAL_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()["output"]["choices"][0]["message"]["content"]


def _extract_audio_and_asr(video_path: Path) -> str:
    """Extract audio from video and run ASR. Returns transcribed text or empty string."""
    ffmpeg = _find_bin("ffmpeg")
    audio_path = video_path.with_suffix(".mp3")
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame",
             "-q:a", "4", str(audio_path)],
            capture_output=True, timeout=120, check=True,
        )
    except Exception:
        return ""  # 无法提取音频

    if not audio_path.exists() or audio_path.stat().st_size < 1024:
        return ""

    # 用 SiliconFlow ASR（和 douyin_downloader 同一套）
    api_key = os.getenv("API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return ""

    try:
        ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
        with open(audio_path, "rb") as f:
            files = {"file": (audio_path.name, f, "audio/mpeg"), "model": (None, "FunAudioLLM/SenseVoiceSmall")}
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = requests.post(ASR_URL, files=files, headers=headers, timeout=120)
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            return text if text else ""
    except Exception:
        return ""
    finally:
        if audio_path.exists():
            audio_path.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════
#  MCP Tools
# ══════════════════════════════════════════════════════════

@mcp.tool()
def parse_share_link(share_link: str) -> str:
    """
    自动识别平台（抖音/小红书），返回结构化内容。
    支持抖音分享链接和小红书分享链接。
    
    返回：标题、正文、作者、标签、图片列表、视频链接、互动数据等
    """
    platform = _detect_platform(share_link)
    try:
        if platform == "xiaohongshu":
            result = parse_xiaohongshu_note(share_link)
            return json.dumps(result, ensure_ascii=False, indent=2)
        elif platform == "douyin":
            result = _douyin_rich_parse(share_link)
            return json.dumps(result, ensure_ascii=False, indent=2)
        else:
            # Try both
            xhs_result = parse_xiaohongshu_note(share_link)
            if xhs_result.get("status") == "success" and (xhs_result.get("title") or xhs_result.get("desc")):
                xhs_result["platform_detected_by"] = "xiaohongshu (fallback)"
                return json.dumps(xhs_result, ensure_ascii=False, indent=2)
            try:
                dy = _douyin_rich_parse(share_link)
                return json.dumps(dy, ensure_ascii=False, indent=2)
            except Exception:
                return json.dumps({"status": "error", "error": "无法识别链接平台"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def analyze_share_images(share_link: str) -> str:
    """
    分析分享链接中的图片内容，同时提取文字和描述画面。
    
    支持抖音图文/视频封面、小红书笔记图片。
    使用千问VL模型分析每张图片。
    
    参数:
    - share_link: 分享链接
    
    返回:
    - 每张图片的画面描述 + 文字内容
    """
    api_key = _api_key()
    if not api_key:
        return json.dumps({"status": "error", "error": "请设置 DASHSCOPE_API_KEY"}, ensure_ascii=False)
    
    try:
        platform = _detect_platform(share_link)
        
        if platform == "xiaohongshu":
            info = parse_xiaohongshu_note(share_link)
            if info.get("status") != "success":
                return json.dumps(info, ensure_ascii=False)
            image_urls = list(info.get("images", []))
        elif platform == "douyin":
            info = _douyin_rich_parse(share_link)
            image_urls = list(info.get("images", []))
            if info.get("cover_url"):
                image_urls.append(info["cover_url"])
        else:
            return json.dumps({"status": "error", "error": "无法识别链接平台"}, ensure_ascii=False)
        
        if not image_urls:
            return json.dumps({"status": "error", "error": "未找到可分析的图片"}, ensure_ascii=False)
        
        result = {
            "status": "success",
            "platform": platform,
            "title": info.get("title", ""),
            "author": info.get("author", {}).get("nickname", "") if isinstance(info.get("author"), dict) else info.get("author", ""),
            "image_count": len(image_urls),
            "image_analysis": [],
        }
        
        for i, img_url in enumerate(image_urls):
            analysis = _call_vlm([img_url], IMAGE_ANALYZE_PROMPT, api_key)
            result["image_analysis"].append({
                "image_index": i + 1,
                "url": img_url,
                "content": analysis,
            })
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


VIDEO_FULL_PROMPT = (
    "请分析这段视频的完整内容。详细描述：\n"
    "1. 画面中的场景和人物/动物，以及画面的变化过程\n"
    "2. 任何文字内容（标题、字幕、贴纸、水印）\n"
    "3. 风格、氛围和主题\n"
    "4. 动作、表情、互动，按时间顺序描述发生了什么事\n"
    "请用中文详细描述整段视频的完整脉络"
)


@mcp.tool()
def analyze_share_video(share_link: str, num_frames: int = 8) -> str:
    """
    从分享链接下载视频，进行双通道分析。
    
    双通道：
    1. 🖼️ 画面通道：抽帧+千问VL分析（DashScope原生视频帧模式，告知模型这是连续视频）
    2. 🎤 音频通道：提取音频+ASR转文字（有人声时）
    
    支持抖音视频和小红书视频。
    
    参数:
    - share_link: 分享链接
    - num_frames: 抽取帧数（默认8张，越多越详细但消耗更多额度）
    
    返回:
    - 画面分析 + 音频文字 + 视频完整脉络
    """
    api_key = _api_key()
    if not api_key:
        return json.dumps({"status": "error", "error": "请设置 DASHSCOPE_API_KEY"}, ensure_ascii=False)
    
    video_path = None
    try:
        platform = _detect_platform(share_link)
        
        # ── 1. 解析链接获取视频URL ─────────────────
        if platform == "xiaohongshu":
            info = parse_xiaohongshu_note(share_link)
            if info.get("status") != "success":
                return json.dumps(info, ensure_ascii=False)
            video_url = info.get("video_url")
        elif platform == "douyin":
            info = _douyin_rich_parse(share_link)
            video_url = info.get("video_url")
        else:
            return json.dumps({"status": "error", "error": "无法识别链接平台"}, ensure_ascii=False)
        
        if not video_url:
            return json.dumps({"status": "error", "error": "该内容没有视频可下载"}, ensure_ascii=False)
        
        result = {
            "status": "success",
            "platform": platform,
            "title": info.get("title", ""),
            "author": info.get("author", {}).get("nickname", "") if isinstance(info.get("author"), dict) else info.get("author", ""),
        }
        
        if info.get("music"):
            result["background_music"] = info["music"]["title"] if isinstance(info.get("music"), dict) else info["music"]
        
        # ── 2. 下载视频 ────────────────────────────
        video_path = _download_video(video_url)
        
        # ── 3. 🖼️ 画面通道：抽帧 + DashScope视频帧模式 ──
        frames = _extract_frames(video_path, num_frames)
        result["frames_extracted"] = len(frames)
        
        frame_b64s = [_image_to_base64(f) for f in frames]
        
        # 计算fps：让模型知道这些帧之间的时间间隔
        dur_str = subprocess.check_output(
            [_find_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            timeout=15
        ).decode().strip()
        dur = float(dur_str) if dur_str else 30
        fps = round(num_frames / max(dur, 1), 1)
        
        # 使用DashScope原生视频帧模式（告知模型这些是连续视频帧）
        analysis = _call_vlm_video(frame_b64s, VIDEO_FULL_PROMPT, api_key, fps=fps)
        result["visual_analysis"] = analysis
        
        # ── 4. 🎤 音频通道：提取 + ASR ──────────────
        transcript = _extract_audio_and_asr(video_path)
        if transcript:
            result["audio_transcript"] = transcript
            result["has_audio"] = True
        else:
            result["has_audio"] = False
            result["audio_note"] = "未检测到有效人声（可能是纯BGM/环境音视频）"
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)
    finally:
        if video_path and video_path.exists():
            video_path.unlink(missing_ok=True)
            for f in Path(video_path.parent).glob("frame_*.jpg"):
                f.unlink(missing_ok=True)
            audio_p = video_path.with_suffix(".mp3")
            if audio_p.exists():
                audio_p.unlink(missing_ok=True)


@mcp.tool()
def extract_share_text(share_link: str) -> str:
    """
    提取分享内容中的文字信息。
    
    抖音：视频语音转文字（ASR）
    小红书：笔记正文文字
    
    参数:
    - share_link: 分享链接
    
    返回:
    - 提取的文字内容
    """
    api_key = _api_key()
    if not api_key:
        return json.dumps({"status": "error", "error": "请设置 DASHSCOPE_API_KEY"}, ensure_ascii=False)
    
    try:
        platform = _detect_platform(share_link)
        
        if platform == "xiaohongshu":
            info = parse_xiaohongshu_note(share_link)
            if info.get("status") != "success":
                return json.dumps(info, ensure_ascii=False)
            text = info.get("desc", "")
            tags = info.get("tags", [])
            result = {
                "status": "success",
                "platform": "xiaohongshu",
                "title": info.get("title", ""),
                "text": text,
                "tags": tags,
                "word_count": len(text),
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        
        elif platform == "douyin":
            try:
                text_result = extract_text(share_link, api_key=api_key, show_progress=False)
                result = {
                    "status": "success",
                    "platform": "douyin",
                    "video_id": text_result.get("video_info", {}).get("video_id", ""),
                    "title": text_result.get("video_info", {}).get("title", ""),
                    "text": text_result.get("text", ""),
                }
                return json.dumps(result, ensure_ascii=False, indent=2)
            except Exception as e:
                info = _douyin_rich_parse(share_link)
                return json.dumps({
                    "status": "success",
                    "platform": "douyin",
                    "title": info.get("title", ""),
                    "text": info.get("title", ""),
                    "note": "视频ASR转录需要API_KEY和dashscope支持，已返回基础文本",
                }, ensure_ascii=False, indent=2)
        else:
            return json.dumps({"status": "error", "error": "无法识别链接平台"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def get_share_download_link(share_link: str) -> str:
    """
    获取分享内容的下载链接。
    
    支持抖音视频下载链接。
    
    参数:
    - share_link: 分享链接
    
    返回:
    - 下载链接信息
    """
    try:
        platform = _detect_platform(share_link)
        
        if platform == "xiaohongshu":
            info = parse_xiaohongshu_note(share_link)
            if info.get("status") != "success":
                return json.dumps(info, ensure_ascii=False)
            result = {
                "status": "success",
                "platform": "xiaohongshu",
                "title": info.get("title", ""),
                "images": info.get("images", []),
                "image_count": info.get("image_count", 0),
            }
            if info.get("video_url"):
                result["video_url"] = info["video_url"]
            return json.dumps(result, ensure_ascii=False, indent=2)
        
        elif platform == "douyin":
            info = get_video_info(share_link)
            result = {
                "status": "success",
                "platform": "douyin",
                "video_id": info["video_id"],
                "title": info["title"],
                "download_url": info["url"],
            }
            return json.dumps(result, ensure_ascii=False, indent=2)
        else:
            return json.dumps({"status": "error", "error": "无法识别链接平台"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)
