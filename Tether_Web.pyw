"""
Tether — YouTube Video Downloader (Web UI)
Warm, cozy dark HTML frontend with a local Python backend.
Double-click to run — opens in your browser, no console window.
"""

import subprocess
import sys
import os
import shutil
import threading
import re
import json
import webbrowser
import io
import base64
import socket
import signal
import time as _time_mod
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# Platform helper: CREATE_NO_WINDOW only on Windows
import platform as _plat
_NO_WINDOW = 0x08000000 if _plat.system() == 'Windows' else 0

# --- Paths ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", os.path.join(os.path.expanduser("~"), "tether_downloads"))
ICON_PATH = os.path.join(SCRIPT_DIR, "icon.ico")

def get_downloads_dir(): return DOWNLOADS_DIR

SUPPORTED_PLATFORMS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "twitter.com": "Twitter/X", "x.com": "Twitter/X", "t.co": "Twitter/X",
    "tiktok.com": "TikTok", "instagram.com": "Instagram", "vimeo.com": "Vimeo",
    "reddit.com": "Reddit", "redd.it": "Reddit",
    "dailymotion.com": "Dailymotion", "twitch.tv": "Twitch",
    "clips.twitch.tv": "Twitch", "soundcloud.com": "SoundCloud",
    "bandcamp.com": "Bandcamp", "niconico.jp": "Niconico",
    "nicovideo.jp": "Niconico", "bilibili.com": "Bilibili",
    "b23.tv": "Bilibili", "facebook.com": "Facebook", "fb.watch": "Facebook",
}

def detect_platform(url):
    u = url.lower().strip()
    for domain, name in SUPPORTED_PLATFORMS.items():
        if domain in u: return name, domain
    if u.startswith("http://") or u.startswith("https://"):
        return "Unknown (will try)", None
    return None, None

def check_yt_dlp():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, timeout=5,
            creationflags=_NO_WINDOW
        )
        return result.returncode == 0
    except Exception: return False

def install_yt_dlp():
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            check=True, capture_output=True, text=True,
            creationflags=_NO_WINDOW
        )
        return True
    except subprocess.CalledProcessError: return False

def make_icon_b64():
    try:
        from PIL import Image
        # Try .ico first, then .png, then give up silently
        icon_path = ICON_PATH
        if not os.path.exists(icon_path):
            png_path = icon_path.replace(".ico", ".png")
            if os.path.exists(png_path):
                icon_path = png_path
            else:
                return ""
        img = Image.open(icon_path).convert("RGBA")
        img = img.resize((28, 28), Image.Resampling.LANCZOS)
        amber = (184, 168, 152)
        data = list(img.getdata())
        new_data = []
        for r, g, b, a in data:
            if a < 10: new_data.append((0, 0, 0, 0))
            else:
                brightness = (r + g + b) / (3 * 255)
                nr = int(amber[0] * brightness)
                ng = int(amber[1] * brightness)
                nb = int(amber[2] * brightness)
                new_data.append((nr, ng, nb, a))
        img.putdata(new_data)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception: return ""

ICON_B64 = make_icon_b64()

active_processes = {}  # {session_id: Popen}
process_lock = threading.Lock()
_session_counter = 0
_session_lock = threading.Lock()
progress_states = {}  # {session_id: state_dict}
progress_lock = threading.Lock()

def _new_progress():
    return {"percent": 0.0, "size": "", "speed": "", "eta": "",
            "done": False, "error": "", "active": False}

def update_progress(session_id, **kwargs):
    with progress_lock:
        if session_id in progress_states:
            progress_states[session_id].update(kwargs)
def get_progress(session_id):
    with progress_lock:
        return dict(progress_states.get(session_id, _new_progress()))
def reset_progress(session_id):
    with progress_lock:
        progress_states[session_id] = _new_progress()

log_buffers = {}  # {session_id: [lines]}
log_lock = threading.Lock()
MAX_LOG_LINES = 2000
def append_log(session_id, line):
    with log_lock:
        if session_id not in log_buffers:
            log_buffers[session_id] = []
        log_buffers[session_id].append(line)
        if len(log_buffers[session_id]) > MAX_LOG_LINES:
            del log_buffers[session_id][:len(log_buffers[session_id]) - MAX_LOG_LINES]
def get_log_lines(session_id):
    with log_lock: return list(log_buffers.get(session_id, []))
def clear_log(session_id):
    with log_lock:
        if session_id in log_buffers:
            log_buffers[session_id].clear()

# --- Security helpers ---
_MAX_POST = 1024 * 1024
_FMT_ID_RE = re.compile(r'^[a-zA-Z0-9_+/\[\]().*| -]+$')
_url_scheme_re = re.compile(r'^https?://', re.IGNORECASE)
_rate_lock = threading.Lock()
_rate_buckets = {}
_MAX_PER_MINUTE = 120

def _check_rate(ip):
    import time as _time
    now = _time.time()
    with _rate_lock:
        ts = _rate_buckets.get(ip, [])
        ts = [t for t in ts if now - t < 60]
        if len(ts) >= _MAX_PER_MINUTE: return False
        ts.append(now)
        _rate_buckets[ip] = ts
        # Periodic cleanup: purge IPs with no recent requests
        if len(_rate_buckets) > 10000:
            stale = [k for k, v in _rate_buckets.items()
                     if not v or now - v[-1] > 120]
            for k in stale: del _rate_buckets[k]
        return True

def _sanitize_format_id(fmt_id):
    if not fmt_id or len(fmt_id) > 512: return None
    if not _FMT_ID_RE.match(fmt_id): return None
    return fmt_id

def _sanitize_url(url):
    if not url or len(url) > 2048: return None
    url = url.strip()
    # Only allow http/https schemes — reject file://, ftp://, data:, javascript:, etc.
    if not _url_scheme_re.match(url):
        # Auto-prepend https:// if it looks like a bare domain (e.g. youtube.com/...)
        if re.match(r'^[a-zA-Z0-9]', url) and not url.startswith('//'):
            url = 'https://' + url
        else:
            return None
    # Extra safety: verify it's not a disguised scheme (e.g. "javascript:...")
    scheme = url.split('://')[0].lower()
    if scheme not in ('http', 'https'):
        return None
    # Reject URLs with characters that could cause issues in subprocess or yt-dlp
    # Only allow safe URL characters per RFC 3986 plus common query string chars
    if not re.match(r'^[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+$', url):
        return None
    # Reject URLs containing backslashes (Windows path confusion) and null bytes
    if '\\' in url or '\x00' in url:
        return None
    # Reject URLs with embedded credentials (e.g. http://user:pass@host)
    # These could be used for SSRF via credential injection
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            return None
    except Exception:
        return None
    return url

def _parse_formats(output):
    formats = []
    for line in output.splitlines():
        s = line.strip()
        if not s: continue
        # Skip header/separator lines
        if s.startswith("ID ") or s.startswith("---") or s.startswith("["): continue
        # Must start with a format ID (digits or sbN for storyboards)
        if not (s[0].isdigit() or s.startswith("sb")): continue
        parts = s.split()
        if len(parts) < 3: continue
        fmt_id = parts[0]
        ext = parts[1]
        # Resolution column: can be "audio only", "256x144", "1920x1080", etc.
        res_id = parts[2]
        fps = filesize = vcodec = acodec = ""
        try:
            # Find the pipe separators that divide columns
            p1 = s.index("|")
            p2 = s.index("|", p1 + 1)
            left = s[:p1].strip().split()
            mid = s[p1 + 1:p2].strip().split()
            right = s[p2 + 1:].strip()
            # Handle "audio only" resolution (two-word value in column)
            if len(left) >= 4 and left[2] == "audio" and left[3] == "only":
                res_id = "audio only"
                fps = left[4] if len(left) > 4 else "-"
            else:
                fps = left[3] if len(left) > 3 else "-"
            # Filesize: strip "~" prefix (approximate size marker)
            if mid:
                filesize = mid[0].lstrip("~")
                if not filesize and len(mid) > 1:
                    filesize = mid[1].lstrip("~")
            # Parse codec info from right section
            right_parts = right.split()
            rl = right.lower()
            if right_parts:
                if "audio only" in rl and "video only" not in rl:
                    vcodec = "-"
                    # Find audio codec: first token with a dot (e.g. mp4a.40.2) or known name
                    for rp in right_parts:
                        if "." in rp or rp.lower() in ("opus", "aac", "mp3", "vorbis", "flac", "alac"):
                            acodec = rp
                            break
                elif "video only" in rl:
                    vcodec = right_parts[0]
                    acodec = "-"
                else:
                    # Combined format: first token is vcodec, find acodec after
                    vcodec = right_parts[0]
                    for rp in right_parts[1:]:
                        rp_lower = rp.lower()
                        if rp_lower in ("video", "only", ""): continue
                        if "." in rp or rp_lower in ("opus", "aac", "mp3", "vorbis", "flac", "alac", "ac-3", "ec-3", "dts"):
                            acodec = rp
                            break
        except (ValueError, IndexError): pass
        formats.append({
            "id": fmt_id, "ext": ext, "resolution": res_id,
            "fps": fps, "filesize": filesize,
            "vcodec": vcodec, "acodec": acodec
        })
    return formats

# --- HTML Frontend ---
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tether</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#080605;--surface:#0e0b0a;--border:#161210;--gray:#14110f;--gray-mid:#241e1b;--gray-lt:#3d342f;--text:#a89a8c;--text-dim:#4a3f38;--white:#b8a898;--amber:#7a6a5a;--amber-hi:#908070;--rose:#7a4a42;--accent:#b8a898}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:'Segoe UI Variable',Inter,'Segoe UI',system-ui,-apple-system,sans-serif;font-size:16px;line-height:1.5;overflow-x:hidden}

/* ═══ Animations ═══ */
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeOut{from{opacity:1;transform:translateY(0)}to{opacity:0;transform:translateY(8px)}}
@keyframes slideUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideDown{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:translateY(0)}}
@keyframes scaleIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:scale(1)}}
@keyframes scaleOut{from{opacity:1;transform:scale(1)}to{opacity:0;transform:scale(.95)}}
@keyframes rowIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:translateX(0)}}
@keyframes pulse{0%,100%{opacity:.8}50%{opacity:.4}}
@keyframes closeSlide{from{opacity:1;transform:translateY(0)}to{opacity:0;transform:translateY(20px)}}

#bgCanvas{position:fixed;inset:0;z-index:0;pointer-events:none}
.app{position:relative;z-index:1;max-width:1280px;width:92vw;margin:0 auto;min-height:100%;display:flex;flex-direction:row;gap:20px;padding:36px 40px 28px;animation:fadeIn .4s ease}
.main-content{flex:1;min-width:0;display:flex;flex-direction:column}
.header{display:flex;align-items:center;gap:12px;margin-bottom:28px;flex-shrink:0;animation:slideDown .4s ease;position:relative}
.settings-bar{position:absolute;top:0;right:0;display:flex;gap:4px;align-items:center}
.header .icon-img{height:36px;width:36px;image-rendering:auto;filter:saturate(.5)brightness(.8)}
.header h1{font-size:24px;font-weight:700;color:var(--white);letter-spacing:3px}
.header .tagline{font-size:13px;color:var(--gray-lt);padding-top:2px}
.section{margin-bottom:18px;flex-shrink:0}
.input-row{display:flex;gap:8px;align-items:stretch}
input[type=text]{flex:1;min-width:0;background:#0a0806;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 14px;font-size:15px;font-family:inherit;outline:none;transition:border-color .2s,box-shadow .2s}
input[type=text]:focus{border-color:var(--gray-mid)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:none;border-radius:8px;cursor:pointer;font-family:inherit;font-size:14px;font-weight:500;padding:9px 16px;white-space:nowrap;transition:background .15s,color .15s,opacity .15s,transform .1s}
.btn:active:not(:disabled){transform:scale(.97)}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-primary{background:var(--white);color:var(--bg);font-weight:700;font-size:15px;padding:11px 26px}
.btn-ghost{background:var(--surface);color:var(--gray-lt);border:1px solid var(--border);font-size:13px;transition:all .15s}
.btn-ghost:hover:not(:disabled){background:var(--gray);color:var(--text)}
.btn-cancel{background:transparent;color:var(--rose);border:1px solid var(--border);font-size:13px;padding:9px 16px}
.btn-chip{background:var(--gray);color:var(--gray-lt);font-size:13px;padding:6px 12px;border-radius:20px;transition:all .15s}
.btn-chip:hover{background:var(--gray-mid);color:var(--text)}
.btn-chip.active{background:var(--gray);color:var(--accent);border:1px solid var(--gray-mid)}
.btn-chip.disabled{opacity:.3;pointer-events:none}
.chips{display:flex;gap:5px;flex-wrap:wrap}
.chip-sep{width:1px;height:16px;background:var(--border);border-radius:1px;align-self:center;margin:0 2px;flex-shrink:0}
.merge-chip-active{color:var(--accent)!important;border-color:var(--gray-mid)!important;background:var(--gray)!important}

/* Fetching animation */
.fetching-wrap{display:flex;align-items:center;gap:10px;margin-top:8px;font-size:13px;color:var(--text-dim);animation:fadeIn .3s ease}
.fetching-dots{display:flex;gap:4px}
.fetching-dots span{width:6px;height:6px;background:var(--accent);border-radius:50%;animation:fetchBounce .8s ease-in-out infinite}
.fetching-dots span:nth-child(2){animation-delay:.15s}
.fetching-dots span:nth-child(3){animation-delay:.3s}
@keyframes fetchBounce{0%,80%,100%{transform:scale(.4);opacity:.3}40%{transform:scale(1);opacity:1}}

/* Quick actions row */
.quick-actions{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;flex-shrink:0}
.quick-actions .btn-chip{font-size:12px;padding:5px 10px}

/* Format panels */
.format-panel{flex:1;min-height:0;display:flex;flex-direction:column;background:#0a0806;border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:18px}
.table-scroll{flex:1;overflow-y:auto;scrollbar-width:thin;scrollbar-color:#161210 #0a0806}
.table-scroll::-webkit-scrollbar{width:8px}
.table-scroll::-webkit-scrollbar-track{background:#0a0806}
.table-scroll::-webkit-scrollbar-thumb{background:#161210;border-radius:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
thead{position:sticky;top:0;z-index:2}
thead th{background:#0a0806;color:var(--gray-mid);font-weight:600;text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:.5px;cursor:pointer;user-select:none;white-space:nowrap;transition:color .15s}
thead th:hover{color:var(--text)}
thead th .sort-arrow{font-size:9px;margin-left:4px;opacity:.3}
thead th.sorted .sort-arrow{opacity:1}
tbody tr{cursor:pointer;transition:background .1s}
tbody tr:hover{background:rgba(255,255,255,.02)}
tbody tr.selected{background:var(--gray)}
tbody td{padding:7px 12px;color:var(--text-dim);border-bottom:1px solid rgba(22,18,16,.5)}
tbody tr.selected td{color:var(--text)}
tbody tr:last-child td{border-bottom:none}
tbody tr{animation:rowIn .25s ease both}
.empty-state{display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-dim);font-size:14px;padding:24px;text-align:center}
.size-warning{display:inline-block;color:var(--rose);font-size:11px;margin-left:4px}
.format-warning{color:var(--rose);font-size:11px;display:block;margin-top:2px}
.bottom{flex-shrink:0;animation:slideUp .4s ease .15s both}
.progress-track{width:100%;height:5px;background:var(--surface);border-radius:3px;overflow:hidden;margin-bottom:8px}
.progress-fill{height:100%;background:var(--accent);border-radius:3px;width:0%;transition:width .3s ease}
.status{font-size:13px;color:var(--text-dim);min-height:18px;margin-bottom:12px;transition:color .3s}
.status.success{color:var(--accent)}
.status.error{color:var(--rose)}
.status.downloading{color:var(--accent);animation:pulse 1s ease-in-out infinite}

/* Downloading indicator badge */
#dlIndicator{display:none;position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:8px 18px;font-size:13px;color:var(--accent);z-index:50;opacity:0;transition:opacity .3s,transform .3s;pointer-events:none;white-space:nowrap;}
#dlIndicator.visible{display:block;opacity:1;transform:translateX(-50%) translateY(0)}
#dlIndicator .dl-dot{display:inline-block;width:7px;height:7px;background:var(--accent);border-radius:50%;margin-right:8px;animation:pulse .8s ease-in-out infinite;vertical-align:middle}

/* Donation button - bottom left fixed */
#donateBtn{position:fixed;bottom:20px;left:20px;z-index:50;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 14px;font-size:12px;color:var(--text);cursor:pointer;font-family:inherit;transition:all .2s;white-space:nowrap;display:flex;align-items:center;gap:6px}
#donateBtn:hover{background:var(--gray);border-color:var(--gray-mid);color:var(--white);transform:translateY(-1px)}
#donateBtn .heart{font-size:14px;animation:pulse 1.2s ease-in-out infinite}

/* Download note */
.download-note{font-size:11px;color:var(--text-dim);text-align:center;margin-top:8px;opacity:.7}

/* Overlays */
.log-overlay{display:none;position:fixed;inset:0;z-index:100;background:rgba(4,3,2,.92);backdrop-filter:blur(4px);flex-direction:column}
.log-overlay.open{display:flex;animation:fadeIn .2s ease}
.log-overlay.closing{animation:closeSlide .25s ease forwards}
.log-header{display:flex;align-items:center;justify-content:space-between;padding:16px 24px;border-bottom:1px solid var(--border);flex-shrink:0}
.log-header h3{font-size:15px;font-weight:600;color:var(--accent);letter-spacing:.5px}
.log-close{background:var(--gray);color:var(--gray-lt);border:1px solid var(--border);border-radius:8px;padding:6px 14px;cursor:pointer;font-family:inherit;font-size:13px;transition:color .15s}
.log-close:hover{color:var(--text)}
.log-body{flex:1;overflow-y:auto;padding:14px 24px;scrollbar-width:thin;scrollbar-color:#161210 #0a0806}
.log-body::-webkit-scrollbar{width:7px}
.log-body::-webkit-scrollbar-track{background:#0a0806}
.log-body::-webkit-scrollbar-thumb{background:#161210;border-radius:4px}
.log-line{font-family:'Cascadia Code',Consolas,'Courier New',monospace;font-size:12px;line-height:1.6;color:var(--text-dim);white-space:pre-wrap;word-break:break-all;padding:2px 0}
.log-line.error{color:var(--rose)}
.log-line.success{color:var(--accent)}
.log-placeholder{color:var(--text-dim);font-size:14px;padding:24px 0;text-align:center}

/* Merge split panels */
.merge-split{display:flex;gap:12px;flex:1;min-height:0;animation:scaleIn .25s ease}
.merge-split .format-panel{flex:1;margin-bottom:0}
.merge-split .panel-label{font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--gray-mid);padding:4px 0 6px}
@keyframes panelClose{from{opacity:1;transform:scale(1)}to{opacity:0;transform:scale(.98)}}
.panel-closing{animation:panelClose .2s ease forwards}
.merge-split .video-panel .panel-label{color:var(--amber)}
.merge-split .audio-panel .panel-label{color:var(--rose)}

/* Legal modal specific */
.legal-content h4{color:var(--text);font-size:14px;margin:16px 0 8px}
.legal-content h4:first-child{margin-top:0}
.legal-content p{margin-bottom:8px}
.legal-content ul{margin:8px 0 12px 20px}
.legal-content li{margin-bottom:4px}

/* ═══ Ad Sidebar ═══ */
.ad-sidebar{width:300px;flex-shrink:0;display:flex;flex-direction:column;gap:12px;position:sticky;top:36px;align-self:flex-start;max-height:calc(100vh - 72px);animation:slideUp .5s ease .3s both}
.ad-box{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.ad-label{font-size:10px;text-transform:uppercase;letter-spacing:1.2px;color:var(--text-dim);padding:8px 12px 4px;opacity:.6}
.ad-body{flex:1;display:flex;align-items:center;justify-content:center;min-height:250px;padding:8px}
.ad-body .placeholder{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--text-dim);font-size:12px;text-align:center;opacity:.4}
.ad-body .placeholder .ad-icon{font-size:28px;opacity:.5}
.ad-footer{font-size:10px;color:var(--text-dim);padding:4px 12px 8px;text-align:center;opacity:.4}
@media(max-width:900px){
  .ad-sidebar{display:none}
  .app{flex-direction:column;max-width:960px;width:90vw}
}
</style>
</head>
<body>

<!-- Downloading floating indicator -->
<div id="dlIndicator"><span class="dl-dot"></span><span id="dlIndicatorText">downloading...</span></div>

<!-- Donation button - bottom left -->
<button id="donateBtn" onclick="showDonate()"><span class="heart">&#10084;</span> Support Tether</button>

<!-- Donate modal -->
<div class="log-overlay" id="donateOverlay">
  <div class="log-header"><h3>Support Tether</h3><button class="log-close" onclick="hideDonate()">close</button></div>
  <div class="log-body" style="max-width:480px;margin:0 auto;text-align:center;padding:32px 24px">
    <div style="font-size:40px;margin-bottom:16px">&#10084;</div>
    <h4 style="color:var(--white);font-size:18px;margin-bottom:12px">Keep Tether a Great Service</h4>
    <p style="color:var(--text-dim);font-size:14px;line-height:1.7;margin-bottom:24px">Tether is built to be fast, reliable, and easy to use. If you find it valuable, consider supporting the project so we can keep improving it, cover hosting costs, and continue building new features. Every contribution makes a difference &mdash; thank you for being part of this.</p>
    <div style="display:flex;flex-direction:column;gap:10px;max-width:320px;margin:0 auto">
      <a href="https://ko-fi.com/tetherteam" target="_blank" rel="noopener" class="btn btn-primary" style="text-decoration:none;width:100%;justify-content:center">&#10084; Support on Ko-fi</a>
    </div>
    <p style="color:var(--text-dim);font-size:11px;margin-top:20px;opacity:.5">Donations are optional and non-refundable. Thank you for your support!</p>
  </div>
</div>

<canvas id="bgCanvas"></canvas>
<div class="app" id="mainApp">
  <div class="main-content">
  <div class="header">
    __ICON_PLACEHOLDER__
    <h1>TETHER</h1>
    <div class="settings-bar">
      <button class="btn btn-ghost" style="font-size:11px;padding:5px 12px" onclick="showSettings('faq')">FAQ</button>
      <button class="btn btn-ghost" style="font-size:11px;padding:5px 12px" onclick="showSettings('legal')">legal &amp; terms</button>
    </div>
  </div>
  <div class="section">
    <div class="input-row">
      <input type="text" id="urlInput" placeholder="Paste a video URL..." autocomplete="off" spellcheck="false">
      <button class="btn btn-ghost" id="pasteBtn" onclick="pasteUrl()">paste</button>
      <button class="btn btn-ghost" id="fetchBtn" onclick="fetchFormats()">fetch</button>
    </div>
  </div>
  <div class="section">
    <div class="chips" id="chips">
      <button class="btn btn-chip active" onclick="selectPreset(this,'Best (Video + Audio)')">best</button>
      <span class="chip-sep"></span>
      <button class="btn btn-chip" id="mergeChip" onclick="toggleMerge()">merge formats</button>
    </div>
  </div>

  <!-- Quick action buttons (grayed out if format not available) -->
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--text-dim);margin-bottom:6px">Quick Actions</div>
  <div class="quick-actions" id="quickActions">
    <button class="btn btn-chip disabled" id="qa4k" disabled onclick="quickDl('4k')">4K</button>
    <button class="btn btn-chip disabled" id="qa2k" disabled onclick="quickDl('2k')">2K</button>
    <button class="btn btn-chip disabled" id="qa1080p" disabled onclick="quickDl('1080p')">1080p</button>
    <button class="btn btn-chip disabled" id="qa720p" disabled onclick="quickDl('720p')">720p</button>
    <button class="btn btn-chip disabled" id="qa360p" disabled onclick="quickDl('360p')">360p</button>
    <button class="btn btn-chip disabled" id="qa240p" disabled onclick="quickDl('240p')">240p</button>
    <button class="btn btn-chip disabled" id="qa144p" disabled onclick="quickDl('144p')">144p</button>
    <button class="btn btn-chip disabled" id="qaAudioMp3" disabled onclick="quickDlAudio('mp3')">audio mp3</button>
  </div>

  <!-- See all formats toggle -->
  <div id="formatsToggleRow" style="display:none;margin-bottom:10px;flex-shrink:0">
    <button class="btn btn-ghost" id="seeFormatsBtn" onclick="toggleFormatsView()" style="font-size:12px;padding:6px 14px">see all available formats</button>
  </div>

  <div id="mergeSlots" style="display:none;gap:8px;align-items:center;margin-bottom:10px;flex-shrink:0;flex-wrap:wrap">
    <span style="font-size:13px;color:var(--gray-lt)">video:</span>
    <span id="mergeVideoSlot" style="font-size:13px;font-weight:600;color:var(--bg);background:rgba(122,106,90,.7);border-radius:6px;padding:4px 10px">&#8212;</span>
    <span style="font-size:13px;color:var(--gray-lt)">audio:</span>
    <span id="mergeAudioSlot" style="font-size:13px;font-weight:600;color:var(--bg);background:rgba(122,74,66,.7);border-radius:6px;padding:4px 10px">&#8212;</span>
    <button class="btn btn-chip" style="font-size:12px;padding:4px 10px" onclick="clearMergeSlots()">clear</button>
  </div>

  <!-- Single table (normal mode) — hidden until user clicks "see all formats" -->
  <div class="format-panel" id="singleFormatPanel" style="display:none">
    <div class="table-scroll">
      <table id="fmtTable">
        <thead><tr><th data-sort="ext">EXT<span class="sort-arrow"></span></th><th data-sort="resolution">RES<span class="sort-arrow"></span></th><th data-sort="fps">FPS<span class="sort-arrow"></span></th><th data-sort="filesize">SIZE<span class="sort-arrow"></span></th><th data-sort="vcodec">VID<span class="sort-arrow"></span></th><th data-sort="acodec">AUD<span class="sort-arrow"></span></th></tr></thead>
        <tbody id="fmtBody"></tbody>
      </table>
      <div class="empty-state" id="emptyState">no formats loaded</div>
    </div>
  </div>

  <!-- Merge mode: split panels -->
  <div class="merge-split" id="mergeSplitPanel" style="display:none">
    <div class="video-panel">
      <div class="panel-label">video only</div>
      <div class="format-panel" style="margin-bottom:0">
        <div class="table-scroll">
          <table id="videoTable"><thead><tr><th>EXT</th><th>RES</th><th>FPS</th><th>SIZE</th><th>CODEC</th></tr></thead><tbody id="videoBody"></tbody></table>
          <div class="empty-state" id="emptyVideo" style="font-size:13px">no video formats</div>
        </div>
      </div>
    </div>
    <div class="audio-panel">
      <div class="panel-label">audio only</div>
      <div class="format-panel" style="margin-bottom:0">
        <div class="table-scroll">
          <table id="audioTable"><thead><tr><th>EXT</th><th>SIZE</th><th>CODEC</th></tr></thead><tbody id="audioBody"></tbody></table>
          <div class="empty-state" id="emptyAudio" style="font-size:13px">no audio formats</div>
        </div>
      </div>
    </div>
  </div>

  <div class="bottom">
    <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
    <div class="status" id="status">ready</div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:14px;flex-wrap:wrap;gap:10px">
      <button class="btn btn-primary" id="downloadBtn" onclick="startDownload()" disabled>&#11015; Download</button>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn btn-cancel" id="cancelBtn" onclick="cancelDownload()" disabled>&#10005; Cancel</button>
      </div>
    </div>
  <!-- Why Tether? -->
  <div style="flex-shrink:0;margin-top:24px">
    <div id="praiseBox" style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 24px;cursor:pointer;transition:border-color .2s" onclick="togglePraise()">
      <div style="display:flex;align-items:center;justify-content:center;gap:10px">
        <span style="font-size:14px;font-weight:600;color:var(--white);letter-spacing:.3px">Why choose Tether?</span>
        <span id="praiseArrow" style="font-size:10px;color:var(--gray-lt);transition:transform .25s">&#9660;</span>
      </div>
    </div>
    <div id="praiseDropdown" style="display:none;background:var(--surface);border:1px solid var(--border);border-top:none;border-radius:0 0 12px 12px;padding:20px 24px;margin-top:-1px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px 24px;font-size:13px;line-height:1.7;color:var(--text-dim)">
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Blazing Fast</div>
          <p>Optimized fetching and downloading pipeline gets your videos quickly and reliably.</p>
        </div>
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Privacy First</div>
          <p>No data collection, no tracking, no accounts. Your downloads are your business.</p>
        </div>
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Multiple Platforms</div>
          <p>Supports video downloads from multiple popular platforms including YouTube, TikTok, Instagram, and more.</p>
        </div>
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Clean &amp; Simple</div>
          <p>Paste a URL, pick a format, download. No bloat, no bundled software.</p>
        </div>
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Highest Quality</div>
          <p>Download in up to 4K resolution with original audio, or grab just the audio as MP3.</p>
        </div>
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Quick Actions</div>
          <p>Smart buttons detect available resolutions so you can download in one click.</p>
        </div>
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Always Up to Date</div>
          <p>Actively maintained to keep up with changes across all supported platforms.</p>
        </div>
        <div>
          <div style="font-weight:600;color:var(--text);margin-bottom:4px">&#10003; Completely Free</div>
          <p>Every feature, every format, every platform — all free.</p>
        </div>
      </div>
    </div>
  </div>

    <div class="download-note">By using this service, you agree to our <a href="#" onclick="showSettings('legal');return false" style="color:var(--accent);text-decoration:underline">terms and conditions</a></div>
  </div>

  <!-- ═══ Ad Sidebar ═══ -->
  <!-- Replace the placeholder content below with your AdSense/advertising code -->
  <aside class="ad-sidebar">
    <div class="ad-box">
      <div class="ad-label">advertisement</div>
      <div class="ad-body">
        <div class="placeholder">
          <div class="ad-icon">&#128270;</div>
          <span>ad space</span>
          <span style="font-size:11px;opacity:.6"><!-- Paste your Google AdSense code here --></span>
        </div>
      </div>
      <div class="ad-footer">powered by ads</div>
    </div>
  </aside>

</div>


<script>
// ═══════════════════════════════════════════════════════════════
//  Background
// ═══════════════════════════════════════════════════════════════
(function(){
  var cvs=document.getElementById("bgCanvas"),ctx=cvs.getContext("2d"),w,h;
  function resize(){w=cvs.width=window.innerWidth;h=cvs.height=window.innerHeight;draw();}
  function draw(){
    ctx.fillStyle="#080605";ctx.fillRect(0,0,w,h);
  }
  window.addEventListener("resize",resize);resize();
})();

// ═══════════════════════════════════════════════════════════════
//  App logic
// ═══════════════════════════════════════════════════════════════
// Per-user session ID for multi-user server support
var SESSION_ID = localStorage.getItem("tether_session") || "";
if(!SESSION_ID){ SESSION_ID = "s_" + Date.now() + "_" + Math.random().toString(36).slice(2,10); localStorage.setItem("tether_session", SESSION_ID); }
var _fetch = window.fetch;
window.fetch = function(url, opts){ opts = opts ||{}; opts.headers = opts.headers ||{}; opts.headers["X-Session-ID"] = SESSION_ID; return _fetch.call(this, url, opts); };
var selectedFormat=null,selectedPreset="Best (Video + Audio)",downloading=false,pollTimer=null;
var allFormats=[],filteredFormats=[],mergeActive=false,mergeVideoId=null,mergeAudioId=null,sortCol=null,sortAsc=true;

function setStatus(m,t){var e=document.getElementById("status");e.textContent=m;e.className="status"+(t?" "+t:"");}
function setProgress(p){document.getElementById("progressFill").style.width=p+"%";}
async function pasteUrl(){try{document.getElementById("urlInput").value=(await navigator.clipboard.readText()).trim()}catch(e){}}

function selectPreset(b,v){
  document.querySelectorAll(".btn-chip").forEach(function(x){if(x.id!=="mergeChip")x.classList.remove("active");});
  b.classList.add("active");selectedPreset=v;selectedFormat=null;
  document.querySelectorAll("#fmtBody tr").forEach(function(r){r.classList.remove("selected");});
  if(mergeActive){toggleMerge();return;}
  // If formats are visible and user picks a preset, show the single table
  if(formatsVisible){renderSingleTable();}
}

var formatsVisible=false;var formatsFetched=false;

async function fetchFormats(){
  var url=document.getElementById("urlInput").value.trim();if(!url){setStatus("paste a URL first");return}
  console.log("[Tether] fetching formats for:",url);
  showFetching();
  try{
    var r=await fetch("/api/formats",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:url})});
    console.log("[Tether] response status:",r.status);
    hideFetching();
    if(r.status===429){setStatus("too many requests — wait a moment and try again","error");return}
    if(r.status!==200){setStatus("server error (HTTP "+r.status+")","error");return}
    var d;
    try{d=await r.json();console.log("[Tether] parsed JSON:",d);}catch(jsonErr){console.log("[Tether] JSON parse error:",jsonErr);setStatus("failed to parse server response","error");return}
    if(d.error){console.log("[Tether] server error:",d.error);setStatus("fetch failed - "+d.error,"error");return}
    allFormats=d.formats;filteredFormats=allFormats.slice();sortCol=null;sortAsc=true;
    console.log("[Tether] formats count:",allFormats.length);
    updateQuickActions();
    formatsFetched=true;
    document.getElementById("downloadBtn").disabled=false;
    // Don't show format panels automatically
    formatsVisible=false;
    document.getElementById("singleFormatPanel").style.display="none";
    document.getElementById("mergeSplitPanel").style.display="none";
    document.getElementById("formatsToggleRow").style.display="";
    document.getElementById("seeFormatsBtn").textContent="see all available formats";
    document.getElementById("seeFormatsBtn").classList.remove("active");
    if(d.formats.length===0){setStatus("no formats found for this video","error");return}
    setStatus(d.formats.length+" formats found — use quick actions or see all formats below");
  }catch(e){
    console.log("[Tether] fetch exception:",e);
    hideFetching();
    setFetchingError("fetch error: "+e.message);
    setStatus("fetch error — check your connection","error");
  }
}

// ── Fetching loading animation ──
function showFetching(){
  var existing=document.getElementById("fetchingIndicator");
  if(existing)existing.remove();
  var wrap=document.createElement("div");
  wrap.id="fetchingIndicator";
  wrap.className="fetching-wrap";
  wrap.innerHTML='<div class="fetching-dots"><span></span><span></span><span></span></div><span>fetching formats...</span>';
  var qa=document.getElementById("quickActions");
  if(qa&&qa.parentNode)qa.parentNode.insertBefore(wrap,qa.nextSibling);
  setStatus("working...");
  // Disable fetch button while fetching
  document.getElementById("fetchBtn").disabled=true;
  document.getElementById("pasteBtn").disabled=true;
}
function hideFetching(){
  var el=document.getElementById("fetchingIndicator");
  if(el)el.remove();
  document.getElementById("fetchBtn").disabled=false;
  document.getElementById("pasteBtn").disabled=false;
}
function setFetchingError(msg){
  var el=document.getElementById("fetchingIndicator");
  if(el){el.innerHTML='<span style="color:var(--rose)">'+msg+'</span>';}
}

function toggleFormatsView(){
  formatsVisible=!formatsVisible;
  var btn=document.getElementById("seeFormatsBtn");
  var singlePanel=document.getElementById("singleFormatPanel");
  var mergePanel=document.getElementById("mergeSplitPanel");
  if(formatsVisible){
    btn.textContent="hide formats";
    btn.classList.add("active");
    // If merge is active show split, otherwise show single
    if(mergeActive){
      singlePanel.style.display="none";
      mergePanel.style.display="flex";
      renderMergePanels();
    } else {
      singlePanel.style.display="";
      mergePanel.style.display="none";
      renderSingleTable();
    }
  } else {
    btn.textContent="see all available formats";
    btn.classList.remove("active");
    // Closing animation
    singlePanel.classList.add("panel-closing");
    mergePanel.classList.add("panel-closing");
    setTimeout(function(){
      singlePanel.style.display="none";
      mergePanel.style.display="none";
      singlePanel.classList.remove("panel-closing");
      mergePanel.classList.remove("panel-closing");
    },200);
  }
}

// ── Quick action availability ──
function getResHeight(r){
  if(!r||r==="audio only")return 0;
  var m=r.match(/^(\d+)x(\d+)$/);
  return m?parseInt(m[2],10):0;
}
function updateQuickActions(){
  var heights={};
  allFormats.forEach(function(f){var h=getResHeight(f.resolution);if(h>0)heights[h]=true;});
  var h4k=false,h2k=false,h1080=false,h720=false,h360=false,h240=false,h144=false;
  for(var h in heights){h=parseInt(h,10);if(h>=2160)h4k=true;if(h>=1440)h2k=true;if(h>=1080)h1080=true;if(h>=720)h720=true;if(h>=360)h360=true;if(h>=240)h240=true;if(h>=144)h144=true;}
  document.getElementById("qa4k").classList.toggle("disabled",!h4k);
  document.getElementById("qa4k").disabled=!h4k;
  document.getElementById("qa2k").classList.toggle("disabled",!h2k);
  document.getElementById("qa2k").disabled=!h2k;
  document.getElementById("qa1080p").classList.toggle("disabled",!h1080);
  document.getElementById("qa1080p").disabled=!h1080;
  document.getElementById("qa720p").classList.toggle("disabled",!h720);
  document.getElementById("qa720p").disabled=!h720;
  document.getElementById("qa360p").classList.toggle("disabled",!h360);
  document.getElementById("qa360p").disabled=!h360;
  document.getElementById("qa240p").classList.toggle("disabled",!h240);
  document.getElementById("qa240p").disabled=!h240;
  document.getElementById("qa144p").classList.toggle("disabled",!h144);
  document.getElementById("qa144p").disabled=!h144;
  var hasAudio=allFormats.some(function(f){return f.resolution==="audio only"||(f.acodec&&f.acodec!=="-"&&f.acodec!=="");});
  document.getElementById("qaAudioMp3").classList.toggle("disabled",!hasAudio);
  document.getElementById("qaAudioMp3").disabled=!hasAudio;
}

function quickDl(res){
  if(!formatsFetched){setStatus("fetch formats first — paste a URL and hit fetch","error");return}
  // Check if any format at this resolution exceeds the size limit
  var targetHeight=0;
  if(res==="4k")targetHeight=2160;
  else if(res==="2k")targetHeight=1440;
  else targetHeight=parseInt(res);
  var overLimit=false;
  allFormats.forEach(function(f){
    var h=getResHeight(f.resolution);
    if(res==="4k"&&h>=2160&&isOverLimit(f.filesize))overLimit=true;
    else if(res==="2k"&&h>=1440&&h<2160&&isOverLimit(f.filesize))overLimit=true;
    else if(h===targetHeight&&isOverLimit(f.filesize))overLimit=true;
  });
  if(res==="4k"&&overLimit){setStatus("4K video over the limit — please select another resolution","error");return}
  if(overLimit){setStatus(res+" video over the 1.5GB limit — please select another resolution","error");return}
  // Find the best matching format ID for this resolution
  var bestFmt=null;
  var bestScore=-1;
  allFormats.forEach(function(f){
    var h=getResHeight(f.resolution);
    if(h<=0)return;
    var score=0;
    if(res==="4k"&&h>=2160)score=h;
    else if(res==="2k"&&h>=1440&&h<2160)score=h;
    else if(h===targetScore(res))score=h;
    // Prefer combined formats (have both video and audio)
    if(f.acodec&&f.acodec!=="-"&&f.acodec!=="")score+=10000;
    // Prefer smaller files (penalty for size)
    var sz=getSizeBytes(f.filesize);
    if(sz>0)score-=sz/1048576;
    if(score>bestScore){bestScore=score;bestFmt=f;}
  });
  if(bestFmt){
    selectedFormat=bestFmt.id;
    // Show the format table and highlight the selection
    if(!formatsVisible){toggleFormatsView();}
    // Highlight the row
    setTimeout(function(){
      document.querySelectorAll("#fmtBody tr").forEach(function(r){r.classList.remove("selected");});
      var row=document.querySelector("#fmtBody tr[data-id='"+bestFmt.id+"']");
      if(row){
        row.classList.add("selected");
        row.scrollIntoView({block:"nearest",behavior:"smooth"});
      }
    },100);
    // Clear preset selection
    document.querySelectorAll(".btn-chip").forEach(function(b){if(b.id!=="mergeChip")b.classList.remove("active");});
    setStatus("selected "+res+" format — click Download to start");
  }
}
function targetScore(res){
  if(res==="4k")return 2160;
  if(res==="2k")return 1440;
  return parseInt(res);
}

function quickDlAudio(){
  if(!formatsFetched){setStatus("fetch formats first — paste a URL and hit fetch","error");return}
  var url=document.getElementById("urlInput").value.trim();if(!url){setStatus("paste a URL first");return}
  // Check if audio formats exceed limit
  var audioOverLimit=allFormats.some(function(f){return(f.resolution==="audio only"||(f.acodec&&f.acodec!=="-"&&f.acodec!==""))&&isOverLimit(f.filesize);});
  if(audioOverLimit){setStatus("audio format over the 1.5GB limit — please select another format","error");return}
  showDlIndicator("downloading audio (mp3)...");
  startDownloadWithFormat("bestaudio[ext=m4a]/bestaudio",null,true);
}

function startDownloadWithFormat(fmtArgs,isMerge,audioOnly){
  // Check size limit for the selected format
  if(isMerge){
    var mv=allFormats.find(function(f){return f.id===mergeVideoId;});
    var ma=allFormats.find(function(f){return f.id===mergeAudioId;});
    if(mv&&isOverLimit(mv.filesize)){setStatus("selected video format is over the 1.5GB limit — please pick another format","error");return;}
    if(ma&&isOverLimit(ma.filesize)){setStatus("selected audio format is over the 1.5GB limit — please pick another format","error");return;}
  } else if(!audioOnly&&fmtArgs&&fmtArgs!=="best"){
    var selFmt=allFormats.find(function(f){return f.id===fmtArgs;});
    if(selFmt&&isOverLimit(selFmt.filesize)){setStatus("this format is over the 1.5GB limit — please pick another format","error");return;}
  }
  downloading=true;document.getElementById("downloadBtn").disabled=true;document.getElementById("cancelBtn").disabled=false;
  document.getElementById("pasteBtn").disabled=true;document.getElementById("fetchBtn").disabled=true;
  setProgress(0);setStatus("downloading...");startPolling();
  var url=document.getElementById("urlInput").value.trim();
  var body={url:url};
  if(audioOnly){body.format_id=fmtArgs;}
  else if(isMerge){body.format_id=mergeVideoId+"+"+mergeAudioId;}
  else{body.format_id=fmtArgs||"best";}
  fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).catch(function(){setStatus("error","error");resetUI();});
}

function getSizeBytes(s){if(!s)return 0;var m=s.match(/^~?(\d+\.?\d*)\s*(\w+)/i);if(!m)return 0;var n=parseFloat(m[1]),u=m[2].toLowerCase();if(u.startsWith("g"))return n*1073741824;if(u.startsWith("m"))return n*1048576;if(u.startsWith("k"))return n*1024;return n;}
var SIZE_LIMIT=1.5*1073741824;
function isOverLimit(fs){return getSizeBytes(fs)>SIZE_LIMIT;}
function sortBy(col){
  if(sortCol===col){sortAsc=!sortAsc;}else{sortCol=col;sortAsc=true;}
  filteredFormats.sort(function(a,b){var va=a[col]||"",vb=b[col]||"";if(col==="filesize")return sortAsc?getSizeBytes(va)-getSizeBytes(vb):getSizeBytes(vb)-getSizeBytes(va);if(col==="fps"){va=parseFloat(va)||0;vb=parseFloat(vb)||0;return sortAsc?va-vb:vb-va;}va=va.toLowerCase();vb=vb.toLowerCase();return sortAsc?va.localeCompare(vb):vb.localeCompare(va);});
  document.querySelectorAll("#singleFormatPanel thead th").forEach(function(t){t.classList.toggle("sorted",t.dataset.sort===col);t.querySelector(".sort-arrow").textContent=t.dataset.sort===col?(sortAsc?"&#9650;":"&#9660;"):"";});
  renderSingleTable();
}
document.querySelectorAll("#singleFormatPanel thead th[data-sort]").forEach(function(t){t.addEventListener("click",function(){sortBy(t.dataset.sort);});});

function renderSingleTable(){
  document.getElementById("singleFormatPanel").style.display="";document.getElementById("mergeSplitPanel").style.display="none";
  var tb=document.getElementById("fmtBody");tb.innerHTML="";document.getElementById("emptyState").style.display=filteredFormats.length?"none":"flex";
  filteredFormats.forEach(function(f,i){var tr=document.createElement("tr");tr.dataset.id=f.id;tr.style.animationDelay=(i*0.02)+"s";var warn=isOverLimit(f.filesize)?' <span class="size-warning" title="Exceeds 1.5 GB limit — may fail to download">&gt;1.5GB</span>':"";tr.innerHTML="<td>"+f.ext+"</td><td>"+f.resolution+"</td><td>"+f.fps+"</td><td>"+f.filesize+warn+"</td><td>"+f.vcodec+"</td><td>"+f.acodec+"</td>";tr.addEventListener("click",function(){document.querySelectorAll("#fmtBody tr").forEach(function(r){r.classList.remove("selected");});tr.classList.add("selected");selectedFormat=f.id;document.querySelectorAll(".btn-chip").forEach(function(b){if(b.id!=="mergeChip")b.classList.remove("active");});if(isVideoOnly(f)){if(!confirm("This video has no audio. Are you sure you want to download this format?")){tr.classList.remove("selected");selectedFormat=null;return;}}});tb.appendChild(tr);});
}

function renderMergePanels(){
  document.getElementById("singleFormatPanel").style.display="none";document.getElementById("mergeSplitPanel").style.display="flex";
  var vf=allFormats.filter(function(f){return isVideoFormat(f);});var af=allFormats.filter(function(f){return isAudioFormat(f);});
  var vt=document.getElementById("videoBody");vt.innerHTML="";document.getElementById("emptyVideo").style.display=vf.length?"none":"flex";
  vf.forEach(function(f,i){var tr=document.createElement("tr");tr.dataset.id=f.id;tr.style.animationDelay=(i*0.03)+"s";var vwarn=isOverLimit(f.filesize)?' <span class="size-warning" title="Exceeds 1.5 GB limit">&gt;1.5GB</span>':"";tr.innerHTML="<td>"+f.ext+"</td><td>"+f.resolution+"</td><td>"+f.fps+"</td><td>"+f.filesize+vwarn+"</td><td>"+f.vcodec+"</td>";tr.addEventListener("click",function(){document.querySelectorAll("#videoBody tr.selected").forEach(function(r){r.classList.remove("selected");});tr.classList.add("selected");mergeVideoId=f.id;document.getElementById("mergeVideoSlot").textContent=f.id+" - "+f.resolution+" - "+f.ext;tryMergeSelect();});vt.appendChild(tr);});
  var at=document.getElementById("audioBody");at.innerHTML="";document.getElementById("emptyAudio").style.display=af.length?"none":"flex";
  af.forEach(function(f,i){var tr=document.createElement("tr");tr.dataset.id=f.id;tr.style.animationDelay=(i*0.03)+"s";var awarn=isOverLimit(f.filesize)?' <span class="size-warning" title="Exceeds 1.5 GB limit">&gt;1.5GB</span>':"";tr.innerHTML="<td>"+f.ext+"</td><td>"+f.filesize+awarn+"</td><td>"+f.acodec+"</td>";tr.addEventListener("click",function(){document.querySelectorAll("#audioBody tr.selected").forEach(function(r){r.classList.remove("selected");});tr.classList.add("selected");mergeAudioId=f.id;document.getElementById("mergeAudioSlot").textContent=f.id+" - "+f.filesize+" - "+f.acodec;tryMergeSelect();});at.appendChild(tr);});
}

function tryMergeSelect(){if(mergeVideoId&&mergeAudioId)setStatus("ready: "+mergeVideoId+" + "+mergeAudioId);}
function isVideoFormat(f){return f.vcodec&&f.vcodec!=="none"&&f.vcodec!==""&&f.vcodec!=="-"&&(!f.acodec||f.acodec==="none"||f.acodec===""||f.acodec==="-");}
function isAudioFormat(f){return f.acodec&&f.acodec!=="none"&&f.acodec!==""&&f.acodec!=="-"&&(!f.vcodec||f.vcodec==="none"||f.vcodec===""||f.vcodec==="-");}
function isVideoOnly(f){return f.vcodec&&f.vcodec!=="none"&&f.vcodec!==""&&f.vcodec!=="-"&&(!f.acodec||f.acodec==="none"||f.acodec===""||f.acodec==="-");}

function toggleMerge(){
  mergeActive=!mergeActive;var chip=document.getElementById("mergeChip");chip.classList.toggle("active",mergeActive);chip.classList.toggle("merge-chip-active",mergeActive);
  var slots=document.getElementById("mergeSlots");if(slots)slots.style.display=mergeActive?"flex":"none";
  // Auto-show format panels when toggling merge
  if(mergeActive&&!formatsVisible){toggleFormatsView();}
  if(!mergeActive){
    clearMergeSlots();
    if(formatsVisible){renderSingleTable();}
    else{document.getElementById("singleFormatPanel").style.display="none";}
  }
  else{
    selectedFormat=null;document.querySelectorAll(".btn-chip").forEach(function(b){b.classList.remove("active");b.classList.remove("merge-chip-active");});
    if(formatsVisible){renderMergePanels();}
  }
}

function clearMergeSlots(){mergeVideoId=null;mergeAudioId=null;document.querySelectorAll("#videoBody tr.selected").forEach(function(r){r.classList.remove("selected");});document.querySelectorAll("#audioBody tr.selected").forEach(function(r){r.classList.remove("selected");});var v=document.getElementById("mergeVideoSlot");var a=document.getElementById("mergeAudioSlot");if(v)v.textContent="-";if(a)a.textContent="-";}

async function startDownload(){
  if(!formatsFetched){setStatus("fetch formats first — paste a URL and hit fetch","error");return}
  var url=document.getElementById("urlInput").value.trim();if(!url){setStatus("paste a URL first");return}
  var fid=selectedFormat;var preset=selectedFormat?null:selectedPreset;
  if(mergeActive){if(!mergeVideoId||!mergeAudioId){setStatus("pick a video AND audio format","error");return;}fid=mergeVideoId+"+"+mergeAudioId;preset=null;}
  // Check size limit for selected format
  if(mergeActive){
    if(mergeVideoId){var mv=allFormats.find(function(f){return f.id===mergeVideoId;});if(mv&&isOverLimit(mv.filesize)){setStatus("selected video format is over the 1.5GB limit — please pick another format","error");return;}}
    if(mergeAudioId){var ma=allFormats.find(function(f){return f.id===mergeAudioId;});if(ma&&isOverLimit(ma.filesize)){setStatus("selected audio format is over the 1.5GB limit — please pick another format","error");return;}}
  } else if(selectedFormat){
    var selFmt=allFormats.find(function(f){return f.id===selectedFormat;});
    if(selFmt&&isOverLimit(selFmt.filesize)){setStatus("this format is over the 1.5GB limit — please pick another format","error");return;}
  }
  downloading=true;document.getElementById("downloadBtn").disabled=true;document.getElementById("cancelBtn").disabled=false;
  document.getElementById("pasteBtn").disabled=true;document.getElementById("fetchBtn").disabled=true;
  setProgress(0);
  var dlLabel=selectedFormat||selectedPreset||"best";
  showDlIndicator("downloading "+dlLabel+"...");
  setStatus("downloading...");startPolling();
  try{await fetch("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:url,format_id:fid,preset:preset})});}catch(e){setStatus("error","error");resetUI();}
}

function startPolling(){if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(async function(){try{var r=await fetch("/api/progress");var d=await r.json();if(d.percent!==undefined){setProgress(d.percent);var s=d.percent.toFixed(0)+"%";if(d.size)s+=" - "+d.size;if(d.speed)s+=" - "+d.speed;if(d.eta)s+=" - ETA "+d.eta;setStatus(s);}if(d.done){setProgress(100);setStatus("done","success");hideDlIndicator();stopPolling();resetUI();setTimeout(function(){setProgress(0);setStatus("ready");},3000);}if(d.error){setStatus("error","error");hideDlIndicator();stopPolling();resetUI();setTimeout(function(){setProgress(0);setStatus("ready");},3000);}}catch(e){hideDlIndicator();}},400);}
function stopPolling(){if(pollTimer){clearInterval(pollTimer);pollTimer=null;}}
async function cancelDownload(){downloading=false;stopPolling();try{await fetch("/api/cancel",{method:"POST"});}catch(e){}hideDlIndicator();setStatus("cancelled");setProgress(0);resetUI();}
function resetUI(){downloading=false;document.getElementById("downloadBtn").disabled=!formatsFetched;document.getElementById("cancelBtn").disabled=true;document.getElementById("pasteBtn").disabled=false;document.getElementById("fetchBtn").disabled=false;}

var urlTypingTimer=null;
document.getElementById("urlInput").addEventListener("input",function(){clearTimeout(urlTypingTimer);var v=this.value.trim();if(!v)return;if(v.includes("youtube.com")||v.includes("youtu.be"))setStatus("looks like YouTube - hit fetch");else if(v.startsWith("http"))setStatus("hit fetch to try this URL");else return;urlTypingTimer=setTimeout(function(){setStatus("ready");},1800);});

// ── Downloading indicator ──
function showDlIndicator(text){
  var el=document.getElementById("dlIndicator");
  if(!el)return;
  document.getElementById("dlIndicatorText").textContent=text;
  el.classList.add("visible");
  // Also update status bar
  setStatus(text,"downloading");
}
function hideDlIndicator(){
  var el=document.getElementById("dlIndicator");
  if(el)el.classList.remove("visible");
}

// ── Donate modal ──
function showDonate(){
  var el=document.getElementById("donateOverlay");
  el.classList.remove("closing");
  el.classList.add("open");
}
function hideDonate(){
  var el=document.getElementById("donateOverlay");
  el.classList.add("closing");
  setTimeout(function(){
    el.classList.remove("open");
    el.classList.remove("closing");
  }, 250);
}

// ── Praise dropdown toggle (animated) ──
var praiseOpen=false;
var praiseAnimating=false;
function togglePraise(){
  if(praiseAnimating)return;
  praiseOpen=!praiseOpen;
  var dropdown=document.getElementById("praiseDropdown");
  var arrow=document.getElementById("praiseArrow");
  var box=document.getElementById("praiseBox");
  if(praiseOpen){
    praiseAnimating=true;
    dropdown.style.display="block";
    dropdown.style.opacity="0";
    dropdown.style.transform="translateY(-8px)";
    dropdown.style.transition="opacity .25s ease,transform .25s ease";
    // Force reflow
    dropdown.offsetHeight;
    dropdown.style.opacity="1";
    dropdown.style.transform="translateY(0)";
    arrow.style.transform="rotate(180deg)";
    box.style.borderRadius="12px 12px 0 0";
    box.style.borderBottom="none";
    setTimeout(function(){praiseAnimating=false;},250);
  } else {
    praiseAnimating=true;
    dropdown.style.opacity="0";
    dropdown.style.transform="translateY(-8px)";
    arrow.style.transform="rotate(0deg)";
    box.style.borderRadius="12px";
    box.style.borderBottom="";
    setTimeout(function(){
      dropdown.style.display="none";
      dropdown.style.opacity="";
      dropdown.style.transform="";
      dropdown.style.transition="";
      praiseAnimating=false;
    },250);
  }
}

// ── Settings menu ──
var settingsOpen=false;
function showSettings(tab){
  settingsOpen=true;
  var el=document.getElementById("settingsOverlay");
  el.classList.remove("closing");
  el.classList.add("open");
  switchSettingsTab(tab||"faq");
}
function hideSettings(){
  if(!settingsOpen)return;
  settingsOpen=false;
  var el=document.getElementById("settingsOverlay");
  el.classList.add("closing");
  setTimeout(function(){
    el.classList.remove("open");
    el.classList.remove("closing");
  }, 250);
}
document.getElementById("settingsOverlay").addEventListener("click",function(e){if(e.target===this)hideSettings();});

function switchSettingsTab(tab){
  document.getElementById("settingsFaqTab").style.display=tab==="faq"?"":"none";
  document.getElementById("settingsLegalTab").style.display=tab==="legal"?"":"none";
  document.getElementById("settingsPrivacyTab").style.display=tab==="privacy"?"":"none";
  document.getElementById("settingsTabFaq").classList.toggle("active",tab==="faq");
  document.getElementById("settingsTabLegal").classList.toggle("active",tab==="legal");
  document.getElementById("settingsTabPrivacy").classList.toggle("active",tab==="privacy");
}

window.addEventListener("beforeunload",function(){navigator.sendBeacon("/api/shutdown");});
</script>
<!-- Settings menu overlay -->
<div class="log-overlay" id="settingsOverlay">
  <div class="log-header"><h3>Settings</h3><button class="log-close" onclick="hideSettings()">close</button></div>
  <div class="log-body" style="max-width:650px;margin:0 auto">
    <div style="display:flex;gap:8px;margin-bottom:20px;border-bottom:1px solid var(--border);padding-bottom:12px">
      <button class="btn btn-chip active" id="settingsTabFaq" onclick="switchSettingsTab('faq')">FAQ</button>
      <button class="btn btn-chip" id="settingsTabLegal" onclick="switchSettingsTab('legal')">legal &amp; terms</button>
      <button class="btn btn-chip" id="settingsTabPrivacy" onclick="switchSettingsTab('privacy')">privacy</button>
    </div>

    <!-- FAQ tab -->
    <div id="settingsFaqTab" class="legal-content" style="font-size:13px;line-height:1.7;color:var(--text-dim)">
      <h4>1. What is Tether?</h4>
      <p>Tether is a web-based video downloading tool that lets you save videos from multiple platforms. Simply paste a URL, pick a format, and download — no accounts, no tracking, no data collection.</p>

      <h4>2. Is Tether safe to use?</h4>
      <p>Yes. Tether is a web-based tool that runs on a server and is accessed through your browser. It does not collect personal data, does not use cookies or tracking, and does not communicate with any external services beyond what is needed to fetch video metadata and content directly from the source platform.</p>

      <h4>3. Is there a file size limit?</h4>
      <p>Yes. Individual video downloads are limited to files under <strong>1.5 GB</strong>. Formats that exceed this limit will be marked with a warning and cannot be downloaded. This per-video limit helps prevent excessive resource usage.</p>

      <h4>4. Do you support playlist downloads?</h4>
      <p>Yes, Tether supports downloading playlists. The 1.5 GB file size limit applies to <strong>each individual video</strong> within the playlist, not the total playlist size.</p>

      <h4>5. Why can\'t I download a particular format?</h4>
      <p>Some formats may be unavailable for download if they exceed the file size limit, if the source platform has restricted access, or if the format requires DRM-decrypted streams. If a download fails, try selecting a different format or quality level.</p>

      <h4>6. What platforms are supported?</h4>
      <p>Tether supports multiple popular platforms including YouTube, TikTok, Instagram, Twitter/X, Reddit, Twitch, Vimeo, SoundCloud, and more. If a URL is not recognized, Tether will still attempt to process it.</p>
    </div>

    <!-- Legal & Terms tab -->
    <div id="settingsLegalTab" class="legal-content" style="display:none;font-size:13px;line-height:1.7;color:var(--text-dim)">
      <h4>1. disclaimer</h4>
      <p>Tether is a web-based video downloading service. it is provided strictly for personal, non-commercial use. you are solely and entirely responsible for how you use this service. the developers, contributors, and distributors of Tether assume no liability whatsoever for any misuse, damages, or legal consequences arising from your use of this service.</p>
      <p>downloading copyrighted content without the explicit permission of the rights holder may violate copyright laws in your jurisdiction, including but not limited to the Digital Millennium Copyright Act (DMCA) in the United States, the Copyright Directive in the European Union, and similar legislation worldwide. it is your responsibility to ensure your use complies with all applicable laws.</p>

      <h4>2. copyright notice &amp; DMCA</h4>
      <p>Tether does not host, store, cache, index, or distribute any media files, videos, audio, or content of any kind. it is a web-based service that facilitates downloads from third-party platforms. all content remains on the respective third-party servers.</p>
      <p>if you believe any content accessible through this tool infringes your copyright, you must contact the hosting platform directly (e.g. YouTube\'s DMCA takedown process at youtube.com/copyright). the developers of Tether have no control over third-party content and cannot process copyright removal requests.</p>
      <p>Tether respects the rights of content creators. you should only download content you have the right to access, including: content in the public domain, content licensed under Creative Commons, content you own, or content explicitly offered for download by the creator.</p>

      <h4>3. file size limits</h4>
      <p>to ensure reliable performance and prevent abuse, Tether enforces a maximum file size limit of 1.5 GB per individual video download. Formats exceeding this limit will be flagged in the format list and cannot be downloaded. For playlist downloads, this limit applies to each video individually, not the aggregate playlist total.</p>

      <h4>4. terms and conditions of use</h4>
      <p>by downloading, installing, or using Tether, you agree to the following terms:</p>
      <ul>
        <li><strong>age requirement.</strong> you must be at least 13 years of age, or the minimum age required in your jurisdiction, to use Tether. if you are under 18, you may only use Tether with the consent and supervision of a parent or legal guardian.</li>
        <li><strong>personal use only.</strong> you will not use Tether for commercial redistribution, resale, or any form of mass downloading for profit.</li>
        <li><strong>no circumvention.</strong> you will not use Tether to circumvent digital rights management (DRM), access controls, or any technological protection measures.</li>
        <li><strong>compliance with platform ToS.</strong> your use of third-party platforms through Tether is subject to their respective terms of service. Tether is not affiliated with, endorsed by, or sponsored by any supported platform.</li>
        <li><strong>no warranty.</strong> Tether is provided "as is" without warranty of any kind, express or implied, including but not limited to merchantability, fitness for a particular purpose, or non-infringement.</li>
        <li><strong>limitation of liability.</strong> in no event shall the developers be liable for any direct, indirect, incidental, special, consequential, or punitive damages arising from your use of Tether.</li>
        <li><strong>indemnification.</strong> you agree to indemnify and hold harmless the developers from any claims, damages, or expenses arising from your use of Tether.</li>
        <li><strong>modifications.</strong> the developers reserve the right to modify these terms at any time. continued use constitutes acceptance of updated terms.</li>
        <li><strong>termination.</strong> the developers reserve the right to terminate or restrict access to Tether at any time, for any reason, without notice.</li>
        <li><strong>governing law.</strong> these terms shall be governed by and construed in accordance with applicable laws, without regard to conflict of law principles.</li>
        <li><strong>severability.</strong> if any provision of these terms is found to be unenforceable, the remaining provisions shall remain in full effect.</li>
        <li><strong>entire agreement.</strong> these terms constitute the entire agreement between you and the developers regarding the use of Tether.</li>
      </ul>

      <h4>5. third-party services</h4>
      <p>Tether interacts with third-party video platforms to facilitate downloads. your use of those platforms is subject to their respective terms of service and privacy policies. Tether is not responsible for the practices of any third-party platform.</p>

      <h4>6. contact</h4>
      <p>for questions, bug reports, legal inquiries, or DMCA notices, please contact us at <a href="mailto:tetherweb.help@gmail.com" style="color:var(--accent)">tetherweb.help@gmail.com</a></p>
    </div>

    <!-- Privacy tab -->
    <div id="settingsPrivacyTab" class="legal-content" style="display:none;font-size:13px;line-height:1.7;color:var(--text-dim)">
      <h4>Privacy Policy</h4>
      <p>Tether is designed with privacy as a core principle:</p>
      <ul>
        <li><strong>no data collection.</strong> Tether does not collect, transmit, or store any personal data, usage statistics, telemetry, or analytics. all video fetching and downloading is handled server-side, and no user activity logs are retained.</li>
        <li><strong>minimal network communication.</strong> Tether only communicates with third-party video platforms to fetch and download content. it makes no other external network requests on your behalf.</li>
        <li><strong>local storage only.</strong> Tether uses no cookies, no tracking pixels, and no fingerprinting. your preferences are stored only in your browser\'s session.</li>
        <li><strong>no accounts.</strong> Tether does not require user accounts, registration, or authentication of any kind.</li>
        <li><strong>third-party connections.</strong> when downloading, connections are made directly to third-party platforms. your IP address and request headers are visible to those platforms. Tether has no control over how third-party platforms handle your data.</li>
      </ul>
    </div>

  </div>
</div>

</body>
</html>"""

# Inject icon
if ICON_B64:
    icon_html = '<img class="icon-img" src="' + ICON_B64 + '" alt="icon">'
else:
    icon_html = '<span style="color:var(--text);font-size:20px;opacity:.7">&#10022;</span>'
HTML = HTML.replace('__ICON_PLACEHOLDER__', icon_html)


def _run_yt_dlp(cmd, timeout=60):
    """Run yt-dlp and return the CompletedProcess result."""
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=_NO_WINDOW, timeout=timeout
    )

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _get_body(self):
        length = self.headers.get('Content-Length')
        if not length: return b''
        try:
            length = int(length)
        except (ValueError, TypeError):
            self.send_response(400); self.end_headers(); return None
        if length < 0 or length > _MAX_POST:
            self.send_response(413); self.end_headers(); return None
        return self.rfile.read(length)

    def do_GET(self):
        if not _check_rate(self.headers.get('X-Real-IP', self.client_address[0])):
            self.send_response(429); self.end_headers(); return
        path = urlparse(self.path).path.rstrip('/') or '/'
        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('X-Content-Type-Options', 'nosniff')
            # CORS: allow configured origins in production
            allowed_origins = os.environ.get('ALLOWED_ORIGINS', '')
            if allowed_origins:
                origin = self.headers.get('Origin', '')
                if origin and origin in allowed_origins.split(','):
                    self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
        elif path == '/api/config':
            self._json({"downloads_dir": get_downloads_dir()})
        elif path == '/api/progress':
            session_id = self.headers.get('X-Session-ID', 'server')
            self._json(get_progress(session_id))
        elif path == '/api/log':
            session_id = self.headers.get('X-Session-ID', 'server')
            self._json({"lines": get_log_lines(session_id)})
        else:
            self.send_response(404); self.end_headers()

    def _is_local_request(self):
        """CSRF guard. In production behind a reverse proxy, all requests
        arrive on 127.0.0.1. We rely on Origin/SameSite checks instead
        of IP-based gating."""
        origin = self.headers.get('Origin', '')
        referer = self.headers.get('Referer', '')
        for hdr in (origin, referer):
            if hdr:
                try:
                    parsed = urlparse(hdr)
                    host = parsed.hostname or ''
                    # Allow same-origin, localhost, and common proxy setups
                    allowed = ('127.0.0.1', 'localhost', '0.0.0.0', '')
                    if host not in allowed and not host.startswith('127.'):
                        return False
                except Exception:
                    pass
        return True

    def do_POST(self):
        if not _check_rate(self.headers.get('X-Real-IP', self.client_address[0])):
            self.send_response(429); self.end_headers(); return
        # CSRF guard: reject cross-origin POSTs
        if not self._is_local_request():
            self.send_response(403); self.end_headers(); return
        path = urlparse(self.path).path.rstrip('/')
        body = self._get_body()
        if body is None: return
        # Require JSON Content-Type to prevent CSRF via form submissions
        ct = self.headers.get('Content-Type', '')
        if 'application/json' not in ct.lower():
            self.send_response(415); self.end_headers(); return

        if path == '/api/formats':
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                self.send_response(400); self.end_headers(); return
            url = _sanitize_url(data.get('url', ''))
            if not url: self._json({"error": "invalid URL"}); return
            platform, _ = detect_platform(url)
            if not platform: self._json({"error": "not a recognized video URL"}); return
            try:
                cmd = [sys.executable, "-m", "yt_dlp", "-F", "--no-playlist",
                       "--no-progress", "--no-warnings", "--socket-timeout", "30",
                       "--cookies-from-browser", "firefox", url]
                result = _run_yt_dlp(cmd, timeout=60)
                combined = result.stdout + "\n" + result.stderr
                formats = _parse_formats(combined)
                if result.returncode != 0 and not formats:
                    err = (result.stderr or result.stdout or "yt-dlp returned an error")[:200]
                    self._json({"error": err}); return
                self._json({"formats": formats})
            except subprocess.TimeoutExpired:
                self._json({"error": "request timed out — the video may be unavailable"})
            except Exception as e:
                self._json({"error": str(e)[:200]})

        elif path == '/api/download':
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                self.send_response(400); self.end_headers(); return
            url = _sanitize_url(data.get('url', ''))
            if not url: self._json({"error": "invalid URL"}); return
            # ── Storage pre-check ──
            free = _get_free_space(get_downloads_dir())
            if free is not None and free < _MIN_FREE_BYTES:
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Retry-After', '300')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "Sorry we had to stop your download since our servers are currently on high loads, please check back later."
                }).encode())
                return
            fmt_id = data.get('format_id')
            preset = data.get('preset', 'Best (Video + Audio)')
            if fmt_id:
                fmt_id = _sanitize_format_id(fmt_id)
                if not fmt_id: self._json({"error": "invalid format ID"}); return
            session_id = 'server'
            clear_log(session_id); reset_progress(session_id); update_progress(session_id, active=True)
            append_log("Starting download: " + url)
            if fmt_id:
                fmt_args = ["-f", fmt_id]
            else:
                fmt_map = {
                    "Best (Video + Audio)": ["-f", "bestvideo+bestaudio/best"],
                    "1080p": ["-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]"],
                    "720p":  ["-f", "bestvideo[height<=720]+bestaudio/best[height<=720]"],
                    "480p":  ["-f", "bestvideo[height<=480]+bestaudio/best[height<=480]"],
                    "360p":  ["-f", "bestvideo[height<=360]+bestaudio/best[height<=360]"],
                }
                fmt_args = fmt_map.get(preset, ["-f", "best"])
            if fmt_id and "+" in fmt_id:
                fmt_args = fmt_args + ["--merge-output-format", "mp4"]
            else:
                fmt_args = fmt_args + ["--merge-output-format", "mp4"]
            cmd = [
                sys.executable, "-m", "yt_dlp", "--newline",
                "--progress-template",
                "%(progress._percent_str)s|%(progress._total_bytes_str)s|"
                "%(progress._speed_str)s|%(progress._eta_str)s",
                *fmt_args,
                "-o", os.path.join(get_downloads_dir(), "%(title)s.%(ext)s"),
                "--no-playlist", "--restrict-filenames",
                "--socket-timeout", "30",
                "--max-filesize", "1610612736",
                "--cookies-from-browser", "firefox",
                url,
            ]
            def run_dl():
                global active_processes
                try:
                    with process_lock:
                        proc = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=_NO_WINDOW
                        )
                    import time as _tm
                    dl_start = _tm.time()
                    MAX_DL_TIME = 3600  # 1 hour max per download
                    active_processes[session_id] = proc
                    for line in proc.stdout:
                        if _tm.time() - dl_start > MAX_DL_TIME:
                            proc.terminate()
                            append_log(session_id, "Download exceeded maximum time (1 hour) — terminated")
                            update_progress(error="download timed out (exceeded 1 hour)")
                            break
                        line = line.strip(); append_log(line)
                        if not line: continue
                        if "|" in line and "%" in line:
                            parts = line.split("|")
                            try:
                                pct = float(parts[0].strip().replace("%", "").strip())
                                kw = {"percent": pct}
                                if len(parts)>1 and parts[1].strip(): kw["size"]=parts[1].strip()
                                if len(parts)>2 and parts[2].strip(): kw["speed"]=parts[2].strip()
                                if len(parts)>3 and parts[3].strip(): kw["eta"]=parts[3].strip()
                                update_progress(session_id, **kw)
                            except (ValueError, IndexError): pass
                        elif "error" in line.lower():
                            clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
                            update_progress(session_id, error=clean[:80])
                    else:
                        proc.wait()
                        if proc.returncode == 0:
                            append_log(session_id, "Download complete.")
                            update_progress(session_id, done=True, percent=100)
                        else:
                            append_log(session_id, "Download failed (exit code %d)" % proc.returncode)
                            update_progress(session_id, error="download failed (exit code %d)" % proc.returncode)
                except Exception as e:
                    append_log(session_id, "Error: " + str(e)[:80])
                    update_progress(session_id, error=str(e)[:60])
                finally:
                    with process_lock:
                        active_processes.pop(session_id, None)
            threading.Thread(target=run_dl, daemon=True).start()
            self._json({"ok": True})

        elif path == '/api/open_folder':
            folder = get_downloads_dir()
            os.makedirs(folder, exist_ok=True)
            try:
                if os.name == "nt": os.startfile(folder)
                else: subprocess.run(["xdg-open", folder], capture_output=True, timeout=5)
            except Exception: pass
            self._json({"ok": True})

        elif path == '/api/shutdown':
            with process_lock:
                for sid, proc in list(active_processes.items()):
                    if proc.poll() is None:
                        proc.terminate()
                active_processes.clear()
            self._json({"ok": True})
            threading.Thread(target=lambda: (_shutdown_event.set(),), daemon=True).start()

        elif path == '/api/cancel':
            session_id = self.headers.get('X-Session-ID', 'server')
            with process_lock:
                proc = active_processes.get(session_id)
                if proc and proc.poll() is None:
                    proc.terminate()
                    active_processes.pop(session_id, None)
            reset_progress(session_id); self._json({"ok": True})

        else:
            self.send_response(404); self.end_headers()

    def _json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

_shutdown_event = threading.Event()

# ── Storage safety constants ──────────────────────────────────
_MIN_FREE_BYTES = 5 * 1024 ** 3          # 5 GB minimum free space
_FILE_MAX_AGE_SEC = 3600                  # 60 minutes — delete files older than this
_CLEANUP_INTERVAL_SEC = 300               # run cleanup sweep every 5 minutes

def _get_free_space(path):
    """Return free disk space in bytes for the volume containing *path*."""
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return None  # indeterminate — allow download to proceed

def _cleanup_old_files():
    """Delete files in DOWNLOADS_DIR older than _FILE_MAX_AGE_SEC."""
    try:
        now = _time_mod.time()
        for entry in os.scandir(get_downloads_dir()):
            try:
                if entry.is_file(follow_symlinks=False) and \
                   now - entry.stat().st_mtime > _FILE_MAX_AGE_SEC:
                    os.remove(entry.path)
            except Exception:
                pass  # keep going even if one file fails
    except Exception:
        pass

def _cleanup_worker():
    """Background thread: sleep → sweep → repeat until shutdown."""
    while not _shutdown_event.is_set():
        _shutdown_event.wait(timeout=_CLEANUP_INTERVAL_SEC)
        if _shutdown_event.is_set():
            break
        _cleanup_old_files()

# Start the background cleanup daemon
_cleanup_thread = threading.Thread(target=_cleanup_worker, daemon=True)
_cleanup_thread.start()

def kill_existing_tether():
    """Kill other Tether instances. Cross-platform."""
    try:
        current_pid = os.getpid()
        if os.name == 'nt':
            # Windows: use tasklist + taskkill
            result = subprocess.run(
                ['tasklist', '/FI', 'IMAGENAME eq python*', '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or 'Tether_Web' not in line: continue
                parts = line.split(',')
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1].strip('"'))
                        if pid != current_pid:
                            subprocess.run(
                                ['taskkill', '/F', '/PID', str(pid)],
                                capture_output=True, timeout=5,
                                creationflags=_NO_WINDOW
                            )
                    except (ValueError, IndexError): pass
        else:
            # Linux/Mac: use pgrep + kill
            result = subprocess.run(
                ['pgrep', '-af', 'Tether_Web'],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                try:
                    pid = int(line.split()[0])
                    if pid != current_pid:
                        subprocess.run(['kill', '-9', str(pid)],
                                       capture_output=True, timeout=5)
                except (ValueError, IndexError): pass
    except Exception: pass

def main():
    kill_existing_tether()
    if not check_yt_dlp(): install_yt_dlp()
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", os.environ.get("TETHER_PORT", 3187)))
    host = os.environ.get("TETHER_HOST", "127.0.0.1")
    server = HTTPServer((host, port), Handler)
    server.socket.settimeout(1.0)
    # Auto-open browser only if DISPLAY is available (not headless)
    if os.environ.get("DISPLAY") or os.name == "nt":
        try: webbrowser.open(f"http://{host}:{port}/")
        except Exception: pass
    def serve():
        while not _shutdown_event.is_set():
            try: server.handle_request()
            except socket.timeout: continue
    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    # Signal handler for systemd / supervisor graceful shutdown
    def _handle_signal(signum, frame):
        _shutdown_event.set()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try: _shutdown_event.wait()
    except KeyboardInterrupt: pass
    finally:
        with process_lock:
            for sid, proc in list(active_processes.items()):
                if proc.poll() is None:
                    proc.terminate()
            active_processes.clear()
        server.shutdown()

if __name__ == "__main__": main()
