#!/usr/bin/env python3
"""Social MCP Server"""
import os, re, json, sys, subprocess, tempfile, shutil, base64, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "douyin-video" / "scripts"))
from fastmcp import FastMCP
from douyin_downloader import get_video_info, extract_text, HEADERS as DY_HEADERS
import requests

mcp = FastMCP("Social MCP Server")
VL_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
VL_MODEL = "qwen3-vl-plus"
MM_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-conversation"
XHS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
IMG_PROMPT = "请综合描述这张图片的完整内容：\n1.【画面描述】物体、人物、场景、颜色、风格\n2.【文字提取】所有文字内容\n请用中文回答"
VID_PROMPT = "请分析这段视频的完整内容。描述：1.场景和人物变化 2.文字内容 3.风格氛围 4.动作互动，按时间顺序"

def ak():return os.getenv("DASHSCOPE_API_KEY")or os.getenv("API_KEY")or""
def fb(n):
    p=shutil.which(n)
    if p:return p
    for d in["/usr/bin","/usr/local/bin","/opt/homebrew/bin","/usr/local/opt/ffmpeg/bin"]:
        c=Path(d)/n
        if c.exists():return str(c)
    raise FileNotFoundError(f"{n} not found")
def dp(u):
    u=u.lower()
    if"douyin"in u:return"douyin"
    if any(x in u for x in["xiaohongshu","xhslink","rednote"]):return"xiaohongshu"
    return"unknown"

# ── Xiaohongshu ──
def xhs_g(u,c="",t=20):
    h={"User-Agent":XHS_UA,"Referer":"https://www.xiaohongshu.com/"}
    if c:h["Cookie"]=c
    with urllib.request.urlopen(urllib.request.Request(u,headers=h),timeout=t)as r:return r.read().decode("utf-8","replace"),r.geturl()
def xhs_b(s):
    d=ins=esc=0
    for i,ch in enumerate(s):
        if esc:esc=0;continue
        if ch=="\\":esc=1;continue
        if ch=='"':ins^=1;continue
        if ins:continue
        if ch=="{":d+=1
        elif ch=="}":d-=1
        if d==0:return s[:i+1]
    return None
def xhs_s(h):
    i=h.find("__INITIAL_STATE__")
    if i<0:return None
    b=h.find("{",i)
    if b<0:return None
    r=xhs_b(h[b:])
    if not r:return None
    try:return json.loads(re.sub(r"([:,\[\]\s*)undefined\b",r"\1null",r))
    except:return None
def xhs_n(s):
    try:
        for v in s["note"]["noteDetailMap"].values():
            if isinstance(v,dict)and v.get("note"):return v["note"]
    except:pass
    f=[None]
    def w(o):
        if f[0]:return
        if isinstance(o,dict):
            if"interactInfo"in o and("desc"in o or"title"in o):f[0]=o;return
            for v in o.values():w(v)
        elif isinstance(o,list):
            for v in o:w(v)
    w(s);return f[0]
def xhs_p(n):
    im=[]
    for img in(n.get("imageList")or[]):
        u=img.get("urlDefault")or img.get("url")or""
        if not u:
            for i in(img.get("infoList")or[]):
                if i.get("url"):u=i["url"];break
        if u:im.append(u)
    it=n.get("interactInfo")or{};us=n.get("user")or{};vu=None
    v=n.get("video")or{};s=(v.get("media")or{}).get("stream")or{}
    for c in("h264","h265","h266","av1"):
        for i in(s.get(c)or[]):
            if i.get("masterUrl"):vu=i["masterUrl"];break
        if vu:break
    return{"title":(n.get("title")or"").strip(),"desc":(n.get("desc")or"").strip().replace("\xa0"," "),
            "author":(us.get("nickName")or us.get("nickname")or"").strip(),
            "tags":[t.get("name","")for t in(n.get("tagList")or[])if t.get("name")],
            "likes":it.get("likedCount",0),"collects":it.get("collectedCount",0),"comments":it.get("commentCount",0),
            "images":im,"image_count":len(im),"video_url":vu,
            "note_type":"video"if(n.get("type")=="video"or vu)else"image","platform":"xiaohongshu"}
def parse_xhs(u):
    c=os.getenv("XHS_COOKIE","")
    try:h,f=xhs_g(u,c)
    except Exception as e:return{"status":"error","error":f"请求失败: {e}","platform":"xiaohongshu"}
    if"请通过小红书"in h or"verify"in f.lower():return{"status":"error","error":"被风控拦截","platform":"xiaohongshu"}
    st=xhs_s(h);d=None
    if st:
        n=xhs_n(st)
        if n:d=xhs_p(n)
    if not d or not(d.get("desc")or d.get("title")):
        def m(p):
            x=re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(p)}["\'][^>]+content=["\'](.*?)["\']',h,re.I)
            return x.group(1).strip()if x else""
        t,d2=m("og:title"),m("og:description")
        if t or d2:d={"title":t,"desc":d2,"author":"","tags":[],"likes":0,"collects":0,"comments":0,"images":[],"image_count":0,"video_url":None,"note_type":"image","platform":"xiaohongshu"}
    if not d or not(d.get("desc")or d.get("title")):return{"status":"error","error":"无法解析笔记内容","platform":"xiaohongshu"}
    r={"status":"success","platform":"xiaohongshu","url":f};r.update(d);return r

# ── Douyin ──
def dy_p(t):
    us=re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',t)
    if not us:raise ValueError("无效链接")
    sr=requests.get(us[0],headers=DY_HEADERS);vid=sr.url.split("?")[0].strip("/").split("/")[-1]
    r=requests.get(f"https://www.iesdouyin.com/share/video/{vid}",headers=DY_HEADERS);r.raise_for_status()
    m=re.search(r"window\\._ROUTER_DATA\\s*=\\s*(.*?)</script>",r.text,re.DOTALL)
    if not m:raise ValueError("解析失败")
    jd=json.loads(m.group(1).strip());ld=jd["loaderData"]
    k="video_(id)/page"if"video_(id)/page"in ld else("note_(id)/page"if"note_(id)/page"in ld else None)
    if not k:raise Exception("解析失败")
    item=ld[k]["videoInfoRes"]["item_list"][0];desc=re.sub(r'[\\\\/:*?"<>|]','_',item.get("desc","").strip()or f"douyin_{vid}")
    res={"status":"success","platform":"douyin","video_id":vid,"title":desc,"content_type":"video","images":[],"image_count":0,"video_url":None,"cover_url":None,"author":None,"music":None}
    if"author"in item:
        a=item["author"];av=a.get("avatar_thumb",{}).get("url_list")
        res["author"]={"nickname":a.get("nickname",""),"avatar":av[0]if av else None,"unique_id":a.get("unique_id","")}
    if isinstance(item.get("video"),dict):
        cv=item["video"].get("cover")
        if isinstance(cv,dict):
            cl=cv.get("url_list")
            if cl:res["cover_url"]=cl[0]
        pa=item["video"].get("play_addr")
        if isinstance(pa,dict):
            vl=pa.get("url_list")
            if vl:res["video_url"]=vl[0].replace("playwm","play")
    imgs=item.get("images")
    if isinstance(imgs,list):
        res["content_type"]="image_post"
        u2=[]
        for img in imgs:
            if isinstance(img,dict):
                u=img.get("url_list",img.get("display_url",[]))
                if isinstance(u,list)and u:u2.append(u[0])
                elif isinstance(u,str):u2.append(u)
        if u2:res["images"]=u2;res["image_count"]=len(u2)
    mus=item.get("music")
    if isinstance(mus,dict):
        ct=mus.get("cover_thumb",{});cl=ct.get("url_list")if isinstance(ct,dict)else None
        res["music"]={"title":mus.get("title",""),"author":mus.get("author",""),"cover":cl[0]if cl else None}
    res["url"]=us[0];return res

# ── Helpers ──
def dl_v(vu):
    p=Path(tempfile.NamedTemporaryFile(suffix=".mp4",delete=False).name)
    r=requests.get(vu,headers=DY_HEADERS,stream=True,timeout=30,allow_redirects=True);r.raise_for_status()
    with open(p,"wb")as f:
        for c in r.iter_content(8192):
            if c:f.write(c)
    return p
def ex_fr(vp,n=5):
    ffp=fb("ffprobe");ffe=fb("ffmpeg");fs=[]
    ds=subprocess.check_output([ffp,"-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(vp)],timeout=15).decode().strip()
    dur=float(ds)if ds else 30
    if dur<=0:dur=30
    iv=max(dur/(n+1),1)
    for i in range(n):
        fp=vp.with_name(f"f{i}.jpg")
        subprocess.run([ffe,"-y","-ss",str(iv*(i+1)),"-i",str(vp),"-vframes","1","-q:v","5",str(fp)],capture_output=True,timeout=30)
        if fp.exists():fs.append(fp)
    if not fs:raise Exception("未能提取帧")
    return fs
def b64(p):
    with open(p,"rb")as f:return f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"
def dl_img_b64(url, timeout=20, max_side=768):
    """下载图片转 base64（2026-08-03 优化：带 UA/Referer 防防盗链 + ffmpeg 压缩到最长边768，减少传输/推理耗时）"""
    h={"User-Agent":XHS_UA,"Referer":"https://www.xiaohongshu.com/"}
    r=requests.get(url,headers=h,timeout=timeout)
    r.raise_for_status()
    data=r.content
    if len(data) > 200_000:
        try:
            ffe=fb("ffmpeg")
            inp=Path(tempfile.NamedTemporaryFile(suffix=".img",delete=False).name)
            outp=Path(tempfile.NamedTemporaryFile(suffix=".jpg",delete=False).name)
            inp.write_bytes(data)
            subprocess.run([ffe,"-y","-i",str(inp),"-vf",f"scale='min({max_side},iw)':-2","-q:v","5",str(outp)],capture_output=True,timeout=15)
            if outp.exists() and outp.stat().st_size > 0:
                data=outp.read_bytes()
            inp.unlink(missing_ok=True); outp.unlink(missing_ok=True)
        except Exception:
            pass
    return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
def call_vlm(urls,prompt,k):
    h={"Authorization":f"Bearer {k}","Content-Type":"application/json"}
    ct=[{"type":"text","text":prompt}]
    for u in urls:ct.append({"type":"image_url","image_url":{"url":u}})
    r=requests.post(VL_URL,headers=h,json={"model":VL_MODEL,"messages":[{"role":"user","content":ct}],"max_tokens":4096},timeout=180)
    r.raise_for_status();return r.json()["choices"][0]["message"]["content"]
def asr(vp):
    ffe=fb("ffmpeg");ap=vp.with_suffix(".mp3")
    try:subprocess.run([ffe,"-y","-i",str(vp),"-vn","-acodec","libmp3lame","-q:a","4",str(ap)],capture_output=True,timeout=120,check=True)
    except:return""
    if not ap.exists()or ap.stat().st_size<1024:return""
    k=os.getenv("API_KEY")or os.getenv("DASHSCOPE_API_KEY")
    if not k:return""
    try:
        with open(ap,"rb")as f:
            fs={"file":(ap.name,f,"audio/mpeg"),"model":(None,"FunAudioLLM/SenseVoiceSmall")}
            r=requests.post("https://api.siliconflow.cn/v1/audio/transcriptions",files=fs,headers={"Authorization":f"Bearer {k}"},timeout=120)
            r.raise_for_status();t=r.json().get("text","").strip();return t if t else""
    except:return""
    finally:
        if ap.exists():ap.unlink(missing_ok=True)

# ══════════════════════════════════════════════
#  MCP Tools
# ══════════════════════════════════════════════════

@mcp.tool()
def parse_share_link(s:str)->str:
    p=dp(s)
    try:
        if p=="xiaohongshu":return json.dumps(parse_xhs(s),ensure_ascii=False,indent=2)
        if p=="douyin":return json.dumps(dy_p(s),ensure_ascii=False,indent=2)
        x=parse_xhs(s)
        if x.get("status")=="success"and(x.get("title")or x.get("desc")):return json.dumps(x,ensure_ascii=False,indent=2)
        return json.dumps(dy_p(s),ensure_ascii=False,indent=2)
    except Exception as e:return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)

@mcp.tool()
def analyze_share_images(s:str)->str:
    k=ak()
    if not k:return json.dumps({"status":"error","error":"请设置API_KEY"},ensure_ascii=False)
    try:
        p=dp(s)
        if p=="xiaohongshu":
            i=parse_xhs(s)
            if i.get("status")!="success":return json.dumps(i,ensure_ascii=False)
            us=list(i.get("images",[]))
        elif p=="douyin":
            i=dy_p(s);us=list(i.get("images",[]))
            if i.get("cover_url"):us.append(i["cover_url"])
        else:return json.dumps({"status":"error","error":"无法识别"},ensure_ascii=False)
        if not us:return json.dumps({"status":"error","error":"无图片"},ensure_ascii=False)
        r={"status":"success","platform":p,"title":i.get("title",""),
           "author":i.get("author",{}).get("nickname","")if isinstance(i.get("author"),dict)else i.get("author",""),
           "image_count":len(us),"image_analysis":[]}
        # 2026-08-03 优化：图片先下载转b64（防盗链）+ 并发识图（多张图耗时≈单张）+ 单张失败降级
        from concurrent.futures import ThreadPoolExecutor
        def one(idx_u):
            idx, u = idx_u
            try:
                b = dl_img_b64(u)
                return {"image_index":idx+1,"url":u,"content":call_vlm([b],IMG_PROMPT,k)}
            except Exception as e:
                return {"image_index":idx+1,"url":u,"content":f"[该图识别失败] {e}"}
        with ThreadPoolExecutor(max_workers=min(4, len(us))) as ex:
            r["image_analysis"] = list(ex.map(one, enumerate(us)))
        return json.dumps(r,ensure_ascii=False,indent=2)
    except Exception as e:return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)

@mcp.tool()
def analyze_share_video(s:str,n: int = 4)->str:
    """视频分析：下载+抽帧+ASR音频转文字（2026-08-03：默认4帧，画面分析失败不丢ASR）"""
    k=ak()
    if not k:return json.dumps({"status":"error","error":"请设置API_KEY"},ensure_ascii=False)
    vp=None
    try:
        p=dp(s)
        if p=="xiaohongshu":
            i=parse_xhs(s)
            if i.get("status")!="success":return json.dumps(i,ensure_ascii=False)
            vu=i.get("video_url")
        elif p=="douyin":
            i=dy_p(s);vu=i.get("video_url")
        else:return json.dumps({"status":"error","error":"无法识别"},ensure_ascii=False)
        if not vu:return json.dumps({"status":"error","error":"无视频"},ensure_ascii=False)
        r={"status":"success","platform":p,"title":i.get("title",""),
           "author":i.get("author",{}).get("nickname","")if isinstance(i.get("author"),dict)else i.get("author","")}
        if i.get("music"):
            r["background_music"]=i["music"]["title"]if isinstance(i.get("music"),dict)else i["music"]
        
        # 下载+抽帧（用OpenAI兼容接口，保证可用）
        vp=dl_v(vu)
        frames=ex_fr(vp,n)
        r["frames_extracted"]=len(frames)
        b=[b64(f)for f in frames]
        
        # 画面分析（失败降级，不丢后续ASR结果）
        try:
            analysis=call_vlm(b,VID_PROMPT,k)
            r["visual_analysis"]=analysis
        except Exception as e:
            r["visual_analysis"]=None
            r["visual_note"]=f"画面分析失败: {e}"
        
        # 音频ASR
        t=asr(vp)
        if t:
            r["audio_transcript"]=t;r["has_audio"]=True
        else:
            r["has_audio"]=False;r["audio_note"]="未检测到人声"
        
        return json.dumps(r,ensure_ascii=False,indent=2)
    except Exception as e:return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)
    finally:
        if vp and vp.exists():
            vp.unlink(missing_ok=True)
            for f in Path(vp.parent).glob("f*.jpg"):f.unlink(missing_ok=True)
            ap=vp.with_suffix(".mp3")
            if ap.exists():ap.unlink(missing_ok=True)

@mcp.tool()
def extract_share_text(s:str)->str:
    k=ak()
    if not k:return json.dumps({"status":"error","error":"请设置API_KEY"},ensure_ascii=False)
    try:
        p=dp(s)
        if p=="xiaohongshu":
            i=parse_xhs(s)
            if i.get("status")!="success":return json.dumps(i,ensure_ascii=False)
            return json.dumps({"status":"success","platform":"xiaohongshu","title":i.get("title",""),
                               "text":i.get("desc",""),"tags":i.get("tags",[]),"word_count":len(i.get("desc",""))},
                              ensure_ascii=False,indent=2)
        elif p=="douyin":
            try:
                tr=extract_text(s,api_key=k,show_progress=False)
                return json.dumps({"status":"success","platform":"douyin","video_id":tr.get("video_info",{}).get("video_id",""),
                                   "title":tr.get("video_info",{}).get("title",""),"text":tr.get("text","")},
                                  ensure_ascii=False,indent=2)
            except:
                i=dy_p(s)
                return json.dumps({"status":"success","platform":"douyin","title":i.get("title",""),
                                   "text":i.get("title",""),"note":"ASR不可用"},ensure_ascii=False,indent=2)
        else:return json.dumps({"status":"error","error":"无法识别"},ensure_ascii=False)
    except Exception as e:return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)

@mcp.tool()
def get_share_download_link(s:str)->str:
    try:
        p=dp(s)
        if p=="xiaohongshu":
            i=parse_xhs(s)
            if i.get("status")!="success":return json.dumps(i,ensure_ascii=False)
            r={"status":"success","platform":"xiaohongshu","title":i.get("title",""),
               "images":i.get("images",[]),"image_count":i.get("image_count",0)}
            if i.get("video_url"):r["video_url"]=i["video_url"]
            return json.dumps(r,ensure_ascii=False,indent=2)
        elif p=="douyin":
            i=get_video_info(s)
            return json.dumps({"status":"success","platform":"douyin","video_id":i["video_id"],
                               "title":i["title"],"download_url":i["url"]},ensure_ascii=False,indent=2)
        else:return json.dumps({"status":"error","error":"无法识别"},ensure_ascii=False)
    except Exception as e:return json.dumps({"status":"error","error":str(e)},ensure_ascii=False)
