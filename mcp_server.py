#!/usr/bin/env python3
"""... same header ..."""
import os, re, json, sys, subprocess, tempfile, shutil, base64, urllib.request
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent / "douyin-video" / "scripts"))
from fastmcp import FastMCP
from douyin_downloader import get_video_info, extract_text, HEADERS as DY_HEADERS
import requests

mcp = FastMCP("Social MCP Server")
DASHSCOPE_VISION_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
DASHSCOPE_VISION_MODEL = "qwen3-vl-plus"

XHS_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

IMAGE_ANALYZE_PROMPT = (
    "请综合描述这张图片的完整内容，包括两个方面：\n"
    "1. 【画面描述】图片中有什么物体、人物、场景、颜色、风格，整体的构图和氛围\n"
    "2. 【文字提取】图片中出现的所有文字内容，包括标题、正文、贴纸、水印、品牌标识等\n"
    "请用中文详细回答，先描述画面再给出文字内容"
)

def _api_key():
    return os.getenv("DASHSCOPE_API_KEY") or os.getenv("API_KEY") or ""

def _find_bin(name):
    p = shutil.which(name)
    if p: return p
    for d in ["/usr/bin","/usr/local/bin","/opt/homebrew/bin","/usr/local/opt/ffmpeg/bin"]:
        c = Path(d)/name
        if c.exists(): return str(c)
    raise FileNotFoundError(f"{name} not found.")

def _detect_platform(url):
    u = url.lower()
    if any(d in u for d in ["douyin.com","iesdouyin.com","douyin"]): return "douyin"
    if any(x in u for x in ["xiaohongshu.com","xhslink.com","xhslink.cn","rednote"]): return "xiaohongshu"
    return "unknown"

# ── Xiaohongshu parser ──────────────────────────────────
def _xhs_http_get(url, cookie="", timeout=20):
    h = {"User-Agent": XHS_UA, "Referer": "https://www.xiaohongshu.com/"}
    if cookie: h["Cookie"] = cookie
    r = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.read().decode("utf-8","replace"), resp.geturl()

def _xhs_balanced_json(s):
    d = ins = esc = 0
    for i,ch in enumerate(s):
        if esc: esc=False; continue
        if ch=="\\": esc=True; continue
        if ch=='"': ins=not ins; continue
        if ins: continue
        if ch=="{": d+=1
        elif ch=="}": d-=1
        if d==0: return s[:i+1]
    return None

def _xhs_extract_initial_state(html):
    i = html.find("__INITIAL_STATE__")
    if i==-1: return None
    b = html.find("{", i)
    if b==-1: return None
    r = _xhs_balanced_json(html[b:])
    if not r: return None
    try: return json.loads(re.sub(r"([:,\[]\s*)undefined\b",r"\1null",r))
    except: return None

def _xhs_find_note(state):
    try:
        for v in state["note"]["noteDetailMap"].values():
            if isinstance(v,dict) and v.get("note"): return v["note"]
    except: pass
    f=[None]
    def w(o):
        if f[0]: return
        if isinstance(o,dict):
            if "interactInfo" in o and ("desc" in o or "title" in o): f[0]=o; return
            for v in o.values(): w(v)
        elif isinstance(o,list):
            for v in o: w(v)
    w(state)
    return f[0]

def _xhs_parse_note(note):
    imgs=[]
    for img in (note.get("imageList") or []):
        u = img.get("urlDefault") or img.get("url") or ""
        if not u:
            for info in (img.get("infoList") or []):
                if info.get("url"): u=info["url"]; break
        if u: imgs.append(u)
    it = note.get("interactInfo") or {}
    user = note.get("user") or {}
    vu = None
    v = note.get("video") or {}
    s = (v.get("media") or {}).get("stream") or {}
    for c in ("h264","h265","h266","av1"):
        for item in (s.get(c) or []):
            if item.get("masterUrl"): vu=item["masterUrl"]; break
        if vu: break
    return {
        "title": (note.get("title") or "").strip(),
        "desc": (note.get("desc") or "").strip().replace("\xa0"," "),
        "author": (user.get("nickName") or user.get("nickname") or "").strip(),
        "tags": [t.get("name","") for t in (note.get("tagList") or []) if t.get("name")],
        "likes": it.get("likedCount",0), "collects": it.get("collectedCount",0),
        "comments": it.get("commentCount",0), "images": imgs, "image_count": len(imgs),
        "video_url": vu, "note_type": "video" if (note.get("type")=="video" or vu) else "image",
        "platform": "xiaohongshu",
    }

def parse_xiaohongshu_note(url):
    c = os.getenv("XHS_COOKIE","")
    try: html, fu = _xhs_http_get(url, c)
    except Exception as e: return {"status":"error","error":f"请求失败: {e}","platform":"xiaohongshu"}
    if "请通过小红书" in html or "verify" in fu.lower():
        return {"status":"error","error":"被风控拦截","platform":"xiaohongshu"}
    st = _xhs_extract_initial_state(html)
    data = None
    if st:
        n = _xhs_find_note(st)
        if n: data = _xhs_parse_note(n)
    if not data or not (data.get("desc") or data.get("title")):
        def meta(p):
            m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(p)}["\'][^>]+content=["\'](.*?)["\']', html, re.I)
            return m.group(1).strip() if m else ""
        t,d = meta("og:title"), meta("og:description")
        if t or d: data = {"title":t,"desc":d,"author":"","tags":[],"likes":0,"collects":0,"comments":0,"images":[],"image_count":0,"video_url":None,"note_type":"image","platform":"xiaohongshu"}
    if not data or not (data.get("desc") or data.get("title")):
        return {"status":"error","error":"无法解析笔记内容","platform":"xiaohongshu"}
    r = {"status":"success","platform":"xiaohongshu","url":fu}; r.update(data); return r

# ── Douyin parser ───────────────────────────────────────
def _douyin_rich_parse(t):
    us = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', t)
    if not us: raise ValueError("未找到有效的分享链接")
    su = us[0]
    sr = requests.get(su, headers=DY_HEADERS)
    vid = sr.url.split("?")[0].strip("/").split("/")[-1]
    r = requests.get(f"https://www.iesdouyin.com/share/video/{vid}", headers=DY_HEADERS); r.raise_for_status()
    m = re.search(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", r.text, re.DOTALL)
    if not m: raise ValueError("解析失败")
    jd = json.loads(m.group(1).strip())
    ld = jd["loaderData"]
    k = "video_(id)/page" if "video_(id)/page" in ld else ("note_(id)/page" if "note_(id)/page" in ld else None)
    if not k: raise Exception("解析失败")
    item = ld[k]["videoInfoRes"]["item_list"][0]
    desc = re.sub(r'[\\/:*?"<>|]','_', item.get("desc","").strip() or f"douyin_{vid}")
    res = {"status":"success","platform":"douyin","video_id":vid,"title":desc,
           "content_type":"video","images":[],"image_count":0,"video_url":None,
           "cover_url":None,"author":None,"music":None}
    if "author" in item:
        a=item["author"]; av=a.get("avatar_thumb",{}).get("url_list")
        res["author"]={"nickname":a.get("nickname",""),"avatar":av[0] if av else None,"unique_id":a.get("unique_id","")}
    if isinstance(item.get("video"), dict):
        cv=item["video"].get("cover")
        if isinstance(cv,dict):
            cl=cv.get("url_list")
            if cl: res["cover_url"]=cl[0]
        pa=item["video"].get("play_addr")
        if isinstance(pa,dict):
            vl=pa.get("url_list")
            if vl: res["video_url"]=vl[0].replace("playwm","play")
    imgs=item.get("images")
    if isinstance(imgs,list):
        res["content_type"]="image_post"
        us2=[]
        for img in imgs:
            if isinstance(img,dict):
                u=img.get("url_list",img.get("display_url",[]))
                if isinstance(u,list) and u: us2.append(u[0])
                elif isinstance(u,str): us2.append(u)
        if us2: res["images"]=us2; res["image_count"]=len(us2)
    mus=item.get("music")
    if isinstance(mus,dict):
        ct=mus.get("cover_thumb",{}); cl=ct.get("url_list") if isinstance(ct,dict) else None
        res["music"]={"title":mus.get("title",""),"author":mus.get("author",""),"cover":cl[0] if cl else None}
    res["url"]=su; return res

# ── Video download & frame extraction ───────────────────
def _download_video(vu):
    p=Path(tempfile.NamedTemporaryFile(suffix=".mp4",delete=False).name)
    r=requests.get(vu,headers=DY_HEADERS,stream=True,timeout=30); r.raise_for_status()
    with open(p,"wb") as f:
        for c in r.iter_content(8192):
            if c: f.write(c)
    return p

def _extract_frames(vp, n=5):
    ffp=_find_bin("ffprobe"); ffe=_find_bin("ffmpeg"); fs=[]
    ds=subprocess.check_output([ffp,"-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(vp)],timeout=15).decode().strip()
    dur=float(ds) if ds else 30
    if dur<=0: dur=30
    iv=max(dur/(n+1),1)
    for i in range(n):
        fp=vp.with_name(f"frame_{i}.jpg")
        subprocess.run([ffe,"-y","-ss",str(iv*(i+1)),"-i",str(vp),"-vframes","1","-q:v","2",str(fp)],capture_output=True,timeout=30)
        if fp.exists(): fs.append(fp)
    if not fs: raise Exception("未能提取到任何视频帧")
    return fs

def _image_to_base64(p):
    with open(p,"rb") as f: return f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"

def _call_vlm(urls, prompt, ak):
    h={"Authorization":f"Bearer {ak}","Content-Type":"application/json"}
    ct=[{"type":"text","text":prompt}]
    for u in urls: ct.append({"type":"image_url","image_url":{"url":u}})
    r=requests.post(DASHSCOPE_VISION_URL,headers=h,json={"model":DASHSCOPE_VISION_MODEL,"messages":[{"role":"user","content":ct}],"max_tokens":4096},timeout=120)
    r.raise_for_status(); return r.json()["choices"][0]["message"]["content"]

def _extract_audio_and_asr(vp):
    ffe=_find_bin("ffmpeg"); ap=vp.with_suffix(".mp3")
    try: subprocess.run([ffe,"-y","-i",str(vp),"-vn","-acodec","libmp3lame","-q:a","4",str(ap)],capture_output=True,timeout=120,check=True)
    except: return ""
    if not ap.exists() or ap.stat().st_size<1024: return ""
    ak=os.getenv("API_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not ak: return ""
    try:
        with open(ap,"rb") as f:
            fs={"file":(ap.name,f,"audio/mpeg"),"model":(None,"FunAudioLLM/SenseVoiceSmall")}
            r=requests.post("https://api.siliconflow.cn/v1/audio/transcriptions",files=fs,headers={"Authorization":f"Bearer {ak}"},timeout=120)
            r.raise_for_status(); t=r.json().get("text","").strip(); return t if t else ""
    except: return ""
    finally:
        if ap.exists(): ap.unlink(missing_ok=True)

# ══════════════════════════════════════════════════════════
#  MCP Tools
# ══════════════════════════════════════════════════════════

@mcp.tool()
def parse_share_link(share_link: str) -> str:
    """自动识别平台（抖音/小红书），返回结构化内容。"""
    p=_detect_platform(share_link)
    try:
        if p=="xiaohongshu": return json.dumps(parse_xiaohongshu_note(share_link),ensure_ascii=False,indent=2)
        if p=="douyin": return json.dumps(_douyin_rich_parse(share_link),ensure_ascii=False,indent=2)
        # fallback
        x=parse_xiaohongshu_note(share_link)
        if x.get("status")=="success" and (x.get("title") or x.get("desc")):
            x["platform_detected_by"]="xiaohongshu (fallback)"
            return json.dumps(x,ensure_ascii=False,indent=2)
        return json.dumps(_douyin_rich_parse(share_link),ensure_ascii=False,indent=2)
    except Exception as e: return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)

@mcp.tool()
def analyze_share_images(share_link: str) -> str:
    """分析图片内容（画面描述+文字提取）"""
    ak=_api_key()
    if not ak: return json.dumps({"status":"error","error":"请设置 DASHSCOPE_API_KEY"},ensure_ascii=False)
    try:
        p=_detect_platform(share_link)
        if p=="xiaohongshu":
            info=parse_xiaohongshu_note(share_link)
            if info.get("status")!="success": return json.dumps(info,ensure_ascii=False)
            us=list(info.get("images",[]))
        elif p=="douyin":
            info=_douyin_rich_parse(share_link)
            us=list(info.get("images",[]))
            if info.get("cover_url"): us.append(info["cover_url"])
        else: return json.dumps({"status":"error","error":"无法识别链接平台"},ensure_ascii=False)
        if not us: return json.dumps({"status":"error","error":"未找到图片"},ensure_ascii=False)
        r={"status":"success","platform":p,"title":info.get("title",""),
           "author":info.get("author",{}).get("nickname","") if isinstance(info.get("author"),dict) else info.get("author",""),
           "image_count":len(us),"image_analysis":[]}
        for i,u in enumerate(us):
            r["image_analysis"].append({"image_index":i+1,"url":u,"content":_call_vlm([u],IMAGE_ANALYZE_PROMPT,ak)})
        return json.dumps(r,ensure_ascii=False,indent=2)
    except Exception as e: return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)

VIDEO_FULL_PROMPT = (
    "请分析这段视频的完整内容。以下是一组按时间顺序截取的视频帧（共8张），"
    "它们是从一段视频中均匀抽取的关键帧。请根据这些帧推断出视频的完整过程。详细描述：\n"
    "1. 画面中的场景和人物/动物，以及画面随时间的变化过程\n"
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
    1. 🖼️ 画面通道：抽帧+千问VL分析画面内容
    2. 🎤 音频通道：提取音频+ASR转文字（有人声时）
    
    参数:
    - share_link: 分享链接
    - num_frames: 抽取帧数（默认8张）
    
    返回: 画面分析 + 音频文字 + 视频完整脉络
    """
    ak=_api_key()
    if not ak: return json.dumps({"status":"error","error":"请设置 DASHSCOPE_API_KEY"},ensure_ascii=False)
    vp=None
    try:
        p=_detect_platform(share_link)
        if p=="xiaohongshu":
            info=parse_xiaohongshu_note(share_link)
            if info.get("status")!="success": return json.dumps(info,ensure_ascii=False)
            vu=info.get("video_url")
        elif p=="douyin":
            info=_douyin_rich_parse(share_link); vu=info.get("video_url")
        else: return json.dumps({"status":"error","error":"无法识别链接平台"},ensure_ascii=False)
        if not vu: return json.dumps({"status":"error","error":"没有视频可下载"},ensure_ascii=False)
        
        r={"status":"success","platform":p,"title":info.get("title",""),
           "author":info.get("author",{}).get("nickname","") if isinstance(info.get("author"),dict) else info.get("author","")}
        if info.get("music"):
            r["background_music"]=info["music"]["title"] if isinstance(info.get("music"),dict) else info["music"]
        
        vp=_download_video(vu)
        
        # 🖼️ 画面通道
        frames=_extract_frames(vp,num_frames)
        r["frames_extracted"]=len(frames)
        b64s=[_image_to_base64(f) for f in frames]
        
        # 加强提示：告诉模型这些是连续视频帧
        analysis=_call_vlm(b64s, VIDEO_FULL_PROMPT, ak)
        r["visual_analysis"]=analysis
        
        # 🎤 音频通道
        transcript=_extract_audio_and_asr(vp)
        if transcript:
            r["audio_transcript"]=transcript; r["has_audio"]=True
        else:
            r["has_audio"]=False; r["audio_note"]="未检测到有效人声（可能是纯BGM/环境音视频）"
        
        return json.dumps(r,ensure_ascii=False,indent=2)
    except Exception as e: return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)
    finally:
        if vp and vp.exists():
            vp.unlink(missing_ok=True)
            for f in Path(vp.parent).glob("frame_*.jpg"): f.unlink(missing_ok=True)
            ap=vp.with_suffix(".mp3")
            if ap.exists(): ap.unlink(missing_ok=True)

@mcp.tool()
def extract_share_text(share_link: str) -> str:
    """提取文字内容（抖音ASR/小红书正文）"""
    ak=_api_key()
    if not ak: return json.dumps({"status":"error","error":"请设置 DASHSCOPE_API_KEY"},ensure_ascii=False)
    try:
        p=_detect_platform(share_link)
        if p=="xiaohongshu":
            info=parse_xiaohongshu_note(share_link)
            if info.get("status")!="success": return json.dumps(info,ensure_ascii=False)
            return json.dumps({"status":"success","platform":"xiaohongshu","title":info.get("title",""),
                               "text":info.get("desc",""),"tags":info.get("tags",[]),"word_count":len(info.get("desc",""))},
                              ensure_ascii=False,indent=2)
        elif p=="douyin":
            try:
                tr=extract_text(share_link,api_key=ak,show_progress=False)
                return json.dumps({"status":"success","platform":"douyin","video_id":tr.get("video_info",{}).get("video_id",""),
                                   "title":tr.get("video_info",{}).get("title",""),"text":tr.get("text","")},
                                  ensure_ascii=False,indent=2)
            except:
                info=_douyin_rich_parse(share_link)
                return json.dumps({"status":"success","platform":"douyin","title":info.get("title",""),
                                   "text":info.get("title",""),"note":"ASR不可用，返回基础文本"},ensure_ascii=False,indent=2)
        else: return json.dumps({"status":"error","error":"无法识别链接平台"},ensure_ascii=False)
    except Exception as e: return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)

@mcp.tool()
def get_share_download_link(share_link: str) -> str:
    """获取下载链接"""
    try:
        p=_detect_platform(share_link)
        if p=="xiaohongshu":
            info=parse_xiaohongshu_note(share_link)
            if info.get("status")!="success": return json.dumps(info,ensure_ascii=False)
            r={"status":"success","platform":"xiaohongshu","title":info.get("title",""),
               "images":info.get("images",[]),"image_count":info.get("image_count",0)}
            if info.get("video_url"): r["video_url"]=info["video_url"]
            return json.dumps(r,ensure_ascii=False,indent=2)
        elif p=="douyin":
            info=get_video_info(share_link)
            return json.dumps({"status":"success","platform":"douyin","video_id":info["video_id"],
                               "title":info["title"],"download_url":info["url"]},ensure_ascii=False,indent=2)
        else: return json.dumps({"status":"error","error":"无法识别链接平台"},ensure_ascii=False)
    except Exception as e: return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)
