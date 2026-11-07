"""Remote browser: Chromium in a Hub-managed container, driven over CDP.

docs/container-runtime.md section 4. The phone never talks to the remote
site: the Hub holds a fixed set of Chrome DevTools Protocol commands (no raw
passthrough), streams JPEG screencast frames (latest frame only, in RAM) and
translates a small validated set of inputs. Memory is released aggressively:
a tab close closes its renderer, an idle container is stopped and removed.

Stdlib only (minimal RFC 6455 client below). Python 3.8 compatible. Real
Container Station / Chromium behaviour is unverified (no NAS during
development); everything external is injectable for tests.
"""
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import struct
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import browser_safety
import container_runtime as cr
import debuglog
import diskio
import rapidgator_extract
import rb_blocklist
import rb_firstload
import rb_mirror
import rb_resident

MAX_TABS = 3
MIN_AVAILABLE_KIB = cr.BROWSER_MIN_AVAILABLE_KIB     # MemAvailable >= 2 GiB
MEM_LIMIT_PERCENT = 80.0
STATS_TTL = 5.0
STATUS_STATS_TTL = 30.0        # v0.1.46: status(fresh_memory=True) reuses a docker stats result this long
STATS_STALE_MAX = 120.0        # v0.1.46: status() without fresh_memory shows a cached result up to this old
VIEWER_STOP_AFTER = 5.0        # stop the screencast after this long without a poll
NO_TAB_IDLE = 300.0            # no tabs for 5 minutes -> stop the container
NO_VIEWER_IDLE = 600.0         # no viewer activity for 10 minutes -> stop
MONITOR_INTERVAL = 5.0
HEALTH_INTERVAL = 30.0         # while running: ask docker whether the container still runs
TRIM_TIMEOUT = 5.0             # resident: memory trim commands are best-effort
FRAME_WAIT_MAX = 10.0
MIN_FRAME_INTERVAL = 0.1       # at most 10 frames per second
START_TIMEOUT = 45.0
CDP_TIMEOUT = 15.0
WS_MAX_MESSAGE = 8 * 1024 * 1024
WS_MAX_SEND = 1024 * 1024
FRAME_MAX_CHARS = 6 * 1024 * 1024
LINK_CAP = 5000
LINK_RESULT_CAP = rapidgator_extract.HARD_LIMIT
# Per-session render quality (v0.1.24): device scale factor cap and JPEG quality.
# The default favours speed: a 1x page is a quarter of the pixels of a 2x one.
QUALITY_MODES = {"speed": (1.0, 40), "standard": (1.5, 55), "high": (2.0, 70)}
DEFAULT_QUALITY = "speed"
SCREENCAST = {"format": "jpeg", "quality": 40, "maxWidth": 1280, "maxHeight": 2000,
              "everyNthFrame": 1}
DEFAULT_SIZE = (1280, 2000)
WINDOW_SIZE = (1600, 2400)     # matches the Xvfb screen: emulated viewports always fit
VIEWPORT_W = (320, 1600)       # CSS pixels accepted from the viewer
VIEWPORT_H = (320, 2400)
VIEWPORT_DPR = (1.0, 3.0)
MOBILE_WIDTH = 760             # narrower viewports are emulated as a touch device
REDUCED_MOTION = {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]}
# Hoster pages that are intercepted (aborted) instead of rendered. Only these
# Document requests are ever paused; every other request is untouched.
FETCH_PATTERNS = tuple({"urlPattern": pattern, "resourceType": "Document"} for pattern in (
    "*://rapidgator.net/file/*", "*://*.rapidgator.net/file/*", "*://rg.to/file/*"))
# v0.1.44 self-test: the internal fixture tab loads a virtual site that is answered from the Hub's RAM
# (Fetch.fulfillRequest); nothing is fetched from the network. A real, valid-looking public https name
# is used so the normal validators (mirror, reader) treat the page like any other.
SELFTEST_ORIGIN = "https://selftest.example.com"
SELFTEST_TABS = 1              # self-test tabs open at once (they do not count against MAX_TABS)
INTERCEPT_CAP = 20             # events kept per tab (RAM only)
INTERCEPT_DEDUP = 2.0          # an identical event within this many seconds is one event
BYPASS_TTL = 30.0              # a "page open" bypass is valid for one navigation, this long
POPUP_WAIT = 3.0               # popups: wait this long for the first URL
# Long-press probe (v0.1.24): a hidden background target follows the link.
MAX_PROBES = 4                 # at most this many probe targets exist at once (all tabs)
MAX_PROBES_PER_TAB = 3         # ... and this many per tab; the rest wait in a FIFO queue
PROBE_QUEUE_PER_TAB = 10       # waiting probes per tab; more are refused (HTTP 429)
PROBE_QUEUE_WAIT = 30.0        # a probe waits this long for the memory guard to clear
PROBE_MIN_AVAILABLE_KIB = 1024 * 1024    # probe guard: MemAvailable >= 1 GiB
PROBE_MEM_LIMIT_PERCENT = 85.0           # probe guard: container usage < 85%
PROBE_TIMEOUT = 15.0           # give up waiting for a hoster URL after this long
PROBE_SETTLE = 4.0             # a loaded page with no navigation for this long is final
PROBE_LINK_CAP = 50
PROBE_CDP_TIMEOUT = 8.0
LINK_NEAR_PX = 28              # a press this close to a link box (CSS px) still counts as on the link
# Long-press resolver (v0.1.30): the link under the point, else the nearest anchor box (every client
# rect) within LINK_NEAR_PX; equal distance -> the smaller box. Fixed script, numbers only.
PROBE_JS = (
    "(function(x,y,T){var e=document.elementFromPoint(x,y);"
    "var a=e&&e.closest?e.closest('a[href]'):null;"
    "if(a&&typeof a.href==='string')return a.href;"
    "var l=document.querySelectorAll('a[href]'),b=null,bd=1e9,ba=1e18;"
    "for(var i=0;i<l.length&&i<5000;i++){var h=l[i].href;if(typeof h!=='string')continue;"
    "var rs=l[i].getClientRects(),c=null;"
    "for(var j=0;j<rs.length;j++){var r=rs[j];if(r.width<2||r.height<2)continue;"
    "var dx=Math.max(r.left-x,0,x-r.right),dy=Math.max(r.top-y,0,y-r.bottom),"
    "d=Math.sqrt(dx*dx+dy*dy);if(d>T)continue;"
    "if(c===null)c=getComputedStyle(l[i]);if(c.visibility==='hidden'||c.display==='none')break;"
    "var ar=r.width*r.height;if(d<bd||(d===bd&&ar<ba)){b=h;bd=d;ba=ar;}}}"
    "return b;})(%.1f,%.1f,%d)")
LOCATION_JS = "String(location.href)"
EDITABLE_JS = ("(()=>{const e=document.activeElement;return !!e&&(e.isContentEditable||"
               "/^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName))})()")
EVENT_EXTRAS = ("where", "dup")
POPUP_BLOCKED = "ポップアップをブロックしました"
SETUP_CACHE_TTL = 30.0
CS_CACHE_TTL = 60.0
WS_PATH = re.compile(r"/devtools/browser/[A-Za-z0-9-]{1,100}\Z")
TARGET_ID = re.compile(r"[A-Za-z0-9]{8,64}\Z")
TAB_ID = re.compile(r"b[0-9]{1,6}\Z")
BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}\Z")

LOG_LINES = 200
TOAST_TAIL_CHARS = 300
UPDATE_NEEDED = "NASブラウザの更新が必要です（初期設定を再実行）"

STEP_DEFS = (
    ("precheck", "Container Stationの検出と事前確認"),
    ("network", "専用ネットワークの作成"),
    ("build", "ブラウザイメージの作成（Chromiumの取得。数分かかります）"),
    ("sandbox", "Chromiumサンドボックスの確認"),
    ("done", "完了"),
)

# key -> (code, Windows virtual key code, text sent with keyDown)
KEYS = {
    "Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, None),
    "Tab": ("Tab", 9, None), "Escape": ("Escape", 27, None),
    "Delete": ("Delete", 46, None),
    "ArrowLeft": ("ArrowLeft", 37, None), "ArrowUp": ("ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", 39, None), "ArrowDown": ("ArrowDown", 40, None),
}
INPUT_TYPES = ("tap", "scroll", "text", "key", "back", "forward", "reload")
MAX_COORD = 20000.0
MAX_SCROLL = 5000.0
MAX_TEXT = 500

# Fast full-page display (v0.1.28): the page is measured, then rendered lazily as JPEG
# tiles that the phone scrolls natively. Tiles live in RAM only (never on disk).
SNAP_TILE = 1600               # CSS pixels per tile (the last one may be shorter)
SNAP_MAX_HEIGHT = 30000        # CSS pixels captured at most; the rest of the page is cut off
SNAP_CLIP_SCALE = 1.0          # clip.scale. The device scale factor already comes from the
                               # emulated viewport; scale=dsf here would multiply it twice.
SNAP_MAX_TILES = 40            # cached tiles per tab (LRU)
SNAP_MAX_BYTES = 24 * 1024 * 1024
SNAP_MAX_TILE_BYTES = 12 * 1024 * 1024
SNAP_KEEP = 2                  # snapshots kept per tab (the previous one until the new one has a tile)
SNAP_REUSE = 2.0               # an unchanged snapshot younger than this is returned as is
SNAP_LINK_CAP = 3000
SNAP_INPUT_CAP = 300
SNAP_CDP_TIMEOUT = 20.0
TAP_NAV_WAIT = 0.6             # after a click: wait this long for a navigation to start
TAP_SETTLE_MAX = 10.0          # ... and at most this long for it to finish
META_WAIT_MAX = 10.0
FIRST_WAIT_MAX = 8.0           # v0.1.59: a tab-open / navigate request may wait this long for the first copy
FIRST_WAITERS_MAX = 4          # v0.1.59: at most this many requests wait for a first copy at once
SNAP_ID = re.compile(r"s[0-9]{1,9}\Z")
SNAP_URL_MAX = 2048
CHALLENGE_TITLES = ("just a moment", "しばらくお待ちください")
CHALLENGE_URL = "/cdn-cgi/challenge"
# Page-side check (v0.1.29): Cloudflare's interstitial keeps its normal title on some
# sites, so the DOM is asked too. Re-evaluated after every load event, at most once per second.
# v0.1.37: a Turnstile widget inside an ordinary page (comment or login form) is not an
# interstitial: it only counts when the page has almost no text of its own.
CHALLENGE_JS = ("!!(window._cf_chl_opt||document.querySelector('#challenge-form,#challenge-stage')||"
                "(document.querySelector('.cf-turnstile,iframe[src*=\"challenges.cloudflare.com\"]')&&"
                "((document.body&&document.body.innerText)||'').length<400))")
CHALLENGE_CHECK_INTERVAL = 1.0
SNAP_JS = (
    "(function(){try{if(!window.__qdlMut){var m=new MutationObserver(function(){window.__qdlDirty=true});"
    "m.observe(document.documentElement,{subtree:true,childList:true,attributes:true,"
    "characterData:true});window.__qdlMut=m;}window.__qdlDirty=false;}catch(e){}"
    "var sx=window.scrollX,sy=window.scrollY,W=%d,H=%d,L=[],I=[];"
    "var a=document.querySelectorAll('a[href]');"
    "for(var i=0;i<a.length&&L.length<%d;i++){var h=a[i].href;"
    "if(typeof h!=='string'||!/^https?:/i.test(h)||h.length>%d)continue;"
    "var r=a[i].getBoundingClientRect();if(r.width<2||r.height<2)continue;"
    "var x=r.left+sx,y=r.top+sy;if(x+r.width<=0||x>=W||y+r.height<=0||y>=H)continue;"
    "var c=getComputedStyle(a[i]);if(c.visibility==='hidden'||c.display==='none')continue;"
    "L.push({x:Math.round(x),y:Math.round(y),w:Math.round(r.width),h:Math.round(r.height),href:h});}"
    "var e=document.querySelectorAll(\"input:not([type=hidden]):not([type=checkbox]):not([type=radio])"
    ":not([type=button]):not([type=submit]):not([type=reset]):not([type=image]):not([type=file])"
    ":not([type=range]):not([type=color]),textarea,[contenteditable=''],[contenteditable=true]\");"
    "for(var j=0;j<e.length&&I.length<%d;j++){var q=e[j].getBoundingClientRect();"
    "if(q.width<2||q.height<2)continue;var u=q.left+sx,v=q.top+sy;"
    "if(u+q.width<=0||u>=W||v+q.height<=0||v>=H)continue;"
    "I.push({x:Math.round(u),y:Math.round(v),w:Math.round(q.width),h:Math.round(q.height)});}"
    "return {links:L,inputs:I};})()")
DIRTY_JS = "window.__qdlDirty===true"
SCROLL_JS = (
    "(function(y){var ih=window.innerHeight;"
    "var m=Math.max(0,Math.max(document.documentElement.scrollHeight,document.body?"
    "document.body.scrollHeight:0)-ih);var t=Math.max(0,Math.min(y-ih/2,m));"
    "window.scrollTo({top:t,left:0,behavior:'instant'});"
    "return [window.scrollX,window.scrollY,window.innerWidth,ih];})(%.1f)")

# ---- v0.1.30: viewport link map (live display) and reader mode ---------------------------
STREAM_PER_TAB = 2             # concurrent MJPEG streams per tab / overall (v0.1.30)
STREAM_TOTAL = 4
STREAM_MAX_SECONDS = 600.0     # a stream ends after this long (the viewer reconnects)
STREAM_HEARTBEAT = 15.0        # no new frame for this long: the last frame is sent again
STREAM_FPS_WINDOW = 3.0
STREAM_BOUNDARY = "qdlframe"
SSE_PING_SECONDS = 10.0        # v0.1.46: `: ping` comment on an idle event stream
SSE_PADDING = (b":" + b" " * 2047 + b"\n\n")      # 2 KiB first comment: small proxy buffers flush at once
SSE_PING = b": ping\n\n"


def sse_event(base64_jpeg):
    """One `frame` event: the frame's base64 JPEG on a single data line (v0.1.46)."""
    if isinstance(base64_jpeg, str):
        base64_jpeg = base64_jpeg.encode("ascii")
    return b"event: frame\ndata: " + base64_jpeg + b"\n\n"
VP_LINK_CAP = 600              # link boxes of the visible viewport (CSS px relative to the viewport)
# Injected once per document: flags that the map may have changed after a scroll ends (200 ms
# debounce), a resize or any DOM mutation. A navigation is a new document, hence dirty again.
# Returns null while nothing changed since the last map (unless `force`).
VP_LINKS_JS = (
    "(function(cap,max,force){var s=window.__qdlVp;"
    "if(!s){s=window.__qdlVp={dirty:true,t:0};"
    "var mark=function(){clearTimeout(s.t);s.t=setTimeout(function(){s.dirty=true},200)};"
    "window.addEventListener('scroll',mark,{passive:true,capture:true});"
    "window.addEventListener('resize',mark,{passive:true});"
    "try{new MutationObserver(function(){s.dirty=true}).observe(document.documentElement,"
    "{subtree:true,childList:true,attributes:true,characterData:true})}catch(e){}}"
    "if(!s.dirty&&!force)return null;s.dirty=false;"
    "var W=window.innerWidth,H=window.innerHeight,L=[],a=document.querySelectorAll('a[href]');"
    "for(var i=0;i<a.length&&L.length<cap;i++){var h=a[i].href;"
    "if(typeof h!=='string'||!/^https?:/i.test(h)||h.length>max)continue;"
    "var rs=a[i].getClientRects(),c=null;"
    "for(var j=0;j<rs.length&&L.length<cap;j++){var r=rs[j];"
    "if(r.width<2||r.height<2||r.right<=0||r.bottom<=0||r.left>=W||r.top>=H)continue;"
    "if(c===null)c=getComputedStyle(a[i]);if(c.visibility==='hidden'||c.display==='none')break;"
    "L.push({x:Math.round(r.left),y:Math.round(r.top),w:Math.round(r.width),"
    "h:Math.round(r.height),href:h});}}"
    "return {scrollX:Math.round(window.scrollX),scrollY:Math.round(window.scrollY),"
    "width:W,height:H,links:L};})(%d,%d,%s)")

READER_BLOCK_CAP = 5000
READER_MAX_JSON = 2 * 1024 * 1024
READER_TEXT_MAX = 20000
READER_RUNS_CAP = 400
READER_IMAGE_CAP = 300
READER_HOSTER_CAP = 100
READER_ALT_MAX = 300
READER_TITLE_MAX = 300
READER_URL_MAX = 2048
READER_TIMEOUT = 20.0
READER_HOSTS = ("rapidgator.net", "www.rapidgator.net", "rg.to", "www.rg.to")
READER_HOSTER_TITLE = "ダウンロードリンク"
IMAGE_MAX_BYTES = 4 * 1024 * 1024
READER_IMAGE_TARGET = 1280      # v0.1.36: the smallest srcset candidate at least this wide is used
IMAGE_CDP_TIMEOUT = 12.0
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Fixed extractor, run in the NAS browser's page (Runtime.evaluate, returnByValue). It only reads the
# DOM and returns plain data; the Hub validates every field again. Image URLs go to the Hub only.
READER_JS = r"""
(function(){
var CAP=__CAP__,TMAX=__TMAX__,BUDGET=__BUDGET__,IMGCAP=__IMGCAP__,RUNCAP=__RUNCAP__,NODES=60000;
var AD=/(^|[-_])(ad|ads|advert|banner|sponsor|promo|popup|cookie)([-_]|$)/i;
var SKIP={SCRIPT:1,STYLE:1,NOSCRIPT:1,TEMPLATE:1,NAV:1,ASIDE:1,SVG:1,
IFRAME:1,OBJECT:1,EMBED:1,CANVAS:1,SELECT:1,BUTTON:1,INPUT:1,TEXTAREA:1,AUDIO:1,VIDEO:1,MAP:1,DIALOG:1};
var INLINE={'inline':1,'inline-block':1,'inline-flex':1,'inline-grid':1,'inline-table':1,
'contents':1,'ruby':1,'table-cell':1};
var blocks=[],images=[],fallbacks=[],imgIndex={},qd=0,size=0,trunc=false,visited=0,depth=0,inA=0,anchorImgs=[];
var hid=false,root=null,pseudoLeft=8000;
var buf=[],ctx={kind:'p',depth:0,level:0};
function norm(s){return s.replace(/[ \t\r\n\f ]+/g,' ');}
function addRun(text,href){
 if(!text)return;
 var last=buf.length?buf[buf.length-1]:null;
 if(last&&!last.href&&!href){last.text+=text;return;}
 buf.push(href?{text:text,href:href}:{text:text});
}
function addText(t){
 if(!t)return;
 var last=buf.length?buf[buf.length-1]:null;
 if(last&&last.text.charAt(last.text.length-1)==='\n'&&t.charAt(0)===' ')t=t.slice(1);
 addRun(t,null);
}
function push(b,len){
 if(blocks.length>=CAP||size>BUDGET){trunc=true;return false;}
 blocks.push(b);size+=len+40;return true;
}
function flush(){
 if(inA)return;
 var runs=buf;buf=[];
 var all='';for(var i=0;i<runs.length;i++)all+=runs[i].text;
 if(!all.replace(/[\s ]+/g,''))return;
 var first=runs[0],last=runs[runs.length-1];
 first.text=first.text.replace(/^[\s ]+/,'');last.text=last.text.replace(/[\s ]+$/,'');
 var out=[],total=0;
 for(var k=0;k<runs.length&&out.length<RUNCAP&&total<TMAX;k++){
  var r=runs[k];if(!r.text)continue;
  if(total+r.text.length>TMAX)r={text:r.text.slice(0,TMAX-total),href:r.href};
  total+=r.text.length;out.push(r.href?{text:r.text,href:r.href}:{text:r.text});
 }
 if(!out.length)return;
 var len=total;for(var m=0;m<out.length;m++)if(out[m].href)len+=out[m].href.length;
 if(ctx.kind==='h'){
  var text='';for(var n=0;n<out.length;n++)text+=out[n].text;
  push({t:'h',level:ctx.level,text:text.replace(/\s+/g,' ').trim()},len);
 }else if(ctx.kind==='li'){push({t:'li',runs:out,depth:ctx.depth},len);}
 else{var pb={t:'p',runs:out};if(qd>0)pb.q=true;push(pb,len);}
}
function adMatch(el){
 var id=el.id;if(typeof id==='string'&&id&&AD.test(id))return true;
 var c=el.getAttribute&&el.getAttribute('class');
 if(c){var tk=c.split(/\s+/);for(var i=0;i<tk.length;i++)if(tk[i]&&AD.test(tk[i]))return true;}
 return false;
}
function adLike(el){
 if(!adMatch(el))return false;
 if(el===root||el.tagName==='H1')return false;
 if((el.textContent||'').length>=1500)return false;
 try{if(el.querySelector('h1'))return false;}catch(e){}
 return true;
}
function pseudo(node,which){
 if(pseudoLeft<=0)return '';pseudoLeft--;
 try{
  var ps=getComputedStyle(node,which),c=ps.content;
  if(!c||c==='none'||c==='normal'||c.charAt(0)!=='"'||ps.display==='none'||ps.visibility==='hidden')return '';
  if(!/^"(?:[^"\\]|\\[\s\S])*"$/.test(c))return '';
  var t=c.slice(1,-1).replace(/\\([0-9a-fA-F]{1,6})[ ]?|\\([\s\S])/g,function(m,h,ch){
   if(ch!==undefined)return ch;
   var n=parseInt(h,16);try{return String.fromCodePoint(n);}catch(e){return '';}
  });
  if(/[\uE000-\uF8FF]/.test(t)||!t.replace(/[\s\u00a0]+/g,''))return '';
  return t.slice(0,200);
 }catch(e){return '';}
}
function absUrl(v){
 try{var u=new URL(v,document.baseURI).href;return /^https?:/i.test(u)&&u.length<=2048?u:null;}catch(e){return null;}
}
/*PICK*/
function parseSrcset(str,baseW){
 var out=[],s=String(str||''),i=0,n=s.length;
 while(i<n){
  while(i<n&&/[\s,]/.test(s.charAt(i)))i++;
  if(i>=n)break;
  var st=i;while(i<n&&!/\s/.test(s.charAt(i)))i++;
  var url=s.slice(st,i),desc='';
  if(/,+$/.test(url)){url=url.replace(/,+$/,'');}
  else{var ds=i;while(i<n&&s.charAt(i)!==',')i++;desc=s.slice(ds,i).replace(/^\s+|\s+$/g,'');}
  if(!url)continue;
  var w=0,m=/^(\d+(?:\.\d+)?)([wx])$/i.exec(desc);
  if(m){w=parseFloat(m[1]);if(m[2].toLowerCase()==='x')w=baseW>0?w*baseW:0;}
  else if(!desc&&baseW>0)w=baseW;
  out.push({u:url,w:Math.round(w)});
 }
 return out;
}
function pickBest(list,target){
 var known=[],all=[],i;
 for(i=0;i<list.length;i++){if(!list[i]||!list[i].u)continue;all.push(list[i]);if(list[i].w>0)known.push(list[i]);}
 if(!all.length)return null;
 if(!known.length)return all[all.length-1];
 var best=null;
 for(i=0;i<known.length;i++){if(known[i].w>=target&&(!best||known[i].w<best.w))best=known[i];}
 if(best)return best;
 for(i=0;i<known.length;i++){if(!best||known[i].w>best.w)best=known[i];}
 return best;
}
/*ENDPICK*/
function lazySrc(el){
 var src=el.currentSrc||el.getAttribute('src')||'';
 if(!src||/^data:/i.test(src)){
  src=el.getAttribute('data-src')||el.getAttribute('data-original')||el.getAttribute('data-lazy-src')||
  el.getAttribute('data-lazy')||(/^data:/i.test(src)?'':src);
 }
 return src;
}
function pictureSets(el,baseW,list){
 var pic=el.parentElement;
 if(!pic||pic.tagName!=='PICTURE')return;
 for(var c=pic.firstElementChild;c;c=c.nextElementSibling){
  if(c.tagName!=='SOURCE')continue;
  var ty=(c.getAttribute('type')||'').toLowerCase();
  if(ty&&!/^image\/(jpeg|png|webp|avif|gif)$/.test(ty))continue;
  var md=c.getAttribute('media');
  if(md){try{if(!window.matchMedia(md).matches)continue;}catch(e){continue;}}
  var set=parseSrcset(c.getAttribute('srcset')||c.getAttribute('data-srcset'),baseW);
  for(var k=0;k<set.length;k++)list.push(set[k]);
 }
}
function image(el,href){
 var r=el.getBoundingClientRect(),rw=Math.round(r.width),rh=Math.round(r.height);
 var nw=el.naturalWidth||0,nh=el.naturalHeight||0;
 var w=nw||rw,h=nh||rh;
 if((rw>0&&rw<24)||(rh>0&&rh<24)||(w===1&&h===1))return;
 var base=rw>0?rw:nw,cands=[];
 pictureSets(el,base,cands);
 var a1=parseSrcset(el.getAttribute('srcset'),base),a2=parseSrcset(el.getAttribute('data-srcset'),base),q;
 for(q=0;q<a1.length;q++)cands.push(a1[q]);
 for(q=0;q<a2.length;q++)cands.push(a2[q]);
 var src0=lazySrc(el),abs0=src0?absUrl(src0):null;
 if(abs0)cands.push({u:src0,w:el.currentSrc&&nw>0?nw:0});
 var pick=pickBest(cands,__TARGET__);
 if(!pick)return;
 var abs=absUrl(pick.u);if(!abs)return;
 var fb=abs0&&abs0!==abs?abs0:null;
 var cw=pick.w>0?pick.w:w;
 var ch=h;
 if(nw>0&&nh>0)ch=Math.round(cw*nh/nw);
 else if(rw>0&&rh>0)ch=Math.round(cw*rh/rw);
 var idx=imgIndex[abs];
 if(idx===undefined){
  if(images.length>=IMGCAP)return;
  idx=images.length;images.push(abs);fallbacks.push(fb);imgIndex[abs]=idx;
 }
 var b={t:'img',i:idx,alt:(el.getAttribute('alt')||'').slice(0,300),w:Math.min(cw,20000),h:Math.min(ch,20000)};
 if(href)b.href=href;
 push(b,60+(href?href.length:0));
}
function walkChildren(node){
 var pb=hid?'':pseudo(node,'::before');
 if(pb)addText(norm(pb));
 var kids=null;
 if(node.tagName&&node.tagName.toUpperCase()==='SLOT'&&node.assignedNodes){
  try{kids=node.assignedNodes({flatten:true});}catch(e){kids=null;}
  if(kids&&!kids.length)kids=null;
 }else if(node.shadowRoot){kids=node.shadowRoot.childNodes;}
 if(kids){for(var k=0;k<kids.length&&!trunc;k++)walk(kids[k]);}
 else for(var c=node.firstChild;c&&!trunc;c=c.nextSibling)walk(c);
 var pa=hid?'':pseudo(node,'::after');
 if(pa)addText(norm(pa));
}
function walk(node){
 if(trunc)return;
 if(node.nodeType===3){if(!hid)addText(norm(node.nodeValue));return;}
 if(node.nodeType!==1)return;
 var sh=hid;
 try{walkEl(node);}finally{hid=sh;}
}
function walkEl(node){
 if(++visited>NODES){trunc=true;return;}
 var tag=node.tagName.toUpperCase();
 if(SKIP[tag]||node.hidden||node.getAttribute('aria-hidden')==='true'||adLike(node))return;
 if((tag==='HEADER'||tag==='FOOTER')&&!node.closest('article,main,[role=main]'))return;
 var cs=getComputedStyle(node);
 if(cs.display==='none')return;
 hid=cs.visibility==='hidden';
 var disp=cs.display;
 if(!INLINE[disp]){
  var r=node.getBoundingClientRect();
  if((r.width<3&&r.height<3)||r.right+window.scrollX<-200||r.bottom+window.scrollY<-200)return;
 }
 if(tag==='BR'){if(!hid)addRun('\n',null);return;}
 if(tag==='HR'){flush();if(!hid)push({t:'hr'},4);return;}
 if(tag==='IMG'){
  if(hid)return;
  if(inA){anchorImgs.push(node);return;}
  flush();image(node,null);return;
 }
 if(tag==='PRE'){
  if(hid){return;}
  flush();var pt=(node.innerText||node.textContent||'').slice(0,TMAX);
  if(pt.replace(/\s+/g,''))push({t:'pre',text:pt},pt.length);return;
 }
 if(tag==='A'){
  var href=node.href;
  if(hid||typeof href!=='string'||!/^https?:/i.test(href)||href.length>2048){walkChildren(node);return;}
  var savedBuf=buf,savedImgs=anchorImgs;buf=[];anchorImgs=[];inA++;
  walkChildren(node);
  inA--;var inner=buf,imgs=anchorImgs;buf=savedBuf;anchorImgs=savedImgs;
  var text='';for(var i=0;i<inner.length;i++)text+=inner[i].text;
  text=norm(text);
  if(text.replace(/[\s ]+/g,''))addRun(text,href);
  else if(text)addText(' ');
  if(imgs.length&&!inA){flush();for(var j=0;j<imgs.length&&!trunc;j++)image(imgs[j],href);}
  else for(var q=0;q<imgs.length;q++)anchorImgs.push(imgs[q]);
  return;
 }
 if(tag==='BLOCKQUOTE'){flush();qd++;try{walkChildren(node);}finally{flush();qd--;}return;}
 var hm=/^H([1-6])$/.exec(tag);
 if(hm){
  flush();var sh=ctx;ctx={kind:'h',level:parseInt(hm[1],10),depth:0};
  walkChildren(node);flush();ctx=sh;return;
 }
 if(tag==='UL'||tag==='OL'){flush();depth++;walkChildren(node);flush();depth--;return;}
 if(tag==='LI'){
  flush();var sl=ctx;ctx={kind:'li',level:0,depth:Math.max(0,Math.min(depth-1,8))};
  walkChildren(node);flush();ctx=sl;return;
 }
 if(INLINE[disp]){
  if(disp==='table-cell'&&node.previousElementSibling)addText(' | ');
  walkChildren(node);return;
 }
 if(inA){addText(' ');walkChildren(node);addText(' ');return;}
 flush();walkChildren(node);flush();
}
var out={title:'',blocks:[],images:[],fallbacks:[],hosters:[],truncated:false};
try{
 var body=document.body||document.documentElement;
 root=document.querySelector('main,[role=main]');
 if(!root){
  var best=null,bl=0,arts=document.querySelectorAll('article');
  for(var a=0;a<arts.length;a++){var l=(arts[a].innerText||'').length;if(l>bl){bl=l;best=arts[a];}}
  root=best;
 }
 var rl=root?(root.innerText||'').length:0;
 var bl2=(body.innerText||'').length;
 if(!root||(rl<200&&bl2>rl*2)||(rl<1500&&bl2>rl*3))root=body;
 walk(root);flush();
}catch(e){trunc=true;}
try{
 var as=document.querySelectorAll('a[href]'),seen={};
 for(var z=0;z<as.length&&out.hosters.length<200;z++){
  var hh=as[z].href;
  if(typeof hh==='string'&&hh.length<=2048&&!seen[hh]&&
   /^https?:\/\/([a-z0-9-]+\.)?(rapidgator\.net|rg\.to)([\/?#:]|$)/i.test(hh)){seen[hh]=1;out.hosters.push(hh);}
 }
}catch(e){}
out.title=String(document.title||'').slice(0,300);
out.blocks=blocks;out.images=images;out.fallbacks=fallbacks;out.truncated=trunc;
return out;
})()
""".replace("__CAP__", str(READER_BLOCK_CAP)).replace(
    "__TMAX__", str(READER_TEXT_MAX)).replace(
    "__BUDGET__", str(READER_MAX_JSON - 256 * 1024)).replace(
    "__IMGCAP__", str(READER_IMAGE_CAP)).replace("__RUNCAP__", str(READER_RUNS_CAP)).replace(
    "__TARGET__", str(READER_IMAGE_TARGET))
IMAGE_FETCH_JS = (
    "(function(u,cap){return fetch(u,{credentials:'include'}).then(function(r){"
    "if(!r.ok)return {err:'status'};var ct=(r.headers.get('content-type')||'').toLowerCase();"
    "if(ct.indexOf('image/')!==0)return {err:'type'};"
    "return r.arrayBuffer().then(function(b){if(b.byteLength>cap)return {err:'size'};"
    "var a=new Uint8Array(b),s='',i=0;"
    "for(;i<a.length;i+=32768)s+=String.fromCharCode.apply(null,a.subarray(i,i+32768));"
    "return {d:btoa(s)};});}).catch(function(){return {err:'fetch'};});})(%s,%d)")


# ---- v0.1.39: mirror display (rb_mirror.py has the serializer and the sanitizer)
MIRROR_WORLD = "qdm"              # name of the isolated world the serializer runs in
MIRROR_TIMEOUT = 25.0
MIRROR_POLL = 0.5                 # DOM-change counter poll while a mirror viewer waits on the meta long-poll
SPARE_EXTRA_KIB = 512 * 1024   # v0.1.54 spare tab: ONE blank tab (the phone's viewport, ad block and hoster
                               # interception already applied) waits in the NAS browser so that opening a page
                               # only has to navigate. It is not a user tab (not counted, not listed). It needs
                               # MemAvailable this much above the tab guard ...
SPARE_MEM_MARGIN = 10.0        # ... container memory this many points below the tab limit ...
SPARE_IDLE = 120.0             # ... and is closed after this long without a tab open or viewer activity
SPARE_WAIT = 8.0               # replenishing waits at most this long for the new page to reach DOMContentLoaded
SPARE_VIEW = (390, 844, 3.0, "speed")      # before the phone has reported its size
MIRROR_FETCH_PARALLEL = 6         # resource fetches in flight per tab (v0.1.54: 4 -> 6)
MIRROR_PREFETCH_IMAGES = 16       # v0.1.54: the Hub fetches this many images (document order) right after a capture ...
MIRROR_PREFETCH_FONTS = 4         # ... and this many web fonts (last), before the phone asks for them
MIRROR_TREE_TTL = 3.0
MIRROR_SHEET_CAP = 3000
MIRROR_CSS_TEXT_MAX = 2 * 1024 * 1024
MIRROR_TOKEN = re.compile(r"[0-9a-f]{16}\Z")
MIRROR_FETCH_JS = (
    "(function(u,cap){return fetch(u,{credentials:'include'}).then(function(r){"
    "if(!r.ok)return {err:'status'};var ct=r.headers.get('content-type')||'';"
    "return r.arrayBuffer().then(function(b){if(b.byteLength>cap)return {err:'size'};"
    "var a=new Uint8Array(b),s='',i=0;"
    "for(;i<a.length;i+=32768)s+=String.fromCharCode.apply(null,a.subarray(i,i+32768));"
    "return {d:btoa(s),ct:ct};});}).catch(function(){return {err:'fetch'};});})(%s,%d)")


def looks_like_challenge(url, title):
    """True while the page is a Cloudflare check (it needs the live screencast to complete)."""
    title = (title or "").lower()
    return (any(t in title for t in CHALLENGE_TITLES) or
            CHALLENGE_URL in (url or "").lower())

def is_challenge_url(url):
    """True for the address of a Cloudflare interstitial history entry (v0.1.52).

    The check page itself lives under /cdn-cgi/challenge-platform/ (or /cdn-cgi/challenge...), and the
    redirect that follows it keeps the original address with a __cf_chl_* query token. A browser back that
    lands on either shows the check again, so history stepping skips them (history_target)."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return (CHALLENGE_URL in parts.path.lower() or "/cdn-cgi/challenge-platform/" in parts.path.lower() or
            "__cf_chl" in parts.query.lower())


def history_target(entries, index, step):
    """(entry id, skipped) a browser back (step -1) / forward (+1) should go to, or None.

    Walks from the current entry in the direction of `step` and takes the first entry that is http(s),
    whose address without the #fragment differs from the current page's, and that is not a Cloudflare
    interstitial (is_challenge_url). `skipped` counts the entries passed over (redirects, checks,
    duplicates), so one gesture is one page."""
    if not isinstance(entries, list) or not isinstance(index, int) or not 0 <= index < len(entries):
        return None
    current = entries[index] if isinstance(entries[index], dict) else {}
    here = current.get("url") if isinstance(current.get("url"), str) else ""
    here = here.split("#", 1)[0]
    skipped, i = 0, index + step
    while 0 <= i < len(entries):
        entry = entries[i] if isinstance(entries[i], dict) else {}
        url = entry.get("url") if isinstance(entry.get("url"), str) else ""
        if (isinstance(entry.get("id"), int) and not isinstance(entry.get("id"), bool) and
                url.lower().startswith(("http://", "https://")) and url.split("#", 1)[0] != here and
                not is_challenge_url(url)):
            return entry["id"], skipped
        skipped += 1
        i += step
    return None


LINKS_JS = (
    "(function(){var o=[],s=new Set(),l=document.querySelectorAll('a[href]');"
    "for(var i=0;i<l.length&&o.length<%d;i++){var h=l[i].href;"
    "if(typeof h==='string'&&h.length<=4096&&!s.has(h)){s.add(h);o.push(h);}}return o;})()"
) % LINK_CAP


class BrowserError(RuntimeError):
    """User-facing (Japanese) reason an operation was refused or failed."""


class SetupBusy(BrowserError):
    pass


class BrowserTimeout(BrowserError):
    """A browser call got no answer in time."""


class TabGone(BrowserError):
    pass


class ProbeQueueFull(BrowserError):
    pass


class SnapshotGone(TabGone):
    """The requested snapshot was replaced or released: take a new one."""


class StreamLimit(ProbeQueueFull):
    """Too many live streams (per tab or overall): HTTP 429."""


class ReaderImageGone(TabGone):
    """Unknown reader image index (the reader was run again): HTTP 404."""


class ImageUnavailable(BrowserError):
    """The image could not be read from the NAS browser: HTTP 502."""


class ImageRefused(BrowserError):
    """Not an allowed image (SVG, not an image, too large): HTTP 415."""


class MirrorGone(BrowserError):
    """A mirror element / resource token / capture that no longer exists (HTTP 410)."""


class WebSocketError(Exception):
    pass


# ------------------------------------------------------------------ RFC 6455
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
_OPCODES = (OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _xor(data, key):
    size = len(data)
    if not size:
        return b""
    stream = (key * (size // 4 + 1))[:size]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(size, "big")


def encode_frame(opcode, payload=b"", *, fin=True, mask=True, key=None):
    """One WebSocket frame. Clients MUST mask (RFC 6455 section 5.3)."""
    if opcode not in _OPCODES:
        raise WebSocketError("unknown opcode")
    if len(payload) > WS_MAX_SEND:
        raise WebSocketError("payload too large")
    if opcode >= OP_CLOSE and (not fin or len(payload) > 125):
        raise WebSocketError("control frame too large or fragmented")
    size = len(payload)
    mask_bit = 0x80 if mask else 0
    head = bytes([(0x80 if fin else 0) | opcode])
    if size < 126:
        head += bytes([mask_bit | size])
    elif size < 65536:
        head += bytes([mask_bit | 126]) + struct.pack(">H", size)
    else:
        head += bytes([mask_bit | 127]) + struct.pack(">Q", size)
    if not mask:
        return head + payload
    key = os.urandom(4) if key is None else key
    if len(key) != 4:
        raise WebSocketError("bad mask key")
    return head + key + _xor(payload, key)


def read_frame(read_exact, max_payload=WS_MAX_MESSAGE, *, allow_masked=False):
    """(fin, opcode, payload). Rejects reserved bits, bad control frames and
    oversize payloads before reading them. Server frames must be unmasked."""
    head = read_exact(2)
    fin = bool(head[0] & 0x80)
    if head[0] & 0x70:
        raise WebSocketError("reserved bits set")
    opcode = head[0] & 0x0F
    if opcode not in _OPCODES:
        raise WebSocketError("unknown opcode")
    masked = bool(head[1] & 0x80)
    size = head[1] & 0x7F
    if size == 126:
        size = struct.unpack(">H", read_exact(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", read_exact(8))[0]
        if size >> 63:
            raise WebSocketError("bad length")
    if opcode >= OP_CLOSE and (not fin or size > 125):
        raise WebSocketError("bad control frame")
    if masked and not allow_masked:
        raise WebSocketError("server frame is masked")
    if size > max_payload:
        raise WebSocketError("frame too large")
    key = read_exact(4) if masked else b""
    payload = read_exact(size) if size else b""
    if masked:
        payload = _xor(payload, key)
    return fin, opcode, payload


class WebSocket:
    """Client over an already connected socket-like object (sendall/makefile/close)."""

    def __init__(self, sock, max_message=WS_MAX_MESSAGE):
        self._sock = sock
        self._fp = sock.makefile("rb", 65536)
        self._max = max_message
        self._send_lock = threading.Lock()
        self.closed = False

    @classmethod
    def connect(cls, host, port, path, timeout=5.0, create=socket.create_connection,
                rng=os.urandom):
        if not WS_PATH.match(path):
            raise WebSocketError("unexpected websocket path")
        sock = create((host, port), timeout)
        try:
            key = base64.b64encode(rng(16)).decode("ascii")
            sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                          "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)).encode("ascii"))
            fp = sock.makefile("rb", 65536)
            status = fp.readline(8192).decode("latin-1")
            if not status.startswith("HTTP/1.1 101"):
                raise WebSocketError("handshake refused")
            headers = {}
            for _ in range(64):
                line = fp.readline(8192).decode("latin-1")
                if line in ("\r\n", "\n", ""):
                    break
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode()
            if headers.get("sec-websocket-accept") != expected:
                raise WebSocketError("bad accept key")
            sock.settimeout(None)
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise
        # The handshake reader may hold buffered bytes: keep using it.
        ws = cls.__new__(cls)
        ws._sock, ws._fp, ws._max = sock, fp, WS_MAX_MESSAGE
        ws._send_lock, ws.closed = threading.Lock(), False
        return ws

    def _read_exact(self, size):
        data = self._fp.read(size)
        if data is None or len(data) != size:
            raise WebSocketError("connection closed")
        return data

    def _send(self, opcode, payload=b""):
        frame = encode_frame(opcode, payload)
        with self._send_lock:
            self._sock.sendall(frame)

    def send_text(self, text):
        self._send(OP_TEXT, text.encode("utf-8"))

    def recv(self):
        """Next complete text message, or None once the peer closed."""
        parts, total, started = [], 0, False
        while True:
            fin, opcode, payload = read_frame(self._read_exact, self._max)
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send(OP_CLOSE, payload[:2])
                except (OSError, WebSocketError):
                    pass
                self.closed = True
                return None
            if opcode == OP_CONT:
                if not started:
                    raise WebSocketError("unexpected continuation")
            else:
                if started:
                    raise WebSocketError("new message inside a fragmented one")
                if opcode == OP_BINARY:
                    raise WebSocketError("binary messages are not used by CDP")
                started = True
            total += len(payload)
            if total > self._max:
                raise WebSocketError("message too large")
            parts.append(payload)
            if fin:
                try:
                    return b"".join(parts).decode("utf-8")
                except UnicodeDecodeError:
                    raise WebSocketError("invalid utf-8") from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._send(OP_CLOSE, struct.pack(">H", 1000))
        except (OSError, WebSocketError):
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def fetch_ws_path(host, port, timeout=3.0):
    """Path of the browser websocket from /json/version.

    Only the path is used: the connection always goes to the inspect-verified
    container address, never to a host named inside the response.
    """
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/json/version", headers={"Host": "%s:%d" % (host, port)})
        response = conn.getresponse()
        if response.status != 200:
            raise WebSocketError("version endpoint refused")
        data = json.loads(response.read(65536).decode("utf-8", "replace"))
    except (OSError, ValueError, http.client.HTTPException):
        raise WebSocketError("version endpoint unavailable") from None
    finally:
        conn.close()
    url = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    path = urlsplit(url).path if isinstance(url, str) else ""
    if not WS_PATH.match(path):
        raise WebSocketError("unexpected websocket url")
    return path


def default_connect(ip):
    return WebSocket.connect(ip, cr.BROWSER_CDP_PORT, fetch_ws_path(ip, cr.BROWSER_CDP_PORT))


# ----------------------------------------------------------------------- CDP
class CDPError(RuntimeError):
    pass


class CDPConnection:
    """Request/response matching and event dispatch over a websocket-like object
    (send_text / recv / close). Events are handled on the reader thread, which
    therefore must never wait for a response."""

    def __init__(self, ws, on_event, on_close=None):
        self._ws, self._on_event, self._on_close = ws, on_event, on_close
        self._lock = threading.Lock()
        self._next = 1
        self._pending = {}
        self.closed = False
        self._thread = threading.Thread(target=self._reader, daemon=True)

    def start(self):
        self._thread.start()

    def _send(self, method, params, session):
        with self._lock:
            if self.closed:
                raise CDPError("closed")
            ident = self._next
            self._next += 1
        message = {"id": ident, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        return ident, json.dumps(message, separators=(",", ":"))

    def send_nowait(self, method, params=None, session=None):
        ident, text = self._send(method, params, session)
        try:
            self._ws.send_text(text)
        except (OSError, WebSocketError):
            raise CDPError("send failed") from None

    def call(self, method, params=None, session=None, timeout=CDP_TIMEOUT):
        ident, text = self._send(method, params, session)
        slot = [threading.Event(), None]
        with self._lock:
            self._pending[ident] = slot
        try:
            self._ws.send_text(text)
        except (OSError, WebSocketError):
            with self._lock:
                self._pending.pop(ident, None)
            raise CDPError("send failed") from None
        if not slot[0].wait(timeout):
            with self._lock:
                self._pending.pop(ident, None)
            raise CDPError("timeout")
        message = slot[1]
        if message is None:
            raise CDPError("closed")
        if "error" in message:
            raise CDPError(str((message["error"] or {}).get("message", "error"))[:200])
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def call_many(self, commands, session=None, timeout=CDP_TIMEOUT):
        """Sends all `commands` ((method, params) pairs, for one session) back to back and then waits for every
        answer: one round trip instead of one per command (v0.1.54). Chromium runs the commands of a session in
        order. Returns [(result dict | None, error text | None)] in command order."""
        slots = []
        for method, params in commands:
            ident, text = self._send(method, params, session)
            slot = [threading.Event(), None]
            with self._lock:
                self._pending[ident] = slot
            slots.append((ident, slot))
            try:
                self._ws.send_text(text)
            except (OSError, WebSocketError):
                with self._lock:
                    for done, _ in slots:
                        self._pending.pop(done, None)
                raise CDPError("send failed") from None
        deadline = time.monotonic() + timeout
        out = []
        for ident, slot in slots:
            if not slot[0].wait(max(0.0, deadline - time.monotonic())):
                with self._lock:
                    self._pending.pop(ident, None)
                out.append((None, "timeout"))
                continue
            message = slot[1]
            if message is None:
                out.append((None, "closed"))
            elif "error" in message:
                out.append((None, str((message["error"] or {}).get("message", "error"))[:200]))
            else:
                result = message.get("result")
                out.append((result if isinstance(result, dict) else {}, None))
        return out

    def _reader(self):
        try:
            while True:
                text = self._ws.recv()
                if text is None:
                    break
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                ident = message.get("id")
                if isinstance(ident, int):
                    with self._lock:
                        slot = self._pending.pop(ident, None)
                    if slot is not None:
                        slot[1] = message
                        slot[0].set()
                elif isinstance(message.get("method"), str):
                    try:
                        self._on_event(message)
                    except Exception:
                        pass     # a bad event must not kill the reader
        except (OSError, WebSocketError):
            pass
        finally:
            with self._lock:
                self.closed = True
                pending, self._pending = list(self._pending.values()), {}
            for slot in pending:
                slot[0].set()
            if self._on_close:
                self._on_close()

    def close(self):
        with self._lock:
            self.closed = True
        try:
            self._ws.close()
        except Exception:
            pass


# ------------------------------------------------------------------- manager
class Tab:
    def __init__(self, ident, now):
        self.id = ident
        self.target_id = None
        self.session = None
        self.url = ""
        self.title = ""
        self.loading = set()
        self.page_challenge = False   # last page-side challenge check (CHALLENGE_JS)
        self.challenge_at = None      # clock of that check; None = stale (navigation/load)
        self.frame = None          # latest JPEG as a base64 string (RAM only)
        self.seq = 0
        self.size = DEFAULT_SIZE
        self.screencast = False
        self.viewport = None       # (css width, css height, capped scale) once the viewer reported it
        self.touch = False
        self.last_poll = now
        self.pollers = 0
        self.blocked = None
        self.rev = 0               # bumped on every change of url/title/loading/blocked/intercepts
        self.intercepts = deque(maxlen=INTERCEPT_CAP)
        self.intercept_seq = 0     # id of the newest intercept event (monotonic per tab)
        self.bypass = {}           # exact URL -> expiry; lets one navigation through the Fetch pattern
        self.page_host = ""        # host of the last https page shown (site rules key)
        self.ads = False           # ad blocking currently applied to this tab
        self.popups_blocked = 0    # popups closed silently by a "block" rule
        self.quality = DEFAULT_QUALITY
        self.probing = 0           # probes running now (<= MAX_PROBES_PER_TAB)
        self.probe_waiting = 0     # probes queued behind the limits
        self.editable = False      # the page's focused element accepts text (after the last input)
        self.snaps = {}            # snapshot id -> Snapshot (newest last, <= SNAP_KEEP)
        self.snap_seq = 0
        self.snap_gen = 0          # bumped by anything that may change the page (input, navigation)
        self.snap_lock = threading.Lock()    # one capture at a time per tab
        self.tiles = {}            # (snapshot id, index) -> JPEG bytes, in LRU order (oldest first)
        self.tile_bytes = 0
        self.streams = 0           # MJPEG / SSE streams being written now
        self.dcl_n = 0             # v0.1.46: DOMContentLoaded events seen (self-test tabs only)
        self.keep_until = 0.0      # v0.1.46: the screencast is not idle-stopped before this (self-test)
        self.iso_ctx = None        # v0.1.46: isolated-world execution context of the self-test scroller
        self.stream_stops = []     # v0.1.46: stop events of the live streams (newest last; the oldest is evicted)
        self.stream_times = deque(maxlen=128)    # monotonic send times (fps for the viewer's overlay)
        self.stream_bytes = 0      # size of the last part sent
        self.dbg_frames = 0        # debug log only: screencast frames in the current 10 s window
        self.dbg_t = 0.0
        self.vp_rev = 0            # bumped by every screencast frame (the live picture changed)
        self.vp_cache = None       # {"token": vp_rev + rev, "map": viewport link map}
        self.vp_lock = threading.Lock()
        self.reader_images = []    # reader image index -> absolute http(s) URL (None = refused); Hub only
        self.reader_fallbacks = [] # v0.1.36: index -> smaller alternative URL (or None); Hub only
        # v0.1.39 mirror display (all RAM only, cleared on navigation / release / close)
        self.mirror_on = False     # a mirror viewer is attached: DOM changes wake the meta long-poll
        self.mirror_rev = 0        # bumped by navigation, load and (polled) DOM changes
        self.mirror_seen = -1      # last DOM change counter read from the page
        self.mirror_ctx = None     # execution context id of the isolated world
        self.mirror_frame = None   # main frame id
        self.mirror_tokens = rb_mirror.TokenMap()
        self.mirror_cache = None   # newest capture (dict) with its rev
        self.mirror_doc = None     # (rev, document text) built on demand
        self.mirror_sheets = {}    # stylesheet URL -> CSS agent id
        self.mirror_css = False    # DOM / CSS agents enabled for this tab
        self.mirror_lock = threading.Lock()      # one capture at a time
        self.mirror_flight = {}    # token -> Event while a resource is being fetched
        self.mirror_sheet_cache = {}   # v0.1.54: rewritten CSS by text hash (one page)
        self.mirror_css_text = {}  # v0.1.54: url -> text of a cross-origin stylesheet (one page)
        self.mirror_prefetch = 0   # v0.1.54: capture rev whose resources were queued for prefetch
        self.mirror_partials = 0   # v0.1.54: partial ("above the fold first") captures made for this tab
        self.mirror_sem = threading.BoundedSemaphore(MIRROR_FETCH_PARALLEL)
        self.mirror_tree = None    # (time, {url: (frame id, type, mime)})
        self.client_dpr = 2.0      # the viewer's device pixel ratio (image choice)
        self.timeline = None       # v0.1.54: rb_firstload.Timeline of the page open (RAM only)
        self.dcl = False           # v0.1.54: DOMContentLoaded fired for the current document
        self.nav_same_doc = False  # v0.1.59: the last navigate() only changed the #fragment (no new document, no DCL)
        self.selftest = False      # v0.1.44: opened by the self-test (not a user tab; closed at its end)
        self.fixture = None        # self-test fixture tab: callable(url) -> (status, content type, bytes) | None


class Snapshot:
    """Measured page: tiles are rendered lazily and cached on the Tab (RAM only)."""

    def __init__(self, ident, width, height, scale, quality, rev, gen, now):
        self.id = ident
        self.width = width
        self.height = height
        self.scale = scale
        self.quality = quality
        self.rev = rev
        self.gen = gen
        self.created = now
        self.tiles = (height + SNAP_TILE - 1) // SNAP_TILE
        self.meta = None


def tile_clip(width, height, index, scale=SNAP_CLIP_SCALE):
    """CDP clip of tile `index`: full width, SNAP_TILE high (the last tile is the remainder)."""
    top = index * SNAP_TILE
    return {"x": 0, "y": top, "width": width, "height": min(SNAP_TILE, height - top),
            "scale": scale}


class Probe:
    """One hidden background target following a long-pressed link (RAM only)."""

    def __init__(self, tab_id, now):
        self.tab_id = tab_id
        self.target = None
        self.session = None
        self.links = []            # hoster URLs, normalized, deduplicated, in order found
        self.redirects = 0
        self.last_doc = None
        self.loaded = False
        self.started = False
        self.loading = set()
        self.popups = set()        # popup target ids opened by the probe page
        self.activity = now


def ttl_or(ttl):
    return STATS_TTL if ttl is None else ttl


def _scheme_ok(url):
    return (not url or url.startswith("https://") or url == "about:blank" or
            url.startswith("chrome-error://"))


class RemoteBrowser:
    def __init__(self, runtime, context_dir, *, meminfo_path="/proc/meminfo", connect=None,
                 clock=time.monotonic, sleep=time.sleep, monitor=True,
                 frame_interval=MIN_FRAME_INTERVAL, start_timeout=START_TIMEOUT,
                 rules=None, auto_send=None, spawn=None, wall=time.time,
                 cgroup_root=diskio.DEFAULT_CGROUP_ROOT, proc_root="/proc",
                 hub_status_path="/proc/self/status", spare=None):
        self.runtime = runtime
        self.context_dir = str(context_dir)
        self._meminfo = meminfo_path
        self._connect = connect if connect is not None else default_connect
        self._clock = clock
        self._sleep = sleep
        self._monitor_on = monitor
        self._frame_interval = frame_interval
        self._start_timeout = start_timeout
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._open_lock = threading.Lock()
        self._state = "stopped"
        self._conn = None
        self._tabs = {}
        self._by_session = {}
        self._popups = {}          # popup target id -> owning Tab, until its first URL is known
        self._anchor = None
        self._next_id = 1
        self._last_activity = clock()
        self._no_tabs_since = clock()
        self._stop_evt = threading.Event()
        self._stats_cache = None
        self._stats_busy = False
        self._tag_cache = False
        self._image_cache = None
        self._cs_cache = None
        self._setup_active = False
        self._steps = self._fresh_steps()
        self._error = None
        self._stale = False
        self._ring = deque(maxlen=LOG_LINES)
        self._ring_lock = threading.Lock()
        # v0.1.24: `rules` provides settings() and site_rule_for(host) (storage.Store);
        # `auto_send(url)` sends a hoster link to JD2 and returns "sent", "dup" or None
        # (not possible now); `spawn(fn)` runs blocking work off the CDP reader thread.
        self.rules = rules
        self.auto_send = auto_send
        self._spawn = spawn if spawn is not None else self._spawn_thread
        self._auto_inflight = set()
        self._streams = 0
        self._mem_none_until = 0.0
        self._probes = 0
        self._probe_queue = []     # FIFO of waiting probe tickets (dicts with the tab)
        self._probe_targets = {}   # probe target id -> Probe
        self._probe_sessions = {}  # CDP session id -> Probe
        self._probe_popups = {}    # popup target id -> Probe
        # v0.1.38: resident mode, watchdog and memory figures (RAM only)
        self._wall = wall
        self._cgroup_root = cgroup_root
        self._proc_root = proc_root
        self._hub_status = hub_status_path
        self._resident = False
        self._user_stopped = False   # stopped on purpose: nothing restarts it
        self.wd = rb_resident.Watchdog(clock, wall)
        self.idle = rb_resident.IdleBaseline(wall)
        self._tick_lock = threading.Lock()
        self._monitor_thread = None
        self._monitor_evt = threading.Event()
        self._health_at = clock()
        self._trim_at = None         # when the last memory trim ran (resident, no tabs)
        self._idle_sample_at = None
        self._mem_cache = None
        self._mirror_res = rb_mirror.ResourceCache()
        # v0.1.54: the spare tab (see SPARE_*). `spare` True / False forces it; None follows the option rb_spare_tab.
        self._spare_opt = spare
        self._spare = None         # {"target", "session", "view", "ads", "at"} of the waiting blank tab
        self._spare_lock = threading.Lock()
        self._spare_pending = False
        self._first_waiters = threading.BoundedSemaphore(FIRST_WAITERS_MAX)   # v0.1.59
        self._last_view = None     # (width, height, dpr, quality mode) of the newest viewport the phone set

    @staticmethod
    def _spawn_thread(fn):
        threading.Thread(target=fn, daemon=True).start()

    # ------------------------------------------------------------- RAM log
    def log(self, text):
        with self._ring_lock:
            for line in str(text).splitlines() or [""]:
                for start in range(0, max(len(line), 1), 500):
                    self._ring.append(line[start:start + 500])

    def log_lines(self):
        with self._ring_lock:
            return list(self._ring)

    # ------------------------------------------------------------- basics
    @staticmethod
    def _fresh_steps():
        return [{"id": i, "label": l, "status": "pending", "detail": "", "progress": None}
                for i, l in STEP_DEFS]

    def _touch(self):
        with self._lock:
            self._last_activity = self._clock()

    def _tag(self):
        if self._tag_cache is False:
            try:
                self._tag_cache = self.runtime.browser_tag(self.context_dir)
            except cr.ContainerError:
                self._tag_cache = None
        return self._tag_cache

    def container_station(self, refresh=False):
        now = self._clock()
        with self._lock:
            cached = self._cs_cache
        if cached is not None and not refresh and now - cached[0] < CS_CACHE_TTL:
            return cached[1]
        version = self.runtime.version()
        with self._lock:
            self._cs_cache = (now, version)
        return version

    def image_ready(self, refresh=False):
        tag = self._tag()
        if tag is None:
            return False
        now = self._clock()
        with self._lock:
            cached = self._image_cache
        if cached is not None and not refresh and now - cached[0] < SETUP_CACHE_TTL:
            return cached[1]
        stale = False
        try:
            ready = self.runtime.browser_image_present(tag)
            if not ready:
                # An older Hub-built image exists: the shipped files changed, so
                # the tag no longer matches and setup must be re-run.
                stale = bool(self.runtime.browser_image_tags())
        except cr.ContainerError:
            ready = False
        with self._lock:
            self._image_cache = (now, ready)
            self._stale = stale and not ready
        return ready

    def _stats(self, ttl=None):
        """`docker stats` of the browser container, cached `ttl` seconds (default STATS_TTL)."""
        ttl = STATS_TTL if ttl is None else ttl
        now = self._clock()
        with self._lock:
            cached = self._stats_cache
        if cached is not None and now - cached[0] < ttl:
            if cached[2] is not None:
                raise cached[2]
            return cached[1]
        try:
            value, error = self.runtime.stats(cr.BROWSER_NAME), None
        except cr.ContainerError:
            value, error = None, BrowserError(
                "ブラウザコンテナのメモリ使用量を確認できないため開きません")
        with self._lock:
            self._stats_cache = (now, value, error)
        if error is not None:
            raise error
        return value

    def _cached_stats(self, max_age=STATS_STALE_MAX):
        """The last successful `docker stats` result if it is at most `max_age` old, else None.

        Never starts `docker stats` (v0.1.46): the /api/state path must stay cheap on a small CPU.
        """
        now = self._clock()
        with self._lock:
            cached = self._stats_cache
        if cached is not None and cached[2] is None and cached[1] is not None and now - cached[0] <= max_age:
            return cached[1]
        return None

    @staticmethod
    def _browser_limit_bytes():
        """The browser container's memory limit in bytes (docker run --memory), or None."""
        m = re.match(r"([0-9]+)([kmg]?)b?\Z", str(cr.BROWSER_MEMORY).strip().lower())
        if not m:
            return None
        return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]

    def _stats_fast(self, ttl):
        """Memory of the browser container as {"percent"} for the tab-open guard, without making the open wait
        for `docker stats` (it samples for a second or two on the NAS; that was most of the 4-5 s of an open):
        no user tab open -> nothing to compare (None; the host's MemAvailable still applies); the cgroup (a
        file read) when it can be found; a `docker stats` result up to STATS_STALE_MAX old (refreshed in the
        background); only when none of these exist does the open wait for one."""
        with self._lock:
            tabs = sum(1 for t in self._tabs.values() if not t.selftest)
        if tabs == 0:
            return None
        try:
            memory = self.container_memory(allow_stats=False)
        except Exception:
            memory = None
        limit = self._browser_limit_bytes()
        if memory is not None and str(memory.get("source", "")).startswith("cgroup") and limit:
            return {"percent": memory["used_bytes"] * 100.0 / limit, "used_bytes": memory["used_bytes"]}
        with self._lock:
            cached = self._stats_cache
        if cached is not None and cached[2] is None and cached[1] is not None:
            age = self._clock() - cached[0]
            # a stale reading is trusted only while it is far from the limit; near it a fresh one decides
            if age <= STATS_STALE_MAX and cached[1]["percent"] < MEM_LIMIT_PERCENT - SPARE_MEM_MARGIN:
                if age > ttl_or(ttl) and not self._stats_busy:
                    self._stats_busy = True
                    self._spawn(self._stats_refresh)
                return cached[1]
        return self._stats(ttl)

    def _stats_refresh(self):
        try:
            self._stats(STATS_TTL)
        except BrowserError:
            pass
        finally:
            self._stats_busy = False

    def _guard_reason(self):
        """The tab-open memory guard (see memory_reason) reading the cgroup instead of `docker stats`."""
        return self.memory_reason(fast=True)

    def memory_reason(self, min_kib=None, limit_percent=None, stats_ttl=None, cached_only=False, fast=False):
        """Japanese reason a new tab (or probe) must not be started now, or None.

        The tab-open guard uses a fresh `docker stats` (STATS_TTL). status() passes `cached_only` (never starts
        docker stats; a missing result skips only that one check) or a longer `stats_ttl`.
        """
        min_kib = MIN_AVAILABLE_KIB if min_kib is None else min_kib
        limit_percent = MEM_LIMIT_PERCENT if limit_percent is None else limit_percent
        available = cr.read_mem_available_kib(self._meminfo)
        if available is None:
            return "空きメモリ(MemAvailable)を確認できないため開きません"
        if available < min_kib:
            return ("空きメモリが不足しています（MemAvailable %d MiB < 必要 %d MiB）。"
                    "他のアプリを止めずに、開くのを見送りました" % (
                        available // 1024, min_kib // 1024))
        with self._lock:
            running = self._state == "running"
        if running:
            if cached_only:
                stats = self._cached_stats()
            else:
                try:
                    stats = self._stats_fast(stats_ttl) if fast else self._stats(stats_ttl)
                except BrowserError as error:
                    return str(error)
            if stats is not None and stats["percent"] >= limit_percent:
                return ("NASブラウザのメモリ使用量が上限の%d%%に達しています（%.0f%%）。"
                        "タブを閉じてから開いてください" % (limit_percent, stats["percent"]))
        return None

    def probe_memory_reason(self):
        return self.memory_reason(PROBE_MIN_AVAILABLE_KIB, PROBE_MEM_LIMIT_PERCENT)

    # ------------------------------------------------------------- status
    def status(self, fresh_memory=False):
        """State of the NAS browser. `fresh_memory=False` (the /api/state path) never starts `docker stats`:
        the memory figure comes from the cgroup or from a cached result at most STATS_STALE_MAX old.
        GET /api/rb/status passes True (settings view) and may run `docker stats` (cached STATUS_STATS_TTL)."""
        station = self.container_station()
        ready = bool(station) and self.image_ready()
        with self._lock:
            stale = self._stale
            state = self._state
            setup = self._setup_active
            tabs = [{"id": t.id, "url": t.url, "title": t.title} for t in self._tabs.values()
                    if not t.selftest]
            steps = [dict(s) for s in (self._steps if (setup or self._error or
                     any(s["status"] != "pending" for s in self._steps)) else self._fresh_steps())]
            error = self._error
        memory = None
        if state == "running":
            stats = None
            if fresh_memory:
                try:
                    stats = self._stats(STATUS_STATS_TTL)
                except BrowserError:
                    stats = None
            else:
                stats = self._cached_stats()
            if stats is not None:
                memory = {"used_mib": stats["used_bytes"] // (1024 * 1024),
                          "limit_mib": stats["limit_bytes"] // (1024 * 1024),
                          "percent": stats["percent"]}
            else:
                used = self.container_memory(allow_stats=False)
                if used is not None:
                    memory = {"used_mib": used["used_bytes"] // (1024 * 1024), "limit_mib": None, "percent": None}
        available = cr.read_mem_available_kib(self._meminfo)
        reason = None
        if ready and state in ("stopped", "running"):
            reason = (self.memory_reason(stats_ttl=STATUS_STATS_TTL) if fresh_memory
                      else self.memory_reason(cached_only=True))
            if reason is None and len(tabs) >= MAX_TABS:
                reason = "NASブラウザのタブは最大%d枚です" % MAX_TABS
        if bool(station) and not ready and stale and not setup:
            reason = UPDATE_NEEDED
        resident = self.resident_status(allow_stats=fresh_memory)
        if resident["watchdog"]["state"] == rb_resident.STATE_GAVE_UP and state == "stopped":
            reason = resident["watchdog"]["message"]
        return {
            "container_station": {"available": bool(station), "version": station},
            "image_ready": ready,
            "update_needed": bool(station) and not ready and stale,
            "phase": "setup" if setup else state,
            "setup_running": setup,
            "steps": steps,
            "error": error,
            "tabs": tabs,
            "max_tabs": MAX_TABS,
            "memory": memory,
            "available_mib": None if available is None else available // 1024,
            "can_open": bool(ready and reason is None),
            "reason": reason,
            "resident": resident,
        }

    # -------------------------------------------------------------- setup
    def _step(self, sid, status, detail=None):
        with self._lock:
            for item in self._steps:
                if item["id"] == sid:
                    item["status"] = status
                    if detail is not None:
                        item["detail"] = detail

    def start_setup(self):
        self._begin_setup()
        threading.Thread(target=self._setup_thread, daemon=True).start()

    def run_setup(self):
        self._begin_setup()
        self._setup_thread()

    def _begin_setup(self):
        with self._lock:
            if self._setup_active:
                raise SetupBusy("初期設定はすでに実行中です")
            if self._state not in ("stopped",):
                raise BrowserError("NASブラウザを停止してから実行してください")
            self._setup_active = True
            self._error = None
            self._steps = self._fresh_steps()

    def _setup_thread(self):
        try:
            self._execute_setup()
        finally:
            with self._lock:
                self._setup_active = False
                self._user_stopped = False     # a rebuild stop is over: resident may start again
            self.wd.unblock()

    def _execute_setup(self):
        current = "precheck"
        try:
            self._step("precheck", "running")
            version = self.container_station(refresh=True)
            if not version:
                raise BrowserError("Container Stationが見つかりません。"
                                   "App CenterでContainer Stationをインストールしてください")
            tag = self.runtime.browser_tag(self.context_dir)
            self._step("precheck", "done", "Container Station (Docker %s) を検出しました" % version)
            current = "network"
            self._step("network", "running")
            created = self.runtime.network_ensure(cr.BROWSER_NET)
            self._step("network", "done" if created else "skipped",
                       "%s を作成しました（ポート非公開）" % cr.BROWSER_NET if created else "作成済みです")
            current = "build"
            self._step("build", "running", "ビルド中…（同梱のDockerfileのみ・Debian署名付きapt）")
            if self.runtime.browser_image_present(tag):
                self._step("build", "skipped", "作成済みのイメージを使います")
            else:
                self.runtime.browser_build(self.context_dir)
                self._step("build", "done", "イメージ %s を作成しました" % tag)
            self.runtime.browser_prune_old(tag)
            current = "sandbox"
            self._step("sandbox", "running")
            self.runtime.browser_selftest(tag)
            self._step("sandbox", "done", "ユーザー名前空間を確認しました（サンドボックスは有効のまま）")
            current = "done"
            with self._lock:
                self._image_cache = None
            self._step("done", "done", "初期設定が完了しました")
        except (BrowserError, cr.ContainerError) as error:
            self._step(current, "failed", str(error))
            with self._lock:
                self._error = str(error)
        except Exception as error:
            reason = "予期しないエラー: %s" % type(error).__name__
            self._step(current, "failed", reason)
            with self._lock:
                self._error = reason

    # ---------------------------------------------------------- CDP access
    def _call(self, method, params=None, session=None, timeout=CDP_TIMEOUT):
        conn = self._conn
        if conn is None or conn.closed:
            raise BrowserError("NASブラウザが起動していません")
        try:
            return conn.call(method, params, session, timeout)
        except CDPError as error:
            if str(error) == "timeout":
                raise BrowserTimeout("ブラウザの操作に失敗しました（%s: %s）" % (method, error)) from None
            raise BrowserError("ブラウザの操作に失敗しました（%s: %s）" % (method, error)) from None

    def _call_many(self, commands, session=None, timeout=CDP_TIMEOUT):
        """Pipelined commands of one session (see CDPConnection.call_many): [(result | None, error | None)]."""
        conn = self._conn
        if conn is None or conn.closed:
            raise BrowserError("NASブラウザが起動していません")
        try:
            return conn.call_many(commands, session, timeout)
        except CDPError as error:
            raise BrowserError("ブラウザの操作に失敗しました（%s）" % error) from None

    # ------------------------------------------------------------ lifecycle
    def _ensure_clean_container(self):
        info = self.runtime.container_state(cr.BROWSER_NAME)
        if not info["exists"]:
            return
        if not info["managed"]:
            raise BrowserError("同名のコンテナがHub管理外のため使用しません")
        self.runtime.stop(cr.BROWSER_NAME, grace=cr.BROWSER_STOP_GRACE)
        self.runtime.remove(cr.BROWSER_NAME)

    def _early_exit_message(self, code):
        """Message for a container that exited early; reads its logs before removal."""
        base = cr.BROWSER_EXIT_MESSAGES.get(code)
        base = ("%s（終了コード %s）" % (base, code) if base else
                "ブラウザコンテナが起動直後に終了しました（終了コード %s）" % code)
        try:
            tail = self.runtime.logs_tail(cr.BROWSER_NAME)
        except cr.ContainerError:
            tail = ""
        self.log(base)
        if not tail:
            return base
        self.log(tail)
        flat = " ".join(tail.split())[-TOAST_TAIL_CHARS:]
        return "%s: %s" % (base, flat)

    def _wait_connect(self):
        deadline = self._clock() + self._start_timeout
        while True:
            info = self.runtime.container_state(cr.BROWSER_NAME)
            if not info["running"]:
                code = info.get("exit_code")
                if code == cr.BROWSER_EXIT_NO_CHROOT:
                    raise BrowserError(cr.NO_CHROOT_MESSAGE)
                if code == cr.BROWSER_EXIT_NO_USERNS:
                    raise BrowserError(cr.NO_USERNS_MESSAGE)
                if code == cr.BROWSER_EXIT_CAPS:
                    raise BrowserError(cr.CAPS_MESSAGE)
                raise BrowserError(self._early_exit_message(code))
            try:
                ip = self.runtime.inspect_ip(cr.BROWSER_NAME, cr.BROWSER_NET)
                return self._connect(ip)
            except (cr.ContainerError, OSError, WebSocketError, ValueError):
                pass
            if self._clock() >= deadline:
                raise BrowserError("ブラウザコンテナに接続できませんでした（時間切れ）")
            self._sleep(1.0)

    def _start(self):
        with self._lock:
            self._state = "starting"
            self._stop_evt = threading.Event()
        try:
            tag = self._tag()
            if tag is None:
                raise BrowserError("ブラウザイメージのビルド用ファイルを確認できません")
            self._ensure_clean_container()
            self.runtime.network_ensure(cr.BROWSER_NET)
            self.runtime.run_browser(tag)
            ws = self._wait_connect()
            conn = CDPConnection(ws, self._on_event, self._on_close)
            conn.start()
            self._conn = conn
            self._call("Target.setDiscoverTargets", {"discover": True})
            # Downloads are never allowed; failing to set this aborts the start.
            # (eventsEnabled: Browser.downloadWillBegin is reported as an intercept.)
            self._call("Browser.setDownloadBehavior", {"behavior": "deny", "eventsEnabled": True})
            pages = [t for t in self._call("Target.getTargets").get("targetInfos", [])
                     if isinstance(t, dict) and t.get("type") == "page"]
            anchor = pages[0].get("targetId") if pages else None
            if not anchor:
                anchor = self._call("Target.createTarget", {"url": "about:blank",
                                                             "newWindow": True}).get("targetId")
            if not isinstance(anchor, str) or not TARGET_ID.match(anchor):
                raise BrowserError("ブラウザの初期ページを確認できません")
            with self._lock:
                self._anchor = anchor    # keeps the browser alive when the last tab closes
                self._state = "running"
                self._last_activity = self._no_tabs_since = self._clock()
                self._health_at = self._clock()
                self._trim_at = None
                self._idle_sample_at = None
                self._user_stopped = False
            self._ensure_monitor()
        except Exception as error:
            self._teardown()
            if isinstance(error, (BrowserError,)):
                raise
            if isinstance(error, cr.ContainerError):
                raise BrowserError(str(error)) from None
            raise BrowserError("ブラウザを起動できません: %s" % type(error).__name__) from None

    def _teardown(self):
        conn, self._conn = self._conn, None
        self._stop_evt.set()
        if conn is not None:
            conn.close()
        for action in (lambda: self.runtime.stop(cr.BROWSER_NAME, grace=cr.BROWSER_STOP_GRACE),
                       lambda: self.runtime.remove(cr.BROWSER_NAME)):
            try:
                action()
            except cr.ContainerError:
                pass
        with self._cv:
            self._tabs.clear()
            self._by_session.clear()
            self._popups.clear()
            self._probe_targets.clear()
            self._probe_sessions.clear()
            self._probe_popups.clear()
            self._spare = None
            self._spare_pending = False
            self._mirror_res.clear()
            self._anchor = None
            self._state = "stopped"
            self._stats_cache = None
            self._cv.notify_all()

    def stop(self):
        """Close every target, then stop and remove the container (memory freed)."""
        with self._lock:
            if self._state == "stopping":
                return False
            was = self._state
            self._state = "stopping"
            self._user_stopped = True      # on purpose: the watchdog / resident start must not undo it
            self.wd.cancel()
            targets = [t.target_id for t in self._tabs.values() if t.target_id]
            if self._spare is not None:
                targets.append(self._spare["target"])
            self._cv.notify_all()
        if was == "running":
            for target in targets:
                try:
                    self._call("Target.closeTarget", {"targetId": target}, timeout=3.0)
                except BrowserError:
                    pass
        self._teardown()
        return True

    def shutdown(self):
        self._monitor_evt.set()
        try:
            self.stop()
        except Exception:
            pass

    def startup_cleanup(self):
        """After a Hub crash a labelled browser container may still hold memory."""
        try:
            if not self.runtime.available():
                return False
            info = self.runtime.container_state(cr.BROWSER_NAME)
            if info["exists"] and info["managed"]:
                self._teardown()
                return True
        except cr.ContainerError:
            pass
        return False

    def _on_close(self):
        with self._cv:
            self._cv.notify_all()

    def _ensure_monitor(self):
        """One housekeeping thread for the life of the Hub (started with the first container)."""
        if not self._monitor_on:
            return
        with self._lock:
            thread = self._monitor_thread
            if thread is not None and thread.is_alive():
                return
            self._monitor_evt.clear()
            thread = self._monitor_thread = threading.Thread(
                target=self._monitor, args=(self._monitor_evt,), daemon=True)
        thread.start()

    def _monitor(self, stop_evt):
        while not stop_evt.wait(MONITOR_INTERVAL):
            try:
                self.tick()
            except Exception:
                pass

    def tick(self):
        """Housekeeping (every few seconds): idle stop, health check, watchdog, resident start."""
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._tick()
        finally:
            self._tick_lock.release()

    def _tick(self):
        now = self._clock()
        with self._lock:
            state = self._state
            setup = self._setup_active
        if setup or state in ("starting", "stopping"):
            return
        if state != "running":
            self._tick_stopped()
            return
        with self._lock:
            conn = self._conn
            resident = self._resident
            idle_tabs = (not self._tabs and now - self._no_tabs_since >= NO_TAB_IDLE
                         and not resident)
            idle_view = now - self._last_activity >= NO_VIEWER_IDLE
            lost = conn is None or conn.closed
            expired = [t for t in self._tabs.values()
                       if t.screencast and t.pollers == 0 and now - t.last_poll > VIEWER_STOP_AFTER
                       and now >= t.keep_until]
            health_due = now - self._health_at >= HEALTH_INTERVAL
        if lost:
            self._unexpected_exit({"lost": True})
            return
        if health_due:
            with self._lock:
                self._health_at = now
            try:
                info = self.runtime.container_state(cr.BROWSER_NAME)
            except cr.ContainerError:
                info = None
            if info is not None and not info.get("running"):
                self._unexpected_exit(info)
                return
        if idle_tabs:
            self.stop()
            return
        if idle_view:
            if not resident:
                self.stop()
                return
            # Resident: the container stays; open tabs are closed to give the memory back.
            self._release_tabs()
            with self._lock:
                self._last_activity = now
        for tab in expired:
            self._stop_screencast(tab)
        self._spare_tick(now)
        if resident:
            self._tick_idle_sample(now)

    # -------------------------------------------- resident mode / watchdog (v0.1.38)
    def resident(self):
        with self._lock:
            return self._resident

    def set_resident(self, on):
        """Setting rb_resident changed (or the Hub booted with it on)."""
        on = bool(on)
        with self._lock:
            changed = on != self._resident
            self._resident = on
            if on:
                self._user_stopped = False
            else:
                self._no_tabs_since = self._last_activity = self._clock()
                self._trim_at = self._idle_sample_at = None
        if changed or on:
            self.wd.event("resident", "常駐をオンにしました" if on else "常駐をオフにしました")
            debuglog.log("rb", "info", "resident", {"on": on})
        if on:
            self.wd.unblock()
            self.wd.schedule_initial()
            self._ensure_monitor()
            self._after_start_trim()
            self._spawn(self.tick)
        else:
            if self.wd.state in (rb_resident.STATE_BLOCKED, rb_resident.STATE_GAVE_UP):
                self.wd.reset()
            else:
                self.wd.cancel()

    def retry(self):
        """The 再試行 button after the watchdog gave up."""
        with self._lock:
            self._user_stopped = False
            running = self._state in ("running", "starting")
        if running:
            self.wd.reset()
            return False
        self.wd.retry()
        debuglog.log("rb", "info", "watchdog retry")
        self._ensure_monitor()
        self._spawn(self.tick)
        return True

    def _tick_stopped(self):
        """Stopped: restart after an unexpected stop, or start the resident browser."""
        with self._lock:
            resident = self._resident
            hold = self._user_stopped
        wd = self.wd
        if wd.state == rb_resident.STATE_IDLE and resident and not hold:
            wd.schedule_initial()
        if not wd.due():
            return
        if hold and wd.state != rb_resident.STATE_STARTING:
            wd.cancel()
            return
        with self._open_lock:
            with self._lock:
                if self._state != "stopped" or self._setup_active or self._user_stopped:
                    return
            if not self.image_ready():
                wd.disarm("NASブラウザのイメージがないため自動起動しません")
                debuglog.log("rb", "warn", "watchdog blocked: image missing")
                return
            reason = self.memory_reason()
            if reason:
                first = wd.state != rb_resident.STATE_MEMORY
                wd.memory_blocked()
                if first:
                    self.log("自動起動を待機: " + reason)
                    debuglog.log("rb", "warn", "watchdog memory wait")
                return
            wd.begin_attempt()
            try:
                self._start()
            except BrowserError as error:
                self.log("自動起動に失敗: %s" % error)
                debuglog.log("rb", "warn", "watchdog start failed", {"state": wd.state})
                wd.failed(str(error))
                return
            toast = wd.succeeded()
            debuglog.log("rb", "info", "watchdog started", {"restarted": toast})
        self._after_start_trim()

    def _exit_info(self, hint):
        """docker's State of the container that disappeared (before it is removed)."""
        info = dict(hint or {})
        try:
            state = self.runtime.container_state(cr.BROWSER_NAME)
        except cr.ContainerError:
            state = {}
        if state.get("exists") and state.get("running") is False:
            for key in ("exit_code", "oom", "error", "finished_at"):
                if key in state and key not in info:
                    info[key] = state[key]
        return info

    def _unexpected_exit(self, hint):
        """The container stopped (or the DevTools connection died) without the Hub asking for it."""
        info = self._exit_info(hint)
        with self._lock:
            if self._state != "running":
                return
            tabs_were = len(self._tabs)
            resident = self._resident
        self._teardown()
        text = rb_resident.reason_text(info)
        self.log("NASブラウザが予期せず停止: " + text)
        debuglog.log("rb", "warn", "unexpected stop", {"tabs": tabs_were, "resident": resident,
                                                       "exit_code": info.get("exit_code"),
                                                       "oom": info.get("oom") is True})
        if resident or tabs_were:
            self.wd.exited(info, tabs_lost=tabs_were > 0)
        else:
            self.wd.event("exit", "停止を検出（%s）。タブも常駐もないため再起動しません" % text)

    def _release_tabs(self):
        """Close every tab (resident, no viewer activity): the container stays up."""
        with self._lock:
            tabs = list(self._tabs.values())
        for tab in tabs:
            self._drop_tab(tab, close=True)
        self._spare_close("viewer idle")

    # ---- memory trimming and figures
    def _after_start_trim(self):
        with self._lock:
            resident = self._resident and not self._tabs and self._state == "running"
        if resident:
            self._schedule_trim()

    def _schedule_trim(self):
        self._spawn(self._trim)

    def _trim(self):
        """Ask Chromium to give memory back; best effort, every error is ignored."""
        with self._lock:
            if self._state != "running" or not self._resident or self._tabs:
                return
        self._spare_close("trim")          # the idle baseline and the trim are measured without it
        with self._lock:
            if self._state != "running" or not self._resident or self._tabs:
                return
            anchor = self._anchor
            self._trim_at = self._clock()
            self._idle_sample_at = None
        try:
            self._call("Memory.simulatePressureNotification", {"level": "critical"},
                       timeout=TRIM_TIMEOUT)
        except BrowserError:
            pass
        if isinstance(anchor, str):
            session = None
            try:
                session = self._call("Target.attachToTarget", {"targetId": anchor, "flatten": True},
                                     timeout=TRIM_TIMEOUT).get("sessionId")
                if isinstance(session, str) and session:
                    self._call("HeapProfiler.collectGarbage", session=session, timeout=TRIM_TIMEOUT)
            except BrowserError:
                pass
            if isinstance(session, str) and session:
                try:
                    self._call("Target.detachFromTarget", {"sessionId": session}, timeout=TRIM_TIMEOUT)
                except BrowserError:
                    pass
        debuglog.log("rb", "info", "memory trim")

    def container_memory(self, fresh=False, allow_stats=True):
        """Current memory of the browser container (cgroup, else docker stats), cached 5 s.

        `allow_stats=False` (v0.1.46, the /api/state path) never starts `docker stats`: without a readable
        cgroup it uses a cached result up to STATS_STALE_MAX old, else returns None (not cached).
        """
        now = self._clock()
        with self._lock:
            cached = self._mem_cache
            running = self._state == "running"
        if not running:
            return None
        if cached is not None and not fresh and now - cached[0] < rb_resident.MEM_TTL:
            if cached[1] is not None or allow_stats:
                return cached[1]
        value = None
        if not allow_stats and not fresh and now < self._mem_none_until:
            return None            # looked just now and found nothing cheap: do not inspect again yet
        paths = diskio.container_cgroup_paths(self.runtime, cr.BROWSER_NAME,
                                              self._cgroup_root, self._proc_root)
        found = rb_resident.read_cgroup_memory(paths) if paths else None
        if found is not None:
            value = found
        else:
            try:
                stats = self._stats() if allow_stats else self._cached_stats()
                if stats is not None:
                    value = {"used_bytes": stats["used_bytes"], "anon_bytes": None, "file_bytes": None,
                             "source": "docker stats"}
            except BrowserError:
                value = None
        if value is None and not allow_stats:
            self._mem_none_until = now + 15.0
            return None
        with self._lock:
            self._mem_cache = (now, value)
        return value

    def _tick_idle_sample(self, now):
        """Resident and no tabs for 60 s after the trim: record the idle memory."""
        with self._lock:
            if self._tabs or self._trim_at is None:
                return
            if now - self._trim_at < rb_resident.IDLE_SETTLE:
                return
            last = self._idle_sample_at
            if last is not None and now - last < rb_resident.IDLE_RESAMPLE:
                return
            self._idle_sample_at = now
        memory = self.container_memory(fresh=True)
        if memory is None:
            return
        if self.idle.record(memory["used_bytes"], memory.get("anon_bytes")):
            self.wd.event("idle_sample", "待機時メモリ %d MiB" % round(memory["used_bytes"] / 1048576))
            debuglog.log("rb", "info", "idle memory", {"mib": round(memory["used_bytes"] / 1048576)})

    def resident_status(self, allow_stats=False):
        """Compact resident / watchdog / memory block for /api/rb/status."""
        with self._lock:
            resident = self._resident
            state = self._state
            hold = self._user_stopped
            tabs = sum(1 for t in self._tabs.values() if not t.selftest)
        wd = self.wd.snapshot()
        total = rb_resident.mem_total_kib(self._meminfo)
        memory = self.container_memory(allow_stats=allow_stats)
        rss = rb_resident.hub_rss_bytes(self._hub_status)
        mib = 1024 * 1024
        if wd["state"] == rb_resident.STATE_GAVE_UP:
            key = "gave_up"
        elif wd["state"] == rb_resident.STATE_MEMORY:
            key = "memory_wait"
        elif wd["state"] in (rb_resident.STATE_SCHEDULED, rb_resident.STATE_STARTING) and wd["restart_n"]:
            key = "restarting"
        elif state == "running":
            key = "resident" if resident else "running"
        elif state == "starting":
            key = "starting"
        else:
            key = "stopped"
        return {
            "on": resident, "state": key, "user_stopped": hold, "tabs": tabs,
            "memory": None if memory is None else {
                "used_mib": round(memory["used_bytes"] / mib),
                "anon_mib": None if memory.get("anon_bytes") is None else round(memory["anon_bytes"] / mib),
                "file_mib": None if memory.get("file_bytes") is None else round(memory["file_bytes"] / mib),
                "source": memory["source"]},
            "idle": self.idle.snapshot(total),
            "total_mib": None if total is None else total // 1024,
            "hub_rss_mib": None if rss is None else round(rss / mib),
            "watchdog": wd,
        }

    # ----------------------------------------------------------------- tabs
    def _tab(self, ident):
        if not isinstance(ident, str) or not TAB_ID.match(ident):
            raise BrowserError("タブの指定が不正です")
        with self._lock:
            tab = self._tabs.get(ident)
        if tab is None:
            raise TabGone("このタブはすでに閉じられています")
        return tab

    def open_tab(self, url, selftest=None, view=None):
        """Opens a tab. `selftest` (v0.1.44, only the self-test passes it) is a dict: the tab is marked as a
        self-test tab (not counted as a user tab) and, with a "fixture" callable, `url` is a virtual page
        under SELFTEST_ORIGIN that the Hub itself answers (no validation of user input is skipped:
        only that exact origin is accepted for a fixture)."""
        timeline = rb_firstload.Timeline(self._clock)
        if selftest is not None and selftest.get("fixture") is not None:
            if not isinstance(url, str) or not url.startswith(SELFTEST_ORIGIN + "/"):
                raise BrowserError("セルフテスト用のページではありません")
            checked = url
        else:
            checked = browser_safety.validate(url)
        with self._open_lock:
            self._touch()
            with self._lock:
                if self._state == "stopping":
                    raise BrowserError("停止処理中です。数秒後にもう一度お試しください")
                if selftest is None:
                    if sum(1 for t in self._tabs.values() if not t.selftest) >= MAX_TABS:
                        raise BrowserError("NASブラウザのタブは最大%d枚です。不要なタブを閉じてください" % MAX_TABS)
                elif sum(1 for t in self._tabs.values() if t.selftest) >= SELFTEST_TABS:
                    raise BrowserError("セルフテスト用のタブがすでに開いています")
                running = self._state == "running"
            if not running:
                if not self.image_ready(refresh=True):
                    with self._lock:
                        stale = self._stale
                    raise BrowserError(UPDATE_NEEDED if stale else
                                       "NASブラウザの初期設定（イメージ作成）が必要です")
            if not running and self.wd.state == rb_resident.STATE_GAVE_UP:
                raise BrowserError(self.wd.message())
            reason = self._guard_reason()
            if reason:
                self._spare_close("memory")
                raise BrowserError(reason)
            if not running:
                self._start()
                self.wd.succeeded()       # a pending automatic restart is done by this open
            created = self._create_tab(checked, selftest, timeline, self.parse_view(view))
            debuglog.log("rb", "info", "tab open", {"tab": created.get("id"), "host": self._host_of(checked),
                                                    "selftest": selftest is not None})
            return created

    def parse_view(self, view):
        """v0.1.59: {width,height,dpr[,quality]} sent with a tab-open / navigate request -> (w, h, dpr, mode) or None."""
        if view is None:
            return None
        if not isinstance(view, dict):
            raise BrowserError("viewが不正です")
        width = self._dimension(view.get("width"), VIEWPORT_W, "width")
        height = self._dimension(view.get("height"), VIEWPORT_H, "height")
        dpr = view.get("dpr")
        if (isinstance(dpr, bool) or not isinstance(dpr, (int, float)) or dpr != dpr or
                not VIEWPORT_DPR[0] <= dpr <= VIEWPORT_DPR[1]):
            raise BrowserError("dprは1〜3で指定してください")
        quality = view.get("quality")
        if quality is not None and (not isinstance(quality, str) or quality not in QUALITY_MODES):
            raise BrowserError("画質はspeed・standard・highのいずれかで指定してください")
        return (width, height, float(dpr), quality or DEFAULT_QUALITY)

    def _create_tab(self, checked, selftest=None, timeline=None, view=None):
        if selftest is None and self._spare_enabled():
            adopted = self._adopt_spare(checked, timeline)
            if adopted is not None:
                if view is not None:
                    try:
                        self.set_viewport(adopted["id"], view[0], view[1], view[2], view[3])
                    except BrowserError as error:
                        self.log("viewport of the adopted spare tab: %s" % error)
                    adopted = dict(adopted, view=self._tabs[adopted["id"]].viewport is not None)
                return adopted
        created = self._call("Target.createTarget", {
            "url": "about:blank", "newWindow": True,
            "width": WINDOW_SIZE[0], "height": WINDOW_SIZE[1]})
        target = created.get("targetId")
        if not isinstance(target, str) or not TARGET_ID.match(target):
            raise BrowserError("タブを作成できません")
        with self._lock:
            ident = "b%d" % self._next_id
            self._next_id += 1
            tab = Tab(ident, self._clock())
            tab.target_id = target
            tab.timeline = timeline
            if selftest is not None:
                tab.selftest = True
                tab.fixture = selftest.get("fixture")
            self._tabs[ident] = tab
        try:
            session = self._call("Target.attachToTarget",
                                 {"targetId": target, "flatten": True}).get("sessionId")
            if not isinstance(session, str) or not session:
                raise BrowserError("タブに接続できません")
            with self._lock:
                tab.session = session
                self._by_session[session] = tab
            tab.page_host = self._host_of(checked)
            patterns = [dict(p) for p in FETCH_PATTERNS]
            if tab.fixture is not None:
                patterns.append({"urlPattern": SELFTEST_ORIGIN + "/*"})
            ads = tab.fixture is None and self._ads_wanted(tab.page_host)
            # v0.1.54: everything the new tab needs is sent in one go (the NAS answers in tens of milliseconds
            # per command, and this used to be a dozen round trips one after the other)
            commands = [("Page.enable", None),
                        ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                        ("Fetch.enable", {"patterns": patterns}),
                        ("Emulation.setEmulatedMedia", REDUCED_MOTION)]
            if ads:
                commands += self._ads_commands(True)
            # v0.1.59: the viewer's size is applied before the navigation (no separate viewport request, no reflow)
            vscale, vmobile, vcmds = None, False, ()
            if view is not None and selftest is None:
                vscale = min(view[2], QUALITY_MODES[view[3]][0])
                vmobile = view[0] < MOBILE_WIDTH
                vcmds = [("Emulation.setDeviceMetricsOverride", {
                    "width": view[0], "height": view[1], "deviceScaleFactor": vscale, "mobile": vmobile})]
                if vmobile:
                    vcmds.append(("Emulation.setTouchEmulationEnabled", {"enabled": True}))
                commands += vcmds
            if timeline is not None:
                timeline.mark("target")
            commands.append(("Page.navigate", {"url": checked}))
            answers = self._call_many(commands, session)
            view_ok = bool(vcmds)
            for (method, _), (_, error) in zip(commands, answers):
                if error is None:
                    continue
                if method.startswith("Emulation.set") and method in [c[0] for c in vcmds]:
                    view_ok = False
                    self.log("%s failed: %s" % (method, error))
                    continue
                if method in ("Page.enable", "Emulation.setFocusEmulationEnabled", "Page.navigate"):
                    raise BrowserError("ブラウザの操作に失敗しました（%s: %s）" % (method, error))
                if method == "Fetch.enable":
                    # Not a safety control: without it hoster pages simply load.
                    self.log("Fetch.enable failed: %s" % error)
                elif method == "Emulation.setEmulatedMedia":
                    self.log("setEmulatedMedia failed: %s" % error)
                else:
                    ads = False
                    self.log("ad blocking failed: %s" % error)
            tab.ads = ads
            if view_ok:
                with self._lock:
                    tab.viewport, tab.touch, tab.quality = (view[0], view[1], vscale, view[3]), vmobile, view[3]
                    tab.client_dpr = view[2]
                    self._last_view = (view[0], view[1], view[2], view[3])
        except BrowserError:
            self._drop_tab(tab, close=True)
            raise
        self._touch()
        self._spare_schedule()
        if view is not None:
            return {"id": tab.id, "url": checked, "view": tab.viewport is not None}   # v0.1.59: was the size applied?
        return {"id": tab.id, "url": checked}

    # ------------------------------------------------------------ spare tab (v0.1.54)
    def _spare_enabled(self):
        if self._spare_opt is not None:
            return bool(self._spare_opt)
        return self._setting("rb_spare_tab", False)

    def spare_info(self):
        """{"on": option, "ready": a blank tab waits} for the status view and the tests."""
        with self._lock:
            ready = self._spare is not None
        return {"on": self._spare_enabled(), "ready": ready}

    def _spare_reason(self):
        """Why no spare tab may be kept now (Japanese text), or None. Stricter than the tab-open guard."""
        with self._lock:
            if self._state != "running":
                return "NASブラウザが動いていません"
            if sum(1 for t in self._tabs.values() if not t.selftest) >= MAX_TABS:
                return "タブが上限です"
        return self.memory_reason(MIN_AVAILABLE_KIB + SPARE_EXTRA_KIB, MEM_LIMIT_PERCENT - SPARE_MEM_MARGIN, fast=True)

    def _spare_make(self):
        """Creates the spare tab unless one exists, the option is off or memory is short. True when one waits."""
        if not self._spare_enabled():
            return False
        if not self._spare_lock.acquire(blocking=False):
            return False
        target = None
        try:
            with self._lock:
                if self._spare is not None:
                    return True
            if self._spare_reason() is not None:
                return False
            view = self._last_view or SPARE_VIEW
            width, height, dpr, mode = view
            scale = min(float(dpr), QUALITY_MODES[mode][0])
            mobile = width < MOBILE_WIDTH
            began = time.monotonic()
            created = self._call("Target.createTarget", {
                "url": "about:blank", "newWindow": True, "width": WINDOW_SIZE[0], "height": WINDOW_SIZE[1]})
            target = created.get("targetId")
            if not isinstance(target, str) or not TARGET_ID.match(target):
                target = None
                return False
            session = self._call("Target.attachToTarget", {"targetId": target, "flatten": True}).get("sessionId")
            if not isinstance(session, str) or not session:
                raise BrowserError("タブに接続できません")
            ads = self._ads_wanted("")
            commands = [("Page.enable", None), ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                        ("Fetch.enable", {"patterns": [dict(p) for p in FETCH_PATTERNS]}),
                        ("Emulation.setEmulatedMedia", REDUCED_MOTION),
                        ("Emulation.setDeviceMetricsOverride", {
                            "width": width, "height": height, "deviceScaleFactor": scale, "mobile": mobile})]
            if mobile:
                commands.append(("Emulation.setTouchEmulationEnabled", {"enabled": True}))
            if ads:
                commands += self._ads_commands(True)
            for (method, _), (_, error) in zip(commands, self._call_many(commands, session)):
                if error is None:
                    continue
                if method in ("Page.enable", "Emulation.setFocusEmulationEnabled",
                              "Emulation.setDeviceMetricsOverride", "Emulation.setTouchEmulationEnabled"):
                    raise BrowserError("%s: %s" % (method, error))
                if method.startswith("Network."):
                    ads = False
            with self._lock:
                if self._state != "running" or self._spare is not None:
                    raise BrowserError("不要になりました")
                self._spare = {"target": target, "session": session, "view": (width, height, scale, mode),
                               "dpr": float(dpr), "mobile": mobile, "ads": ads, "at": self._clock()}
            target = None
            debuglog.log("rb", "info", "spare tab ready", {"ms": int((time.monotonic() - began) * 1000)})
            return True
        except BrowserError as error:
            self.log("spare tab: %s" % error)
            return False
        finally:
            if target is not None:
                try:
                    self._call("Target.closeTarget", {"targetId": target}, timeout=5.0)
                except BrowserError:
                    pass
            self._spare_lock.release()

    def _spare_close(self, why):
        """Closes the waiting spare tab (memory short, idle, option off, browser stopping). True when one was open."""
        with self._spare_lock:
            with self._lock:
                spare, self._spare = self._spare, None
            if spare is None:
                return False
            try:
                self._call("Target.closeTarget", {"targetId": spare["target"]}, timeout=5.0)
            except BrowserError:
                pass
        debuglog.log("rb", "info", "spare tab closed", {"why": why})
        return True

    def _adopt_spare(self, checked, timeline):
        """Opens `checked` in the waiting spare tab (navigation only). None when there is no usable spare."""
        if not self._spare_lock.acquire(blocking=False):
            return None
        try:
            with self._lock:
                spare, self._spare = self._spare, None
        finally:
            self._spare_lock.release()
        if spare is None:
            return None
        with self._lock:
            ident = "b%d" % self._next_id
            self._next_id += 1
            tab = Tab(ident, self._clock())
            tab.target_id, tab.session = spare["target"], spare["session"]
            tab.timeline = timeline
            tab.viewport, tab.touch, tab.quality = spare["view"], spare["mobile"], spare["view"][3]
            tab.client_dpr = spare["dpr"]
            tab.ads = spare["ads"]
            tab.page_host = self._host_of(checked)
            self._tabs[ident] = tab
            self._by_session[tab.session] = tab
        wanted = self._ads_wanted(tab.page_host)
        commands = self._ads_commands(wanted) if wanted != spare["ads"] else []
        if timeline is not None:
            timeline.mark("target")
        commands.append(("Page.navigate", {"url": checked}))
        try:
            answers = self._call_many(commands, tab.session)
            for (method, _), (_, error) in zip(commands, answers):
                if error is not None and method == "Page.navigate":
                    raise BrowserError("navigate: %s" % error)
                if error is not None:
                    self.log("ad blocking failed: %s" % error)
                    wanted = False if wanted else wanted
            tab.ads = wanted
        except BrowserError as error:
            self.log("spare tab unusable: %s" % error)
            self._drop_tab(tab, close=True)
            return None
        self._touch()
        debuglog.log("rb", "info", "spare tab used", {"tab": tab.id})
        self._spare_schedule()
        return {"id": tab.id, "url": checked}

    def _spare_schedule(self):
        """After a tab was opened: make the next spare once the new page reached DOMContentLoaded (the
        renderer start would compete with the page for the NAS's small CPU)."""
        if not self._spare_enabled():
            return
        with self._lock:
            if self._spare is not None or self._spare_pending:
                return
            self._spare_pending = True

        def later():
            try:
                end = time.monotonic() + SPARE_WAIT
                with self._cv:
                    while time.monotonic() < end and self._state == "running" and any(
                            t.loading and not t.dcl for t in self._tabs.values()):
                        self._cv.wait(0.25)
            finally:
                with self._lock:
                    self._spare_pending = False
            self._spare_make()
        self._spawn(later)

    def _spare_tick(self, now):
        """Housekeeping (monitor thread): close a spare that is not wanted, make one that is."""
        with self._lock:
            have = self._spare is not None
            idle = (not any(not t.selftest for t in self._tabs.values()) and
                    now - self._last_activity >= SPARE_IDLE)
            running = self._state == "running"
        if not running:
            return
        if not self._spare_enabled():
            if have:
                self._spare_close("option off")
            return
        if have:
            if idle:
                self._spare_close("idle")
            elif self.memory_reason(MIN_AVAILABLE_KIB + SPARE_EXTRA_KIB, MEM_LIMIT_PERCENT - SPARE_MEM_MARGIN,
                                    cached_only=True) is not None:
                self._spare_close("memory")
        elif not idle and now - self._last_activity < SPARE_IDLE:
            self._spare_schedule()

    # ------------------------------------------------- site rules / ad blocking
    @staticmethod
    def _host_of(url):
        try:
            return (urlsplit(url).hostname or "").lower().rstrip(".")
        except ValueError:
            return ""

    def _rule_for(self, host):
        """The site rule for a page host, or None (also when the store fails)."""
        if self.rules is None or not host:
            return None
        try:
            return self.rules.site_rule_for(host)
        except Exception:
            return None

    def _ads_wanted(self, host):
        """Global switch AND the per-site exception (block_ads=0 turns it off)."""
        on = True
        if self.rules is not None:
            try:
                on = self.rules.settings().get("block_ads", True) is not False
            except Exception:
                on = True
        rule = self._rule_for(host)
        return on and not (rule is not None and rule.get("block_ads") is False)

    @staticmethod
    def _ads_commands(on):
        if on:
            return [("Network.enable", None),
                    ("Network.setBlockedURLs", {"urls": list(rb_blocklist.patterns())})]
        return [("Network.setBlockedURLs", {"urls": []})]

    def _drop_tab(self, tab, close):
        with self._cv:
            self._tabs.pop(tab.id, None)
            if tab.session:
                self._by_session.pop(tab.session, None)
            empty = not self._tabs
            if empty:
                self._no_tabs_since = self._clock()
                self._mirror_res.clear()        # v0.1.54: the shared resource cache lives only while a tab is open
            tab.frame = None
            self._snap_clear(tab)
            self._mirror_forget(tab)
            self._cv.notify_all()
        if close and tab.target_id:
            try:
                self._call("Target.closeTarget", {"targetId": tab.target_id}, timeout=5.0)
            except BrowserError:
                pass
        if empty:
            # Resident: hand the memory of the closed tab back (best effort, off this thread).
            with self._lock:
                resident = self._resident and self._state == "running"
            if resident:
                self._schedule_trim()

    def close_tab(self, ident):
        tab = self._tab(ident)
        self._touch()
        self._drop_tab(tab, close=True)
        debuglog.log("rb", "info", "tab close", {"tab": ident})
        return True

    def navigate(self, ident, url, view=None):
        checked = browser_safety.validate(url)
        view = self.parse_view(view)
        tab = self._tab(ident)
        self._touch()
        if view is not None:
            self.set_viewport(ident, view[0], view[1], view[2], view[3])    # v0.1.59: a no-op when unchanged
        tab.blocked = None
        tab.snap_gen += 1
        # v0.1.59: a link to "#section" of this very page commits no document: nothing to wait for
        tab.nav_same_doc = "#" in checked and checked.split("#")[0] == (tab.url or "").split("#")[0]
        tab.dcl = False
        tab.timeline = rb_firstload.Timeline(self._clock, tab.timeline.kind if tab.timeline else "mirror")
        self._call("Page.navigate", {"url": checked}, session=tab.session)
        return {"id": tab.id, "url": checked}

    # ------------------------------------------------------------ first copy inline (v0.1.59)
    def wait_first(self, ident, kind, timeout=FIRST_WAIT_MAX):
        """After a tab open / navigation: waits (at most `timeout` s, and only while one of FIRST_WAITERS_MAX
        slots is free) until the new document reached DOMContentLoaded, then makes the first mirror copy
        (kind "mirror", partial) or reader extraction (kind "reader") and returns it with the tab metadata the
        viewer would otherwise long-poll for: {"meta": ..., "mirror" | "reader": data | None}. Anything that
        goes wrong (timeout, busy, capture error) yields data None: the viewer then continues as before."""
        if kind not in ("mirror", "reader"):
            raise BrowserError("firstはmirrorまたはreaderを指定してください")
        tab = self._tab(ident)
        data = None
        if not tab.nav_same_doc and self._first_waiters.acquire(blocking=False):
            try:
                deadline = time.monotonic() + max(0.0, min(float(timeout), FIRST_WAIT_MAX))
                with self._cv:
                    while self._tabs.get(tab.id) is tab and self._state == "running":
                        tl = tab.timeline
                        if tl is not None and tl.has("commit") and (tab.dcl or not tab.loading):
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            tl = None
                            break
                        self._cv.wait(min(remaining, 0.5))
                    else:
                        tl = None
                if tl is not None:
                    try:
                        data = self.mirror(tab.id, full=False, partial=True) if kind == "mirror" \
                            else self.reader(tab.id)
                    except BrowserError as error:
                        debuglog.log("render", "info", "first copy inline failed", {"tab": tab.id, "kind": kind,
                                                                                     "error": str(error)[:120]})
            finally:
                self._first_waiters.release()
        return {"meta": self.page_meta(tab.id, wait=0), kind: data}

    # ------------------------------------------------------------ first-load timeline (v0.1.54)
    @staticmethod
    def _timeline_mark(tab, name, kind=None):
        tl = tab.timeline
        if tl is None or (name in ("cap0", "cap1", "doc") and not tl.has("commit")):
            return        # a copy of the blank page before the navigation committed is not the first load
        if kind is not None and not tl.has(name):
            tl.kind = kind
        tl.mark(name)

    def first_load(self, ident, stages=None, capture_kind=None):
        """The first-load breakdown of the tab's newest page open (see rb_firstload), or None."""
        tab = self._tab(ident)
        tl = tab.timeline
        return None if tl is None else tl.breakdown(stages, capture_kind)

    def first_load_report(self, ident, marks, rtts=None):
        """The phone's marks (ms since the tap) merged into the timeline; logs and returns the breakdown."""
        tab = self._tab(ident)
        tl = tab.timeline
        if tl is None or not isinstance(marks, dict):
            raise BrowserError("計測データがありません")
        if not tl.reported:
            tl.set_client(marks, rtts)
            data = tl.breakdown()
            debuglog.log("render", "info", "first load", rb_firstload.debug_fields(data))
        else:
            data = tl.breakdown()
        data["line"] = rb_firstload.line(data)
        return data

    # ------------------------------------------------------------ self-test support (v0.1.44)
    def selftest_tabs(self):
        """Ids of the tabs the self-test opened (RAM)."""
        with self._lock:
            return [t.id for t in self._tabs.values() if t.selftest]

    def selftest_info(self, ident):
        """Small read-only view of a tab for the self-test: loading flag, url, rev and screencast frame counter."""
        tab = self._tab(ident)
        with self._lock:
            return {"url": tab.url, "title": tab.title, "loading": bool(tab.loading), "rev": tab.rev,
                    "seq": tab.seq, "screencast": tab.screencast, "challenge": bool(tab.page_challenge),
                    "dcl": tab.dcl_n}

    def selftest_keep(self, ident, seconds):
        """Keeps the screencast of a self-test tab running (no idle stop) for `seconds`, and starts it."""
        tab = self._tab(ident)
        if not tab.selftest:
            raise BrowserError("セルフテスト用のタブではありません")
        seconds = max(0.0, min(float(seconds), 120.0))
        with self._lock:
            tab.keep_until = self._clock() + seconds
            starting = not tab.screencast
            tab.screencast = True
        self._touch()
        if starting:
            try:
                self._start_screencast(tab)
            except BrowserError:
                with self._lock:
                    tab.screencast = False
                raise
        return {"ok": True}

    def selftest_scroll(self, ident, dy=200):
        """Scrolls the page of a self-test tab by `dy` px (back to the top at the bottom) from an isolated
        world, so a still page produces screencast frames. Returns the new scroll offset or None."""
        tab = self._tab(ident)
        if not tab.selftest or not tab.session:
            raise BrowserError("セルフテスト用のタブではありません")
        dy = int(max(-2000, min(2000, dy)))
        expression = ("(function(){var m=Math.max(0,document.documentElement.scrollHeight-innerHeight),"
                      "y=window.scrollY+(%d);window.scrollTo(0,y>m?0:y<0?m:y);return window.scrollY;})()" % dy)
        for attempt in range(2):
            ctx = tab.iso_ctx
            if ctx is None:
                tree = self._call("Page.getFrameTree", session=tab.session, timeout=PROBE_CDP_TIMEOUT)
                frame_id = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
                if not isinstance(frame_id, str):
                    return None
                world = self._call("Page.createIsolatedWorld", {
                    "frameId": frame_id, "worldName": "qdl-selftest"}, session=tab.session,
                    timeout=PROBE_CDP_TIMEOUT)
                ctx = world.get("executionContextId")
                if not isinstance(ctx, int):
                    return None
                tab.iso_ctx = ctx
            try:
                value = ((self._call("Runtime.evaluate", {
                    "expression": expression, "contextId": ctx, "returnByValue": True, "timeout": 2000},
                    session=tab.session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value"))
            except BrowserError:
                tab.iso_ctx = None
                continue
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        return None

    def selftest_history_to(self, ident, path):
        """Puts a fixture tab back on the history entry for `path` (no challenge token); True when it moved
        or was already there. Used by the swipe diagnostic to give each step its starting page."""
        tab = self._tab(ident)
        if tab.fixture is None or not isinstance(path, str) or not path.startswith("/") or "//" in path:
            raise BrowserError("セルフテスト用のタブではありません")
        history = self._call("Page.getNavigationHistory", session=tab.session)
        entries, index = history.get("entries"), history.get("currentIndex")
        want = SELFTEST_ORIGIN + path
        if not isinstance(entries, list) or not isinstance(index, int):
            return False
        for i in sorted(range(len(entries)), key=lambda k: abs(k - index)):
            entry = entries[i] if isinstance(entries[i], dict) else {}
            url = entry.get("url") if isinstance(entry.get("url"), str) else ""
            if url == want and isinstance(entry.get("id"), int):
                if i == index:
                    return True
                self._touch()
                with self._lock:
                    tab.blocked = None
                    tab.snap_gen += 1
                self._call("Page.navigateToHistoryEntry", {"entryId": entry["id"]}, session=tab.session)
                return True
        return False

    def selftest_goto(self, ident, path):
        """Navigates a fixture tab to another page of the virtual site (nothing else is accepted)."""
        tab = self._tab(ident)
        if tab.fixture is None or not isinstance(path, str) or not path.startswith("/") or "//" in path:
            raise BrowserError("セルフテスト用のタブではありません")
        self._touch()
        with self._lock:
            tab.blocked = None
            tab.snap_gen += 1
        self._call("Page.navigate", {"url": SELFTEST_ORIGIN + path}, session=tab.session)
        return SELFTEST_ORIGIN + path

    def image_tag(self):
        """Tag of the Hub-built browser image (its suffix identifies the build), or None."""
        return self._tag()

    def browser_version(self):
        """Chromium product string (e.g. "HeadlessChrome/126.0.6478.55"), or None."""
        with self._lock:
            if self._state != "running":
                return None
        try:
            value = self._call("Browser.getVersion", timeout=5.0).get("product")
        except BrowserError:
            return None
        return value[:80] if isinstance(value, str) else None

    # ------------------------------------------------------------ viewport
    @staticmethod
    def _dimension(value, bounds, name):
        if (isinstance(value, bool) or not isinstance(value, int) or
                not bounds[0] <= value <= bounds[1]):
            raise BrowserError("%sは%d〜%dの整数で指定してください" % (name, bounds[0], bounds[1]))
        return value

    def _screencast_params(self, tab):
        view = tab.viewport
        if view is None:
            return dict(SCREENCAST)
        width, height, scale, mode = view
        return {"format": "jpeg", "quality": QUALITY_MODES[mode][1],
                "maxWidth": int(round(width * scale)), "maxHeight": int(round(height * scale)),
                "everyNthFrame": 1}

    def set_viewport(self, ident, width, height, dpr, quality=None):
        """Size the page to the viewer (CSS pixels) so no pixels are wasted."""
        width = self._dimension(width, VIEWPORT_W, "width")
        height = self._dimension(height, VIEWPORT_H, "height")
        if (isinstance(dpr, bool) or not isinstance(dpr, (int, float)) or dpr != dpr or
                not VIEWPORT_DPR[0] <= dpr <= VIEWPORT_DPR[1]):
            raise BrowserError("dprは1〜3で指定してください")
        if quality is not None and (not isinstance(quality, str) or quality not in QUALITY_MODES):
            raise BrowserError("画質はspeed・standard・highのいずれかで指定してください")
        tab = self._tab(ident)
        self._touch()
        mode = quality if quality is not None else tab.quality
        scale = min(float(dpr), QUALITY_MODES[mode][0])
        mobile = width < MOBILE_WIDTH
        view = (width, height, scale, mode)
        with self._lock:
            tab.client_dpr = float(dpr)
            if not tab.selftest:
                self._last_view = (width, height, float(dpr), mode)
            if tab.viewport == view:
                return {"ok": True, "changed": False}
            touch = tab.touch
        session = tab.session
        self._call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height, "deviceScaleFactor": scale, "mobile": mobile}, session)
        if mobile != touch:
            self._call("Emulation.setTouchEmulationEnabled", {"enabled": mobile}, session)
        with self._lock:
            tab.viewport, tab.touch, tab.quality = view, mobile, mode
            running = tab.screencast
            self._snap_clear(tab)    # other width / scale / quality: tiles would be wrong
            tab.snap_gen += 1
        if running:
            # New frame size / quality: restart the already running screencast.
            self._call("Page.stopScreencast", session=session, timeout=5.0)
            self._call("Page.startScreencast", self._screencast_params(tab), session=session)
        return {"ok": True, "changed": True}

    # ------------------------------------------------------------ events
    def _find_by_target(self, target):
        for tab in self._tabs.values():
            if tab.target_id == target:
                return tab
        return None

    def _on_event(self, message):
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        conn = self._conn
        if method == "Page.screencastFrame":
            self._on_frame(message.get("sessionId"), params, conn)
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
            self._on_target_info(method, info, conn)
        elif method in ("Page.frameNavigated", "Page.navigatedWithinDocument", "CSS.styleSheetAdded",
                        "CSS.styleSheetRemoved"):
            self._on_mirror_event(message.get("sessionId"), method, params)
        elif method in ("Target.targetDestroyed", "Target.targetCrashed"):
            with self._cv:
                tab = self._find_by_target(params.get("targetId"))
                if self._spare is not None and self._spare["target"] == params.get("targetId"):
                    self._spare = None
            if tab is not None:
                self._drop_tab(tab, close=False)
        elif method in ("Page.frameStartedLoading", "Page.frameStoppedLoading",
                        "Page.loadEventFired", "Page.domContentEventFired", "Network.requestWillBeSent"):
            probe = self._probe_sessions.get(message.get("sessionId"))
            if probe is not None:
                self._probe_event(probe, method, params)
                return
            if method == "Page.domContentEventFired":
                with self._cv:
                    owner = self._by_session.get(message.get("sessionId"))
                    if owner is not None and owner.selftest and owner.url not in ("", "about:blank"):
                        owner.dcl_n += 1           # v0.1.46: the self-test times DOMContentLoaded, not load
                    if owner is not None:
                        self._on_dcl(owner)
                return
            if method == "Page.loadEventFired":
                with self._cv:
                    stale = self._by_session.get(message.get("sessionId"))
                    if stale is not None:
                        stale.challenge_at = None
                        if stale.timeline is not None and stale.timeline.has("commit"):
                            stale.timeline.mark("load")
                        self._mirror_bump(stale)
            if method not in ("Page.frameStartedLoading", "Page.frameStoppedLoading"):
                return
            with self._cv:
                tab = self._by_session.get(message.get("sessionId"))
                if tab is not None:
                    tab.challenge_at = None
                frame_id = params.get("frameId")
                if tab is not None and isinstance(frame_id, str):
                    before = bool(tab.loading)
                    if method.endswith("StartedLoading"):
                        tab.loading.add(frame_id)
                    else:
                        tab.loading.discard(frame_id)
                    if before != bool(tab.loading):
                        tab.rev += 1
                    self._cv.notify_all()
        elif method == "Network.loadingFailed":
            blocked = params.get("blockedReason") == "inspector" or "BLOCKED_BY_CLIENT" in str(params.get("errorText"))
            tab = self._by_session.get(message.get("sessionId"))
            if blocked and tab is not None and tab.timeline is not None:
                tab.timeline.blocked += 1       # v0.1.54: counted for the first-load line (RAM only)
        elif method == "Fetch.requestPaused":
            self._on_request_paused(message.get("sessionId"), params, conn)
        elif method == "Browser.downloadWillBegin":
            self._on_download(params)
        elif method == "Page.javascriptDialogOpening":
            session = message.get("sessionId")
            tab = self._by_session.get(session)
            owner = tab if tab is not None else self._probe_sessions.get(session)
            if owner is not None and conn is not None:
                # Dialogs would freeze the page: always dismiss, never accept.
                conn.send_nowait("Page.handleJavaScriptDialog", {"accept": False}, session=session)

    def _on_dcl(self, tab):
        """DOMContentLoaded of the document after the navigation committed (caller holds the lock):
        the first copy / extraction may start now instead of waiting for the load event (v0.1.54)."""
        tl = tab.timeline
        if tl is None or not tl.has("commit") or tab.dcl:
            return
        tl.mark("dcl")
        tab.dcl = True
        tab.rev += 1               # wakes the viewer's meta long-poll
        self._cv.notify_all()
        if not tab.selftest and tab.session:
            self._spawn(lambda: self._mirror_prewarm(tab))

    def _on_frame(self, session, params, conn):
        data = params.get("data")
        ack = params.get("sessionId")
        with self._cv:
            tab = self._by_session.get(session)
            if tab is not None and (isinstance(data, str) and 0 < len(data) <= FRAME_MAX_CHARS and
                                    BASE64.match(data)):
                # Only the newest frame is kept; older ones are simply replaced.
                tab.frame = data
                tab.seq += 1
                if debuglog.LOG._enabled:
                    self._debug_frame_rate(tab)
                tab.vp_rev += 1      # the picture changed: the viewport link map may be stale
                meta = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
                width, height = meta.get("deviceWidth"), meta.get("deviceHeight")
                if (isinstance(width, (int, float)) and isinstance(height, (int, float)) and
                        0 < width <= 20000 and 0 < height <= 20000):
                    tab.size = (int(width), int(height))
            if tab is not None:
                self._cv.notify_all()
        # Ack at once (still on the reader thread, no waiting for a response), so
        # Chromium keeps producing frames without waiting for the viewer's next poll.
        if tab is not None and isinstance(ack, int) and not isinstance(ack, bool) and conn is not None:
            try:
                conn.send_nowait("Page.screencastFrameAck", {"sessionId": ack}, session=session)
            except CDPError:
                pass

    @staticmethod
    def _debug_frame_rate(tab):
        now = time.monotonic()
        tab.dbg_frames += 1
        if tab.dbg_t == 0.0:
            tab.dbg_t = now
        elif now - tab.dbg_t >= 10.0:
            debuglog.log("render", "debug", "screencast frames", {
                "tab": tab.id, "fps": round(tab.dbg_frames / (now - tab.dbg_t), 1)})
            tab.dbg_frames, tab.dbg_t = 0, now

    # ------------------------------------------------- intercepts (RAM only)
    def _record_event(self, tab, kind, url, **extra):
        now = self._clock()
        with self._cv:
            last = tab.intercepts[-1] if tab.intercepts else None
            if (last is not None and last["kind"] == kind and last["url"] == url and
                    now - last["t"] < INTERCEPT_DEDUP):
                return
            tab.intercept_seq += 1
            event = {"id": tab.intercept_seq, "kind": kind, "url": url, "t": now}
            event.update({k: v for k, v in extra.items() if k in EVENT_EXTRAS})
            tab.intercepts.append(event)
            tab.rev += 1
            self._cv.notify_all()

    def _add_intercept(self, tab, kind, url):
        if kind == "hoster" and self._maybe_auto_send(tab, url):
            return
        self._record_event(tab, kind, url)

    def _maybe_auto_send(self, tab, url):
        """True when this hoster link is being sent to JD2 by a site's auto_send rule."""
        if self.auto_send is None:
            return False
        rule = self._rule_for(tab.page_host)
        if rule is None or rule.get("auto_send") is not True:
            return False
        with self._lock:
            if url in self._auto_inflight:
                return True
            self._auto_inflight.add(url)
        self._spawn(lambda: self._auto_send_worker(tab, url))
        return True

    def _auto_send_worker(self, tab, url):
        try:
            outcome = self.auto_send(url)
        except Exception as error:
            # Fall back to the sheet, and tell the viewer why it was not automatic.
            reason = str(error)[:200] if isinstance(error, (ValueError, RuntimeError)) else ""
            with self._cv:
                tab.blocked = "自動送信できませんでした" + (": " + reason if reason else "")
                tab.rev += 1
                self._cv.notify_all()
            self._record_event(tab, "hoster", url)
            return
        finally:
            with self._lock:
                self._auto_inflight.discard(url)
        if outcome in ("sent", "dup"):
            self._record_event(tab, "auto_sent", url, dup=(outcome == "dup"))
        else:
            self._record_event(tab, "hoster", url)    # not possible now: show the sheet

    def _on_request_paused(self, session, params, conn):
        request_id = params.get("requestId")
        request = params.get("request") if isinstance(params.get("request"), dict) else {}
        url = request.get("url") if isinstance(request.get("url"), str) else ""
        if conn is None or not isinstance(request_id, str):
            return
        probe = self._probe_sessions.get(session)
        if probe is not None:
            self._probe_paused(probe, request_id, url, params.get("resourceType"), conn)
            return
        now = self._clock()
        with self._cv:
            tab = self._by_session.get(session)
            fixture = tab.fixture if tab is not None else None
            bypassed = False
            if tab is not None:
                tab.bypass = {u: t for u, t in tab.bypass.items() if t > now}
                bypassed = tab.bypass.pop(url, None) is not None
        if fixture is not None and url.startswith(SELFTEST_ORIGIN + "/"):
            self._serve_fixture(fixture, session, request_id, url, conn)
            return
        normalized = rapidgator_extract.normalize(url) if len(url) <= 4096 else None
        try:
            if tab is None or bypassed or params.get("resourceType") != "Document" or normalized is None:
                # Never block what is not a recognisable hoster file page.
                conn.send_nowait("Fetch.continueRequest", {"requestId": request_id}, session=session)
                return
            conn.send_nowait("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"},
                             session=session)
        except CDPError:
            return
        self._add_intercept(tab, "hoster", normalized)

    @staticmethod
    def _serve_fixture(fixture, session, request_id, url, conn):
        """Answers a request of the self-test virtual site from RAM (never from the network)."""
        try:
            answer = fixture(url)
        except Exception:
            answer = None
        try:
            if answer is None:
                conn.send_nowait("Fetch.fulfillRequest", {
                    "requestId": request_id, "responseCode": 404,
                    "responseHeaders": [{"name": "Content-Type", "value": "text/plain"}],
                    "body": base64.b64encode(b"not found").decode("ascii")}, session=session)
                return
            code, ctype, body = answer
            conn.send_nowait("Fetch.fulfillRequest", {
                "requestId": request_id, "responseCode": code,
                "responseHeaders": [{"name": "Content-Type", "value": ctype},
                                    {"name": "Cache-Control", "value": "no-store"}],
                "body": base64.b64encode(body).decode("ascii") if body else ""}, session=session)
        except CDPError:
            return

    def _on_download(self, params):
        url = params.get("url")
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048:
            return
        frame_id = params.get("frameId")
        if isinstance(frame_id, str) and (frame_id in self._probe_targets or
                                          frame_id in self._probe_popups):
            return      # a probe never reports downloads to a tab
        with self._cv:
            tab = self._find_by_target(frame_id) if isinstance(frame_id, str) else None
            if tab is None and self._tabs:
                tab = max(self._tabs.values(), key=lambda t: t.last_poll)
        if tab is not None:
            self._add_intercept(tab, "download", rapidgator_extract.normalize(url) or url)

    # ----------------------------------------------------------------- popups
    @staticmethod
    def _real_url(url):
        return isinstance(url, str) and url not in ("", "about:blank")

    def _popup_opened(self, target, owner, url, conn):
        with self._cv:
            self._popups[target] = owner
        if self._real_url(url):
            self._popup_resolve(target, url)
            return
        timer = threading.Timer(POPUP_WAIT, self._popup_resolve, (target, None))
        timer.daemon = True
        timer.start()

    def _popup_resolve(self, target, url):
        """Close the popup, then record what its first URL is (the opener is not moved)."""
        with self._cv:
            owner = self._popups.pop(target, None)
            alive = owner is not None and self._tabs.get(owner.id) is owner
        conn = self._conn
        if owner is None or conn is None:
            return
        try:
            conn.send_nowait("Target.closeTarget", {"targetId": target})
        except CDPError:
            return
        if not alive:
            return
        policy = (self._rule_for(owner.page_host) or {}).get("popup", "ask")
        if not self._real_url(url):
            self._popup_denied(owner, policy)
            return
        normalized = rapidgator_extract.normalize(url)
        if normalized is not None:
            self._add_intercept(owner, "hoster", normalized)
            return
        try:
            checked = browser_safety.validate(url)
        except ValueError:
            self._popup_denied(owner, policy)
            return
        if policy == "block":
            self._popup_counted(owner)
        elif policy == "allow":
            self._spawn(lambda: self._popup_allow(owner, checked))
        else:
            # Never navigate the opener on its own (ad popups would hijack the tab):
            # the viewer decides through "intercepts/open".
            self._add_intercept(owner, "popup", checked)

    def _popup_denied(self, tab, policy):
        if policy == "block":
            self._popup_counted(tab)
        else:
            self._popup_note(tab)

    def _popup_counted(self, tab):
        with self._cv:
            tab.popups_blocked += 1
            tab.rev += 1
            self._cv.notify_all()

    def _popup_allow(self, owner, checked):
        """An "allow" site: a NEW tab when the tab and memory limits permit, else
        the opener tab itself. Runs off the CDP reader thread (it may call docker)."""
        with self._lock:
            alive = self._tabs.get(owner.id) is owner
            room = sum(1 for t in self._tabs.values() if not t.selftest) < MAX_TABS
        if not alive:
            return
        if room and self.memory_reason() is None:
            self._record_event(owner, "popup_opened", checked, where="new")
            return
        try:
            self._call("Page.navigate", {"url": checked}, session=owner.session)
        except BrowserError:
            self._add_intercept(owner, "popup", checked)
            return
        self._record_event(owner, "popup_opened", checked, where="current")

    def _popup_note(self, tab):
        with self._cv:
            tab.blocked = POPUP_BLOCKED
            tab.rev += 1
            self._cv.notify_all()

    def _on_target_info(self, method, info, conn):
        target = info.get("targetId")
        if info.get("type") != "page" or not isinstance(target, str):
            return
        if target in self._probe_targets:
            return      # the hidden probe page itself
        if self._probe_target_event(method, info, conn):
            return
        with self._cv:
            tab = self._find_by_target(target)
            known = tab is not None or target == self._anchor
        if tab is None:
            if known or conn is None:
                return
            url = info.get("url") if isinstance(info.get("url"), str) else ""
            with self._cv:
                waiting = target in self._popups
                owner = self._find_by_target(info.get("openerId")) if info.get("openerId") else None
            if waiting:
                if self._real_url(url):
                    self._popup_resolve(target, url)
            elif method == "Target.targetCreated" and info.get("openerId"):
                if owner is None:
                    # Opened by something that is not one of our tabs: never keep it.
                    try:
                        conn.send_nowait("Target.closeTarget", {"targetId": target})
                    except CDPError:
                        pass
                else:
                    # A popup (window.open / target=_blank): wait for its first URL, close
                    # it, and either report a hoster link or follow it in the opener tab.
                    self._popup_opened(target, owner, url, conn)
            return
        url = info.get("url") if isinstance(info.get("url"), str) else ""
        title = info.get("title") if isinstance(info.get("title"), str) else ""
        block = not _scheme_ok(url)
        new_host = self._host_of(url) if url.startswith("https://") else ""
        ads_change = None
        if new_host and new_host != tab.page_host:
            tab.page_host = new_host
            wanted = self._ads_wanted(new_host)
            if wanted != tab.ads:
                tab.ads = wanted
                ads_change = wanted
        with self._cv:
            before = (tab.url, tab.title, tab.blocked)
            tab.url = url[:2048] if not block else tab.url
            tab.title = title[:300]
            tab.challenge_at = None
            if block:
                tab.blocked = "https以外のアドレスはブロックしました"
            if before != (tab.url, tab.title, tab.blocked):
                tab.rev += 1
            self._cv.notify_all()
        if ads_change is not None and conn is not None and tab.session:
            try:
                for method_name, params in self._ads_commands(ads_change):
                    conn.send_nowait(method_name, params, session=tab.session)
            except CDPError:
                pass
        if block and conn is not None and tab.session:
            try:
                conn.send_nowait("Page.navigate", {"url": "about:blank"}, session=tab.session)
            except CDPError:
                pass

    # ----------------------------------------------------------- screencast
    def _start_screencast(self, tab):
        debuglog.log("render", "info", "screencast start", {"tab": tab.id})
        self._call("Target.activateTarget", {"targetId": tab.target_id})
        self._call("Page.startScreencast", self._screencast_params(tab), session=tab.session)

    def _stop_screencast(self, tab):
        with self._lock:
            if not tab.screencast:
                return
            tab.screencast = False
        debuglog.log("render", "info", "screencast stop", {"tab": tab.id})
        try:
            self._call("Page.stopScreencast", session=tab.session, timeout=5.0)
        except BrowserError:
            pass

    def frame(self, ident, since, wait=FRAME_WAIT_MAX, rev=None):
        """Long-poll: newest frame with seq > since, else meta only after `wait`.

        With `rev` the poll also returns as soon as the tab's metadata (url, title,
        loading, blocked note, intercepts) differs from that revision.
        """
        tab = self._tab(ident)
        wait = max(0.0, min(float(wait), FRAME_WAIT_MAX))
        with self._lock:
            tab.pollers += 1
            tab.last_poll = self._clock()
            starting = not tab.screencast
            tab.screencast = True
        self._touch()
        try:
            if starting:
                try:
                    self._start_screencast(tab)
                except BrowserError:
                    with self._lock:
                        tab.screencast = False
                    raise
            deadline = self._clock() + wait
            with self._cv:
                while (tab.seq <= since and (rev is None or tab.rev == rev) and
                       self._tabs.get(tab.id) is tab and self._state == "running"):
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        break
                    self._cv.wait(min(remaining, 1.0))
                    tab.last_poll = self._clock()
                if self._tabs.get(tab.id) is not tab:
                    raise TabGone("このタブはすでに閉じられています")
                fresh = tab.seq > since
                result = {
                    "seq": tab.seq,
                    "jpeg_base64": tab.frame if fresh else None,
                    "width": tab.size[0], "height": tab.size[1],
                    "url": tab.url, "title": tab.title,
                    "loading": bool(tab.loading), "blocked": tab.blocked,
                    "intercepts": tab.intercept_seq, "rev": tab.rev,
                    "popups": tab.popups_blocked, "editable": tab.editable,
                    "links_rev": tab.vp_rev + tab.rev,
                }
            result["challenge"] = self._challenge(tab)
            self._touch()
            return result
        finally:
            with self._lock:
                tab.pollers -= 1
                tab.last_poll = self._clock()

    def stream(self, ident, write, max_seconds=STREAM_MAX_SECONDS, heartbeat=STREAM_HEARTBEAT, sse=False):
        """Push the tab's screencast to `write(bytes)` as multipart/x-mixed-replace parts (or, with `sse`,
        as Server-Sent Events: `event: frame` + the frame's base64 JPEG, v0.1.46).

        Always the newest frame (a slow writer simply skips the ones in between); the last frame is
        repeated after `heartbeat` seconds without a new one. Ends when `write` raises OSError (the
        viewer left), when the tab or the container goes away, when a newer stream of the same tab
        evicts it (a viewer behind a buffering proxy never shows us that its old stream is gone), or
        after `max_seconds`. The SSE form starts with a 2 KiB comment (defeats small proxy buffers) and
        sends a `: ping` comment every SSE_PING_SECONDS while no frame goes out.
        """
        tab = self._tab(ident)
        stop = threading.Event()
        with self._lock:
            if len(tab.stream_stops) >= STREAM_PER_TAB:
                tab.stream_stops.pop(0).set()          # the oldest stream of this tab makes room
            if sum(len(t.stream_stops) for t in self._tabs.values()) >= STREAM_TOTAL:
                raise StreamLimit("ライブ表示の同時接続が多すぎます")
            tab.stream_stops.append(stop)
            tab.streams += 1
            self._streams += 1
            tab.pollers += 1
            tab.last_poll = self._clock()
            starting = not tab.screencast
            tab.screencast = True
        stream_started = stream_logged = time.monotonic()
        stream_parts = 0
        debuglog.log("net", "info", "stream connect", {"tab": ident, "kind": "sse" if sse else "mjpeg"})
        try:
            self._touch()
            if starting:
                try:
                    self._start_screencast(tab)
                except BrowserError:
                    with self._lock:
                        tab.screencast = False
                    raise
            started = last_sent = last_write = time.monotonic()
            if sse:
                write(SSE_PADDING)
                last_write = time.monotonic()
            end = started + max_seconds
            since, last = 0, None
            while time.monotonic() < end and not stop.is_set():
                with self._cv:
                    while (tab.seq <= since and self._tabs.get(tab.id) is tab and
                           self._state == "running" and not stop.is_set()):
                        now = time.monotonic()
                        remaining = min(1.0, heartbeat - (now - last_sent), end - now)
                        if sse:
                            remaining = min(remaining, SSE_PING_SECONDS - (now - last_write))
                        if remaining <= 0:
                            break
                        self._cv.wait(remaining)
                        tab.last_poll = self._last_activity = self._clock()
                    if self._tabs.get(tab.id) is not tab or self._state != "running" or stop.is_set():
                        return
                    fresh = tab.seq > since
                    since = tab.seq
                    text = tab.frame
                if sse and not fresh and time.monotonic() - last_write >= SSE_PING_SECONDS:
                    write(SSE_PING)
                    last_write = time.monotonic()
                if fresh and text:
                    last = text if sse else base64.b64decode(text)
                elif last is None or time.monotonic() - last_sent < heartbeat:
                    if last is None:
                        last_sent = time.monotonic()       # nothing to repeat yet: keep waiting
                    continue
                if sse:
                    part = sse_event(last)
                    size = len(last) * 3 // 4
                else:
                    part = ("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                            % (STREAM_BOUNDARY, len(last))).encode("ascii") + last + b"\r\n"
                    size = len(last)
                write(part)
                last_sent = last_write = time.monotonic()
                stream_parts += 1
                if debuglog.LOG._enabled and last_sent - stream_logged >= 10.0:
                    stream_logged = last_sent
                    debuglog.log("render", "debug", "stream frames", {
                        "tab": ident, "fps": self.stream_fps(tab), "bytes": size})
                with self._lock:
                    tab.stream_times.append(last_sent)
                    tab.stream_bytes = size
                    tab.last_poll = self._last_activity = self._clock()
        finally:
            debuglog.log("net", "info", "stream disconnect", {
                "tab": ident, "seconds": int(time.monotonic() - stream_started), "parts": stream_parts})
            with self._lock:
                if stop in tab.stream_stops:
                    tab.stream_stops.remove(stop)
                tab.streams -= 1
                self._streams -= 1
                tab.pollers -= 1
                tab.last_poll = self._clock()

    def stream_fps(self, tab):
        now = time.monotonic()
        with self._lock:
            count = sum(1 for t in tab.stream_times if now - t <= STREAM_FPS_WINDOW)
        return round(count / STREAM_FPS_WINDOW, 1)

    def frame_image(self, ident, since, wait=FRAME_WAIT_MAX, rev=None):
        """Like frame() but the JPEG is returned as bytes (None when nothing newer)."""
        result = self.frame(ident, since, wait=wait, rev=rev)
        text = result.pop("jpeg_base64")
        result["jpeg"] = base64.b64decode(text) if text else None
        return result

    # ---------------------------------------------------------------- input
    @staticmethod
    def _number(value, low, high, name):
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                value != value or not low <= value <= high):
            raise BrowserError("%sの値が範囲外です" % name)
        return float(value)

    def send_input(self, ident, event):
        if not isinstance(event, dict) or event.get("type") not in INPUT_TYPES:
            raise BrowserError("入力の種類が不正です")
        tab = self._tab(ident)
        self._touch()
        kind, session = event["type"], tab.session
        if kind != "scroll":
            tab.snap_gen += 1
        if kind == "tap":
            x = self._number(event.get("x"), 0, MAX_COORD, "x")
            y = self._number(event.get("y"), 0, MAX_COORD, "y")
            base = {"x": x, "y": y, "button": "left", "clickCount": 1}
            self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session)
            self._call("Input.dispatchMouseEvent", dict(base, type="mousePressed", buttons=1), session)
            self._call("Input.dispatchMouseEvent", dict(base, type="mouseReleased", buttons=0), session)
            self._refresh_editable(tab)
        elif kind == "scroll":
            x = self._number(event.get("x"), 0, MAX_COORD, "x")
            y = self._number(event.get("y"), 0, MAX_COORD, "y")
            dx = self._number(event.get("dx", 0), -MAX_SCROLL, MAX_SCROLL, "dx")
            dy = self._number(event.get("dy", 0), -MAX_SCROLL, MAX_SCROLL, "dy")
            self._call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                    "deltaX": dx, "deltaY": dy}, session)
        elif kind == "text":
            text = event.get("text")
            if (not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT or
                    any(ord(c) < 32 and c not in "\n\t" for c in text)):
                raise BrowserError("入力文字列が不正です（1〜%d文字）" % MAX_TEXT)
            self._call("Input.insertText", {"text": text}, session)
        elif kind == "key":
            spec = KEYS.get(event.get("key"))
            if spec is None:
                raise BrowserError("対応していないキーです")
            code, vk, text = spec
            down = {"type": "keyDown" if text else "rawKeyDown", "key": event["key"], "code": code,
                    "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
            if text:
                down["text"] = text
            self._call("Input.dispatchKeyEvent", down, session)
            self._call("Input.dispatchKeyEvent", {"type": "keyUp", "key": event["key"], "code": code,
                                                  "windowsVirtualKeyCode": vk,
                                                  "nativeVirtualKeyCode": vk}, session)
        elif kind == "reload":
            tab.blocked = None
            self._call("Page.reload", session=session)
        else:
            history = self._call("Page.getNavigationHistory", session=session)
            target = history_target(history.get("entries"), history.get("currentIndex"),
                                    -1 if kind == "back" else 1)
            if target is None:
                return {"ok": True, "moved": False}
            self._call("Page.navigateToHistoryEntry", {"entryId": target[0]}, session=session)
            return {"ok": True, "moved": True, "skipped": target[1], "editable": tab.editable}
        return {"ok": True, "editable": tab.editable}

    def _refresh_editable(self, tab):
        try:
            value = (self._call("Runtime.evaluate", {
                "expression": EDITABLE_JS, "returnByValue": True, "timeout": 2000},
                session=tab.session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
        except BrowserError:
            value = False
        tab.editable = value is True

    # ------------------------------------------------- fast full-page display
    # The page is measured once (snapshot) and rendered lazily as JPEG tiles that the
    # phone scrolls natively. Tiles and link boxes are data only: nothing from the
    # remote page is ever executed on the phone, and every href is validated again
    # before the Hub acts on it.
    def _snap_clear(self, tab):
        tab.snaps.clear()
        tab.tiles.clear()
        tab.tile_bytes = 0

    def snapshot_release(self, ident):
        tab = self._tab(ident)
        with self._lock:
            self._snap_clear(tab)
        return {"ok": True}

    def _snap_drop(self, tab, snap_id):
        tab.snaps.pop(snap_id, None)
        for key in [k for k in tab.tiles if k[0] == snap_id]:
            tab.tile_bytes -= len(tab.tiles.pop(key))

    def _tile_put(self, tab, key, data):
        if len(data) > SNAP_MAX_BYTES:
            return      # never cached; still served once
        tab.tiles.pop(key, None)
        tab.tiles[key] = data
        tab.tile_bytes = sum(len(v) for v in tab.tiles.values())
        while len(tab.tiles) > 1 and (len(tab.tiles) > SNAP_MAX_TILES or
                                       tab.tile_bytes > SNAP_MAX_BYTES):
            oldest = next(iter(tab.tiles))
            tab.tile_bytes -= len(tab.tiles.pop(oldest))

    @staticmethod
    def _boxes(raw, cap, width, height, with_href):
        """Sanitised link / input boxes: finite ints, http(s) hrefs only, bounded."""
        out = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if len(out) >= cap:
                break
            if not isinstance(item, dict):
                continue
            box = {}
            for name in ("x", "y", "w", "h"):
                value = item.get(name)
                if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                        value != value or not -MAX_COORD <= value <= MAX_COORD):
                    box = None
                    break
                box[name] = int(round(value))
            if box is None or box["w"] < 1 or box["h"] < 1 or box["y"] >= height or box["x"] >= width:
                continue
            if with_href:
                href = item.get("href")
                if (not isinstance(href, str) or len(href) > SNAP_URL_MAX or
                        not (href.startswith("https://") or href.startswith("http://"))):
                    continue
                box["href"] = href
            out.append(box)
        return out

    def _challenge(self, tab):
        """Title/URL heuristic OR the page-side check (cached up to CHALLENGE_CHECK_INTERVAL)."""
        if looks_like_challenge(tab.url, tab.title):
            return True
        if not tab.session:
            return False
        now = self._clock()
        with self._lock:
            fresh = (tab.challenge_at is not None and
                     now - tab.challenge_at < CHALLENGE_CHECK_INTERVAL)
            known = tab.page_challenge
            if not fresh:
                tab.challenge_at = now   # also throttles retries after an error
        if fresh:
            return known
        try:
            value = (self._call("Runtime.evaluate", {
                "expression": CHALLENGE_JS, "returnByValue": True, "timeout": 1500},
                session=tab.session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
            found = value is True
        except BrowserError:
            found = False
        with self._lock:
            tab.page_challenge = found
        return found

    def _snap_public(self, tab, snap, reused):
        meta = dict(snap.meta)
        meta.update({"url": tab.url, "title": tab.title, "reused": reused,
                     "challenge": self._challenge(tab)})
        return meta

    def snapshot(self, ident, force=False, if_changed=False):
        """Measure the page and return the snapshot description (no tile is rendered yet).

        An unchanged snapshot younger than SNAP_REUSE is returned as is. With `if_changed`
        the newest snapshot is also returned while the page reports no DOM change.
        """
        tab = self._tab(ident)
        self._touch()
        with tab.snap_lock:
            now = self._clock()
            with self._lock:
                newest = next(reversed(tab.snaps.values()), None) if tab.snaps else None
                same = (newest is not None and newest.gen == tab.snap_gen and
                        newest.rev == tab.rev)
            if same and not force:
                if now - newest.created < SNAP_REUSE:
                    return self._snap_public(tab, newest, True)
                if if_changed and self._dom_quiet(tab):
                    return self._snap_public(tab, newest, True)
            snap_started = time.monotonic()
            reason = self.probe_memory_reason()
            if reason:
                raise BrowserError(reason)
            session = tab.session
            try:
                self._call("Target.activateTarget", {"targetId": tab.target_id}, timeout=5.0)
            except BrowserError:
                pass
            with self._lock:
                gen, rev = tab.snap_gen, tab.rev
                viewport = tab.viewport
                quality = tab.quality
            layout = self._call("Page.getLayoutMetrics", session=session,
                                timeout=PROBE_CDP_TIMEOUT)
            content = layout.get("cssContentSize") or layout.get("contentSize") or {}
            page_height = content.get("height") if isinstance(content, dict) else None
            if (isinstance(page_height, bool) or not isinstance(page_height, (int, float)) or
                    page_height != page_height or page_height < 0):
                raise BrowserError("ページの高さを取得できません")
            height = min(max(int(-(-page_height // 1)), 1), SNAP_MAX_HEIGHT)
            if viewport is not None:
                width, scale = viewport[0], viewport[2]
            else:
                visual = layout.get("cssLayoutViewport") or {}
                width = visual.get("clientWidth") if isinstance(visual, dict) else None
                width = int(width) if (isinstance(width, (int, float)) and
                                       not isinstance(width, bool) and 1 <= width <= 20000) else 1280
                scale = 1.0
            result = self._call("Runtime.evaluate", {
                "expression": SNAP_JS % (width, SNAP_MAX_HEIGHT, SNAP_LINK_CAP, SNAP_URL_MAX,
                                         SNAP_INPUT_CAP),
                "returnByValue": True, "timeout": 5000},
                session=session, timeout=PROBE_CDP_TIMEOUT)
            value = (result.get("result") or {}).get("value")
            value = value if isinstance(value, dict) else {}
            with self._lock:
                tab.snap_seq += 1
                snap = Snapshot("s%d" % tab.snap_seq, width, height, scale, quality, rev, gen, now)
                snap.meta = {
                    "snap_id": snap.id, "width": width, "height": height,
                    "tile_height": SNAP_TILE, "tiles": snap.tiles, "scale": scale,
                    "quality": quality,
                    "links": self._boxes(value.get("links"), SNAP_LINK_CAP, width, height, True),
                    "inputs": self._boxes(value.get("inputs"), SNAP_INPUT_CAP, width, height, False),
                }
                tab.snaps[snap.id] = snap
                # The previous snapshot stays (until the new one renders a tile); older ones go.
                while len(tab.snaps) > SNAP_KEEP:
                    self._snap_drop(tab, next(iter(tab.snaps)))
            debuglog.log("render", "debug", "tile snapshot", {
                "tab": tab.id, "ms": int((time.monotonic() - snap_started) * 1000),
                "tiles": snap.tiles, "height": height})
            return self._snap_public(tab, snap, False)

    def _dom_quiet(self, tab):
        try:
            value = (self._call("Runtime.evaluate", {
                "expression": DIRTY_JS, "returnByValue": True, "timeout": 2000},
                session=tab.session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
        except BrowserError:
            return False
        return value is False

    def tile(self, ident, snap_id, index):
        """JPEG bytes of one tile; rendered on first request, then served from the LRU."""
        tab = self._tab(ident)
        if (not isinstance(snap_id, str) or not SNAP_ID.match(snap_id) or
                isinstance(index, bool) or not isinstance(index, int) or index < 0):
            raise BrowserError("タイルの指定が不正です")
        self._touch()
        with self._lock:
            snap = tab.snaps.get(snap_id)
            if snap is None:
                raise SnapshotGone("この表示は古くなりました。再取得します")
            if index >= snap.tiles:
                raise BrowserError("タイルの番号が範囲外です")
            data = tab.tiles.get((snap_id, index))
            if data is not None:
                tab.tiles[(snap_id, index)] = tab.tiles.pop((snap_id, index))    # LRU: most recent last
                return data
        with tab.snap_lock:
            with self._lock:
                data = tab.tiles.get((snap_id, index))
                if data is not None:
                    return data
                if tab.snaps.get(snap_id) is not snap:
                    raise SnapshotGone("この表示は古くなりました。再取得します")
            result = self._call("Page.captureScreenshot", {
                "format": "jpeg", "quality": QUALITY_MODES[snap.quality][1],
                "clip": tile_clip(snap.width, snap.height, index),
                "captureBeyondViewport": True, "fromSurface": True},
                session=tab.session, timeout=SNAP_CDP_TIMEOUT)
            text = result.get("data")
            if (not isinstance(text, str) or not text or len(text) > SNAP_MAX_TILE_BYTES * 4 // 3 + 8
                    or not BASE64.match(text)):
                raise BrowserError("画面を取得できませんでした")
            data = base64.b64decode(text)
            with self._lock:
                if tab.snaps.get(snap_id) is snap:
                    self._tile_put(tab, (snap_id, index), data)
                    if snap_id == next(reversed(tab.snaps)):
                        for old in [k for k in tab.snaps if k != snap_id]:
                            self._snap_drop(tab, old)       # the new snapshot has its first tile
            return data

    def _viewport_point(self, tab, snap_id, x, y):
        """Scroll the page so snapshot point (x, y) is visible; the point in viewport coordinates."""
        if not isinstance(snap_id, str) or not SNAP_ID.match(snap_id):
            raise BrowserError("スナップショットの指定が不正です")
        x = self._number(x, 0, MAX_COORD, "x")
        y = self._number(y, 0, MAX_COORD, "y")
        with self._lock:
            snap = tab.snaps.get(snap_id)
        if snap is None:
            raise SnapshotGone("この表示は古くなりました。再取得します")
        if x > snap.width or y > snap.height:
            raise BrowserError("タップ位置がページの外です")
        self._touch()
        state = (self._call("Runtime.evaluate", {
            "expression": SCROLL_JS % y, "returnByValue": True, "timeout": 3000},
            session=tab.session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
        if (not isinstance(state, list) or len(state) != 4 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != v
                    for v in state)):
            raise BrowserError("ページをスクロールできません")
        scroll_x, scroll_y, inner_w, inner_h = state
        vx, vy = x - scroll_x, y - scroll_y
        if not (0 <= vx <= inner_w and 0 <= vy <= inner_h):
            raise BrowserError("タップ位置を表示できません。画面を更新してください")
        return vx, vy

    def tap_page(self, ident, snap_id, x, y):
        """Click at page coordinates (x, y) of a snapshot: scroll there, click, wait to settle."""
        tab = self._tab(ident)
        vx, vy = self._viewport_point(tab, snap_id, x, y)
        session = tab.session
        with self._lock:
            before_url, before_rev = tab.url, tab.rev
            tab.snap_gen += 1
        base = {"x": vx, "y": vy, "button": "left", "clickCount": 1}
        self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": vx, "y": vy}, session)
        self._call("Input.dispatchMouseEvent", dict(base, type="mousePressed", buttons=1), session)
        self._call("Input.dispatchMouseEvent", dict(base, type="mouseReleased", buttons=0), session)
        self._refresh_editable(tab)
        started = self._settle(tab, before_rev)
        with self._lock:
            navigated = started or tab.url != before_url
        return {"ok": True, "navigated": navigated, "editable": tab.editable,
                "snap_needed": True}

    def _settle(self, tab, before_rev, wait_load=True):
        """Wait for a navigation caused by a click: True when one started. Without `wait_load` it
        returns as soon as the navigation started (v0.1.43: the mirror follows the page itself)."""
        deadline = self._clock() + TAP_NAV_WAIT
        started = False
        with self._cv:
            while True:
                if tab.loading or tab.rev != before_rev:
                    started = True
                    if not wait_load:
                        return True
                    break
                remaining = deadline - self._clock()
                if remaining <= 0 or self._state != "running":
                    return False
                self._cv.wait(min(remaining, 0.1))
            deadline = self._clock() + TAP_SETTLE_MAX
            while tab.loading and self._state == "running" and self._tabs.get(tab.id) is tab:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cv.wait(min(remaining, 0.25))
        return started

    def page_meta(self, ident, rev=None, wait=META_WAIT_MAX, links=False, vp=None, fresh=False):
        """Long-poll of tab metadata (no screencast): returns when `rev` is out of date.

        With `links` the visible viewport's link map is added (live display); `vp` is the map
        revision the viewer already holds and `fresh` rescans the page even if nothing is flagged.
        """
        tab = self._tab(ident)
        wait = max(0.0, min(float(wait), META_WAIT_MAX))
        self._touch()
        deadline = self._clock() + wait
        if tab.mirror_on and rev is not None and wait > 0:
            self._mirror_wait(tab, rev, deadline)       # v0.1.39: DOM changes of a mirrored page end the wait
        with self._cv:
            while (rev is not None and tab.rev == rev and self._tabs.get(tab.id) is tab and
                   self._state == "running"):
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cv.wait(min(remaining, 1.0))
            if self._tabs.get(tab.id) is not tab:
                raise TabGone("このタブはすでに閉じられています")
            newest = next(reversed(tab.snaps)) if tab.snaps else None
            result = {
                "url": tab.url, "title": tab.title, "loading": bool(tab.loading),
                "blocked": tab.blocked, "intercepts": tab.intercept_seq, "rev": tab.rev,
                "popups": tab.popups_blocked, "editable": tab.editable, "snap": newest,
                "width": tab.size[0], "height": tab.size[1], "links_rev": tab.vp_rev + tab.rev,
                "streams": tab.streams, "frame_bytes": tab.stream_bytes,
                "mirror_rev": tab.mirror_rev, "dcl": tab.dcl,
            }
        result["fps"] = self.stream_fps(tab) if tab.streams else 0.0
        result["challenge"] = self._challenge(tab)
        if links:
            result["viewport_links"] = self._viewport_links(tab, vp, fresh)
        self._touch()
        return result

    @staticmethod
    def _vp_boxes(raw, width, height):
        """Sanitised viewport link boxes: finite ints, http(s) hrefs only, on screen, bounded."""
        out = []
        if not isinstance(raw, list):
            return out
        for item in raw:
            if len(out) >= VP_LINK_CAP:
                break
            if not isinstance(item, dict):
                continue
            box = {}
            for name in ("x", "y", "w", "h"):
                value = item.get(name)
                if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                        value != value or not -MAX_COORD <= value <= MAX_COORD):
                    box = None
                    break
                box[name] = int(round(value))
            if (box is None or box["w"] < 1 or box["h"] < 1 or box["x"] + box["w"] <= 0 or
                    box["y"] + box["h"] <= 0 or box["x"] >= width or box["y"] >= height):
                continue
            href = item.get("href")
            if (not isinstance(href, str) or len(href) > SNAP_URL_MAX or
                    not (href.startswith("https://") or href.startswith("http://"))):
                continue
            box["href"] = href
            out.append(box)
        return out

    def _viewport_links(self, tab, known, fresh=False):
        """{rev, scrollX, scrollY, width, height, links} of the visible viewport.

        `rev` changes whenever a screencast frame or the tab metadata changed. The page keeps a
        dirty flag (scroll end, resize, DOM mutation), so an unchanged page is not scanned again.
        Returns {"rev", "unchanged": True} when the viewer already holds this revision, and None
        when the page cannot be read right now.
        """
        if not tab.session:
            return None
        with tab.vp_lock:
            with self._lock:
                token = tab.vp_rev + tab.rev
                cache = tab.vp_cache
            if cache is None or cache["token"] != token or fresh:
                try:
                    value = (self._call("Runtime.evaluate", {
                        "expression": VP_LINKS_JS % (VP_LINK_CAP, SNAP_URL_MAX,
                                                     "true" if cache is None or fresh else "false"),
                        "returnByValue": True, "timeout": 3000},
                        session=tab.session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
                except BrowserError:
                    return None
                if isinstance(value, dict):
                    size = []
                    for name in ("width", "height"):
                        item = value.get(name)
                        size.append(int(item) if (isinstance(item, (int, float)) and
                                                  not isinstance(item, bool) and item == item and
                                                  1 <= item <= 20000) else 0)
                    scroll = []
                    for name in ("scrollX", "scrollY"):
                        item = value.get(name)
                        scroll.append(int(item) if (isinstance(item, (int, float)) and
                                                    not isinstance(item, bool) and item == item and
                                                    0 <= item <= MAX_COORD) else 0)
                    if size[0] and size[1]:
                        links = self._vp_boxes(value.get("links"), size[0], size[1])
                    else:
                        links = []
                    cache = {"token": token, "map": {
                        "scrollX": scroll[0], "scrollY": scroll[1], "width": size[0],
                        "height": size[1], "links": links}}
                elif cache is not None:
                    cache = {"token": token, "map": cache["map"]}      # nothing changed on the page
                else:
                    cache = {"token": token, "map": {"scrollX": 0, "scrollY": 0, "width": 0,
                                                     "height": 0, "links": []}}
                with self._lock:
                    tab.vp_cache = cache
            if known == cache["token"]:
                return {"rev": cache["token"], "unchanged": True}
            return dict(cache["map"], rev=cache["token"])

    # ---------------------------------------------------------------- links
    @staticmethod
    def extract_links(hrefs):
        chunks, current, size = [], [], 0
        for href in hrefs:
            if not isinstance(href, str) or len(href) > 4096:
                continue
            cost = len(href.encode("utf-8", "replace")) + 1
            if size + cost > 60000 and current:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(href)
            size += cost
        if current:
            chunks.append("\n".join(current))
        found, seen = [], set()
        for chunk in chunks:
            for url in rapidgator_extract.extract(chunk, LINK_RESULT_CAP):
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                    if len(found) >= LINK_RESULT_CAP:
                        return found
        return found

    def links(self, ident):
        tab = self._tab(ident)
        self._touch()
        result = self._call("Runtime.evaluate", {"expression": LINKS_JS, "returnByValue": True,
                                                  "timeout": 5000}, session=tab.session)
        value = (result.get("result") or {}).get("value")
        hrefs = value if isinstance(value, list) else []
        items = self.extract_links(hrefs[:LINK_CAP])
        return {"items": items, "count": len(items), "scanned": min(len(hrefs), LINK_CAP)}

    # ---------------------------------------------------------------- reader mode (v0.1.30)
    # A fixed extractor runs in the page and returns plain data (headings, paragraphs with link
    # runs, list items, images, preformatted text). The Hub validates every field again; the phone
    # builds its own DOM from that data with createElement/textContent. No remote HTML, script or
    # style reaches the phone, and image URLs never leave the Hub (only an index does).
    @staticmethod
    def _http_url(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= READER_URL_MAX or
                not (value.startswith("https://") or value.startswith("http://")) or
                any(ord(c) < 33 or ord(c) == 127 for c in value)):
            return None
        try:
            if not urlsplit(value).hostname:
                return None
        except ValueError:
            return None
        return value

    @staticmethod
    def _clean_text(value, limit=READER_TEXT_MAX):
        return CONTROL_CHARS.sub("", value)[:limit]

    @staticmethod
    def _small_int(value, low, high):
        if (isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high):
            return None
        return value

    def _reader_runs(self, raw):
        if not isinstance(raw, list):
            return None
        runs, total = [], 0
        for item in raw[:READER_RUNS_CAP]:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            text = self._clean_text(item["text"], READER_TEXT_MAX - total)
            if not text:
                continue
            run = {"text": text}
            href = self._http_url(item.get("href"))
            if href is not None:
                run["href"] = href
            runs.append(run)
            total += len(text)
            if total >= READER_TEXT_MAX:
                break
        if not any(r["text"].strip() for r in runs):
            return None
        return runs

    def _reader_block(self, item, images):
        if not isinstance(item, dict):
            return None
        kind = item.get("t")
        if kind == "hr":
            return {"t": "hr"}
        if kind == "h":
            level = self._small_int(item.get("level"), 1, 6)
            text = item.get("text")
            if level is None or not isinstance(text, str):
                return None
            text = self._clean_text(text).strip()
            return {"t": "h", "level": level, "text": text} if text else None
        if kind in ("p", "li"):
            runs = self._reader_runs(item.get("runs"))
            if runs is None:
                return None
            if kind == "p":
                block = {"t": "p", "runs": runs}
                if item.get("q") is True:      # v0.1.36: block quote (a boolean only)
                    block["q"] = True
                return block
            depth = self._small_int(item.get("depth", 0), 0, 8)
            return {"t": "li", "runs": runs, "depth": depth if depth is not None else 0}
        if kind == "pre":
            text = item.get("text")
            if not isinstance(text, str):
                return None
            text = self._clean_text(text)
            return {"t": "pre", "text": text} if text.strip() else None
        if kind == "img":
            index = self._small_int(item.get("i"), 0, READER_IMAGE_CAP - 1)
            if index is None or index >= len(images) or images[index] is None:
                return None
            alt = item.get("alt")
            block = {"t": "img", "i": index,
                     "alt": self._clean_text(alt, READER_ALT_MAX) if isinstance(alt, str) else "",
                     "w": self._small_int(item.get("w"), 0, 20000) or 0,
                     "h": self._small_int(item.get("h"), 0, 20000) or 0}
            href = self._http_url(item.get("href"))
            if href is not None:
                block["href"] = href
            return block
        return None

    def _reader_hosters(self, raw, present):
        out = []
        if not isinstance(raw, list):
            return out
        for value in raw:
            href = self._http_url(value)
            if href is None or href in present or href in out:
                continue
            try:
                host = (urlsplit(href).hostname or "").lower()
            except ValueError:
                continue
            if host in READER_HOSTS:
                out.append(href)
                if len(out) >= READER_HOSTER_CAP:
                    break
        return out

    def _reader_clean(self, value):
        """(public data, image table) from the extractor output, or BrowserError."""
        if not isinstance(value, dict):
            raise BrowserError("本文を取り出せませんでした")
        raw_images = value.get("images")
        images = [self._http_url(u) for u in (raw_images[:READER_IMAGE_CAP]
                                              if isinstance(raw_images, list) else [])]
        blocks = []
        raw_blocks = value.get("blocks")
        raw_blocks = raw_blocks if isinstance(raw_blocks, list) else []
        present = set()
        for item in raw_blocks:
            for run in (item.get("runs") if isinstance(item, dict) and
                        isinstance(item.get("runs"), list) else []):
                if isinstance(run, dict) and isinstance(run.get("href"), str):
                    present.add(run["href"])
        hosters = self._reader_hosters(value.get("hosters"), present)
        reserve = len(hosters) + 1 if hosters else 0
        truncated = value.get("truncated") is True
        size = 0
        for item in raw_blocks:
            block = self._reader_block(item, images)
            if block is None:
                continue
            cost = len(json.dumps(block, ensure_ascii=False))
            if len(blocks) >= READER_BLOCK_CAP - reserve or size + cost > READER_MAX_JSON - 64 * 1024:
                truncated = True
                break
            blocks.append(block)
            size += cost
        if hosters:
            blocks.append({"t": "h", "level": 2, "text": READER_HOSTER_TITLE})
            blocks.extend({"t": "li", "runs": [{"text": href, "href": href}], "depth": 0}
                          for href in hosters)
        title = value.get("title")
        title = self._clean_text(title, READER_TITLE_MAX).strip() if isinstance(title, str) else ""
        return {"title": title, "blocks": blocks, "count": len(blocks), "truncated": truncated,
                "hoster_links": len(hosters)}, images

    def _reader_fallbacks(self, value, count):
        """Smaller alternative URL per image index (None when absent or invalid); Hub only."""
        raw = value.get("fallbacks") if isinstance(value, dict) else None
        raw = raw if isinstance(raw, list) else []
        out = [self._http_url(u) for u in raw[:count]]
        return out + [None] * (count - len(out))

    def reader(self, ident):
        """Reader data of the page: validated blocks (see the section comment)."""
        tab = self._tab(ident)
        self._touch()
        reader_started = time.monotonic()
        self._timeline_mark(tab, "cap0", "reader")
        try:
            result = self._call("Runtime.evaluate", {
                "expression": READER_JS, "returnByValue": True, "timeout": 15000},
                session=tab.session, timeout=READER_TIMEOUT)
        except BrowserTimeout:
            raise BrowserError("ページの本文取得がタイムアウトしました（広告の多いページなどで応答しない場合があります）") from None
        except BrowserError as error:
            if "terminated" in str(error).lower():
                raise BrowserError("ページの本文取得がタイムアウトしました（広告の多いページなどで応答しない場合があります）") from None
            raise
        value = (result.get("result") or {}).get("value")
        data, images = self._reader_clean(value)
        fallbacks = self._reader_fallbacks(value, len(images))
        with self._lock:
            if self._tabs.get(tab.id) is not tab:
                raise TabGone("このタブはすでに閉じられています")
            tab.reader_images = images
            tab.reader_fallbacks = fallbacks
            data.update({"url": tab.url, "host": self._host_of(tab.url)})
            if not data["title"]:
                data["title"] = tab.title[:READER_TITLE_MAX]
        self._touch()
        self._timeline_mark(tab, "cap1")
        if tab.timeline is not None:
            tab.timeline.capture_ms.setdefault("reader", int((time.monotonic() - reader_started) * 1000))
        self._timeline_mark(tab, "doc")        # the reader has no separate document: the data is the delivery
        debuglog.log("render", "debug", "reader extract", {
            "tab": tab.id, "ms": int((time.monotonic() - reader_started) * 1000),
            "blocks": data.get("count")})
        return data

    @staticmethod
    def sniff_image(data):
        """MIME type from the magic bytes: JPEG, PNG, GIF, WebP or AVIF only (never SVG)."""
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
            return "image/avif"
        return None

    @staticmethod
    def _find_resource_frame(node, url, budget):
        """frameId of the frame whose resource list holds `url` (an Image), searching child frames."""
        if not isinstance(node, dict) or budget[0] <= 0:
            return None
        budget[0] -= 1
        frame = node.get("frame") if isinstance(node.get("frame"), dict) else {}
        for resource in node.get("resources") if isinstance(node.get("resources"), list) else []:
            if (isinstance(resource, dict) and resource.get("url") == url and
                    resource.get("type") == "Image" and isinstance(frame.get("id"), str)):
                return frame["id"]
        for child in node.get("childFrames") if isinstance(node.get("childFrames"), list) else []:
            found = RemoteBrowser._find_resource_frame(child, url, budget)
            if found is not None:
                return found
        return None

    @staticmethod
    def _decode_image(text):
        if (not isinstance(text, str) or not text or len(text) > IMAGE_MAX_BYTES * 4 // 3 + 8 or
                not BASE64.match(text)):
            raise ImageRefused("画像が大きすぎるか、形式が対応していません")
        data = base64.b64decode(text)
        if len(data) > IMAGE_MAX_BYTES:
            raise ImageRefused("画像が大きすぎます")
        return data

    def _image_from_resources(self, tab, url):
        """Bytes of an already loaded image (Page.getResourceTree + getResourceContent), else None."""
        try:
            tree = self._call("Page.getResourceTree", session=tab.session, timeout=PROBE_CDP_TIMEOUT)
            frame_id = self._find_resource_frame(tree.get("frameTree"), url, [64])
            if frame_id is None:
                return None
            content = self._call("Page.getResourceContent", {"frameId": frame_id, "url": url},
                                 session=tab.session, timeout=PROBE_CDP_TIMEOUT)
        except BrowserError:
            return None
        if content.get("base64Encoded") is not True:
            raise ImageRefused("画像が大きすぎるか、形式が対応していません")   # text, e.g. SVG
        return self._decode_image(content.get("content"))

    def _image_from_page(self, tab, url):
        """Fetch inside the page (its cookies and referrer apply). None when that fails."""
        try:
            value = (self._call("Runtime.evaluate", {
                "expression": IMAGE_FETCH_JS % (json.dumps(url), IMAGE_MAX_BYTES),
                "returnByValue": True, "awaitPromise": True, "timeout": 10000},
                session=tab.session, timeout=IMAGE_CDP_TIMEOUT).get("result") or {}).get("value")
        except BrowserError:
            return None
        if not isinstance(value, dict):
            return None
        if value.get("err") == "size":
            raise ImageRefused("画像が大きすぎます")
        if value.get("err") or "d" not in value:
            return None
        return self._decode_image(value.get("d"))

    def _fetch_reader_image(self, tab, url):
        data = self._image_from_resources(tab, url)
        if data is None:
            data = self._image_from_page(tab, url)
        if data is None:
            raise ImageUnavailable("画像を取得できません")
        kind = self.sniff_image(data)
        if kind is None:
            raise ImageRefused("対応していない画像形式です")
        return data, kind

    def reader_image(self, ident, index):
        """(bytes, mime type) of reader image `index`, read through the NAS browser."""
        tab = self._tab(ident)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise BrowserError("画像の番号が不正です")
        with self._lock:
            images = tab.reader_images
            url = images[index] if index < len(images) else None
            fallbacks = tab.reader_fallbacks
            fallback = fallbacks[index] if index < len(fallbacks) else None
        if url is None:
            raise ReaderImageGone("この画像は見つかりません。読み込み直してください")
        self._touch()
        try:
            return self._fetch_reader_image(tab, url)
        except (ImageRefused, ImageUnavailable):
            # v0.1.36: the large srcset candidate was too big / unsupported / failed: use the
            # smaller one the page itself showed (same 4 MB cap and magic-byte check).
            if fallback is None or fallback == url:
                raise
            return self._fetch_reader_image(tab, fallback)

    # ------------------------------------------------------------ mirror display (v0.1.39)
    # The NAS Chromium renders the page. A fixed serializer runs in an ISOLATED WORLD of the page
    # (page scripts cannot see or change it) and returns plain text; rb_mirror re-parses it with an
    # allow-list and rewrites every resource URL into a Hub reference. The phone shows the result in
    # a sandboxed iframe without scripts; images and fonts come through mirror_resource(), which
    # fetches them with the NAS browser, never from the Hub.
    def _mirror_bump(self, tab):
        with self._cv:
            tab.mirror_rev += 1
            if tab.mirror_on:
                tab.rev += 1
            self._cv.notify_all()

    def _mirror_clear(self, tab, keep_resources=False):
        tab.mirror_ctx = None
        tab.mirror_frame = None
        tab.mirror_seen = -1
        tab.mirror_cache = None
        tab.mirror_doc = None
        tab.mirror_tree = None
        tab.mirror_sheets.clear()
        tab.mirror_tokens.clear()
        tab.mirror_sheet_cache.clear()
        tab.mirror_css_text.clear()
        tab.mirror_prefetch = 0
        if not keep_resources:
            self._mirror_res.drop_tab(tab.id)

    def _mirror_reset(self, tab):
        """A new document: nothing of the old one (world, references) may be used. The fetched bytes stay in
        the shared cache (v0.1.54): the next page of the site reuses its logo, fonts and style images."""
        self._mirror_clear(tab, keep_resources=True)
        self._mirror_bump(tab)

    def _mirror_forget(self, tab):
        self._mirror_clear(tab)
        tab.mirror_on = False

    def _on_mirror_event(self, session, method, params):
        with self._lock:
            tab = self._by_session.get(session)
        if tab is None:
            return
        if method == "Page.frameNavigated":
            frame = params.get("frame") if isinstance(params.get("frame"), dict) else {}
            if not frame.get("parentId"):
                tab.dcl = False
                url = frame.get("url")
                if tab.timeline is not None and isinstance(url, str) and url not in ("", "about:blank"):
                    tab.timeline.mark("commit")
                self._mirror_reset(tab)
        elif method == "Page.navigatedWithinDocument":
            self._mirror_bump(tab)
        elif method == "CSS.styleSheetAdded":
            header = params.get("header") if isinstance(params.get("header"), dict) else {}
            source, ident = header.get("sourceURL"), header.get("styleSheetId")
            if (isinstance(source, str) and isinstance(ident, str) and source and
                    (len(tab.mirror_sheets) < MIRROR_SHEET_CAP or source in tab.mirror_sheets)):
                tab.mirror_sheets[source[:rb_mirror.URL_MAX]] = ident
        elif method == "CSS.styleSheetRemoved":
            ident = params.get("styleSheetId")
            for source in [k for k, v in tab.mirror_sheets.items() if v == ident]:
                tab.mirror_sheets.pop(source, None)

    def _mirror_world(self, tab, fresh=False):
        """Execution context id of the tab's isolated world (created again after a navigation)."""
        with self._lock:
            ctx = tab.mirror_ctx
        if ctx is not None and not fresh:
            return ctx
        tree = self._call("Page.getFrameTree", session=tab.session, timeout=PROBE_CDP_TIMEOUT)
        frame = ((tree.get("frameTree") or {}).get("frame") or {}).get("id")
        if not isinstance(frame, str) or not frame:
            raise BrowserError("ページの状態を取得できません")
        made = self._call("Page.createIsolatedWorld", {"frameId": frame, "worldName": MIRROR_WORLD},
                          session=tab.session, timeout=PROBE_CDP_TIMEOUT)
        ctx = made.get("executionContextId")
        if isinstance(ctx, bool) or not isinstance(ctx, int):
            raise BrowserError("ページの状態を取得できません")
        with self._lock:
            tab.mirror_ctx, tab.mirror_frame = ctx, frame
        return ctx

    def _mirror_eval(self, tab, expression, timeout=MIRROR_TIMEOUT, await_promise=False):
        """Value of `expression` evaluated in the isolated world (one retry in a new world)."""
        for attempt in (0, 1):
            ctx = self._mirror_world(tab, fresh=attempt == 1)
            params = {"expression": expression, "contextId": ctx, "returnByValue": True,
                      "timeout": int(timeout * 1000) - 1000 if timeout > 3 else 2000}
            if await_promise:
                params["awaitPromise"] = True
            try:
                result = self._call("Runtime.evaluate", params, session=tab.session, timeout=timeout)
            except BrowserTimeout:
                raise
            except BrowserError as error:
                if attempt == 0 and ("context" in str(error).lower() or "frame" in str(error).lower()):
                    with self._lock:
                        tab.mirror_ctx = None
                    continue
                raise
            if result.get("exceptionDetails"):
                raise BrowserError("ページの複製に失敗しました")
            return (result.get("result") or {}).get("value")
        raise BrowserError("ページの複製に失敗しました")

    def _mirror_poll(self, tab):
        """Reads the DOM change counter; a change bumps mirror_rev (and wakes the meta long-poll)."""
        try:
            value = self._mirror_eval(tab, rb_mirror.COUNT_JS, timeout=PROBE_CDP_TIMEOUT)
            changed = False
            if value == -1:         # a new document: the observer is not there yet
                value = self._mirror_eval(tab, rb_mirror.WATCH_JS, timeout=PROBE_CDP_TIMEOUT)
                changed = True
        except BrowserError:
            return
        if isinstance(value, bool) or not isinstance(value, int):
            return
        if changed or value != tab.mirror_seen:
            tab.mirror_seen = value
            self._mirror_bump(tab)

    def _mirror_default(self):
        """True when the option makes the mirror the display of new pages."""
        if self.rules is None:
            return False
        try:
            return self.rules.settings().get("rb_default_mode") == "mirror"
        except Exception:
            return False

    def _mirror_prewarm(self, tab):
        """At DOMContentLoaded (mirror is or will be shown): create the isolated world, switch the CSS agent
        on and start the change observer now, so the first copy - requested a round trip later - finds them
        ready instead of paying for them (v0.1.54). Best effort; skipped while a copy is being made."""
        if self._tabs.get(tab.id) is not tab or not (tab.mirror_on or self._mirror_default()):
            return
        if not tab.mirror_lock.acquire(blocking=False):
            return
        try:
            if self._tabs.get(tab.id) is not tab:
                return
            self._mirror_css_enable(tab)
            self._mirror_poll(tab)
        except BrowserError:
            pass
        finally:
            tab.mirror_lock.release()

    def _mirror_wait(self, tab, rev, deadline):
        while True:
            with self._cv:
                if tab.rev != rev or self._tabs.get(tab.id) is not tab or self._state != "running":
                    return
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return
                self._cv.wait(min(remaining, MIRROR_POLL))
                if tab.rev != rev:
                    return
            self._mirror_poll(tab)

    def _mirror_css_enable(self, tab):
        if tab.mirror_css:
            return
        try:
            self._call("DOM.enable", session=tab.session, timeout=PROBE_CDP_TIMEOUT)
            self._call("CSS.enable", session=tab.session, timeout=PROBE_CDP_TIMEOUT)
            tab.mirror_css = True
        except BrowserError as error:
            self.log("CSS.enable failed: %s" % error)

    def _mirror_css_text(self, tab, url):
        sheet = tab.mirror_sheets.get(url)
        if sheet is None:
            return None
        known = tab.mirror_css_text.get((url, sheet))
        if known is not None:
            return known                    # v0.1.54: a recapture of the same page does not ask Chromium again
        try:
            text = self._call("CSS.getStyleSheetText", {"styleSheetId": sheet}, session=tab.session,
                              timeout=PROBE_CDP_TIMEOUT).get("text")
        except BrowserError:
            return None
        if isinstance(text, str) and len(text) <= MIRROR_CSS_TEXT_MAX:
            tab.mirror_css_text[(url, sheet)] = text
            return text
        return None

    def mirror(self, ident, full=True, force=False, partial=False):
        """Capture of the page for the mirror display (see rb_mirror.assemble); cached per page version.

        `partial` (v0.1.54, first copy of a page) asks for "above the fold first": on a large page only the
        first screens are complete (`partial: true` in the answer) and the caller follows with a normal request.
        """
        tab = self._tab(ident)
        self._touch()
        tab.mirror_on = True
        with tab.mirror_lock:
            self._mirror_poll(tab)
            with self._lock:
                rev = tab.mirror_rev
                cached = tab.mirror_cache
            if (cached is not None and cached["rev"] == rev and not force and
                    not (cached.get("partial") and not partial)):
                debuglog.log("render", "debug", "mirror capture cached", {"tab": tab.id, "rev": rev})
                return self._mirror_public(tab, cached, full, reused=True)
            started = time.monotonic()
            self._timeline_mark(tab, "cap0", "mirror")
            self._mirror_css_enable(tab)
            fold = 0
            if partial:
                view = tab.viewport
                fold = rb_mirror.PARTIAL_SCREENS * (view[1] if view else 844)
            try:
                raw = self._mirror_eval(tab, rb_mirror.serializer_script(tab.client_dpr, fold))
            except BrowserTimeout:
                raise BrowserError("ページの複製がタイムアウトしました（重いページでは時間がかかります）") from None
            js_ms = int((time.monotonic() - started) * 1000)
            if not isinstance(raw, dict):
                raise BrowserError("ページを複製できませんでした")

            fonts_seen = []
            web_fonts = not self._setting("rb_no_webfonts", True)

            def register(url, kind):
                token = tab.mirror_tokens.register(url, kind)
                if not token:
                    return None
                # a resource the Hub already holds gets its content hash into the URL: the phone may keep it
                path = rb_mirror.res_path(tab.id, token, self._mirror_res.known_hash(token))
                if kind == "font" and len(fonts_seen) < rb_mirror.PRELOAD_FONTS:
                    fonts_seen.append(path)
                return path

            def register_data(data, mime):
                token = tab.mirror_tokens.register_data(data, mime, "font")
                if not token:
                    return None
                path = rb_mirror.res_path(tab.id, token, rb_mirror.content_hash(data))
                if len(fonts_seen) < rb_mirror.PRELOAD_FONTS:
                    fonts_seen.append(path)
                return path
            try:
                capture = rb_mirror.assemble(raw, register, register_data,
                                             lambda url: self._mirror_css_text(tab, url),
                                             web_fonts=web_fonts, cache=tab.mirror_sheet_cache)
            except rb_mirror.MirrorError as error:
                raise BrowserError(str(error)) from None
            capture["preload"] = rb_mirror.preload_links(capture.get("first_images") or (), fonts_seen)
            dom = raw.get("domN")
            if isinstance(dom, int) and not isinstance(dom, bool):
                tab.mirror_seen = dom
            capture["rev"] = rev
            capture["js_ms"] = js_ms
            capture["resources"] = len(tab.mirror_tokens)
            capture["ms"] = int((time.monotonic() - started) * 1000)
            with self._lock:
                if self._tabs.get(tab.id) is not tab:
                    raise TabGone("このタブはすでに閉じられています")
                tab.mirror_cache = capture
                tab.mirror_doc = None
            self._timeline_mark(tab, "cap1")
            if capture["partial"]:
                tab.mirror_partials += 1
            if tab.timeline is not None:
                tab.timeline.capture_ms.setdefault("mirror", capture["ms"])
            debuglog.log("render", "info", "mirror capture", {
                "tab": tab.id, "ms": capture["ms"], "js_ms": js_ms, "nodes": capture["nodes"],
                "html_kb": capture["html_bytes"] // 1024, "css_kb": capture["css_bytes"] // 1024,
                "res": capture["resources"], "truncated": capture["truncated"], "partial": capture["partial"]})
            self._mirror_prefetch(tab, capture)
            return self._mirror_public(tab, capture, full, reused=False)

    @staticmethod
    def _mirror_public(tab, capture, full, reused):
        result = {
            "rev": capture["rev"], "url": capture["url"], "title": capture["title"],
            "truncated": capture["truncated"], "css_truncated": capture["css_truncated"],
            "partial": capture.get("partial") is True,
            "doc": "/api/rb/mirror/doc?tab=%s&rev=%d" % (tab.id, capture["rev"]), "reused": reused,
            "stats": {"nodes": capture["nodes"], "html_bytes": capture["html_bytes"],
                      "css_bytes": capture["css_bytes"], "res": capture["resources"],
                      "images": capture["images"], "links": capture["links"], "ms": capture["ms"],
                      "placeholders": capture["placeholders"]},
        }
        if full:
            result.update(html=capture["html"], css=capture["css"], html_attrs=capture["html_attrs"],
                          body_attrs=capture["body_attrs"])
        return result

    def mirror_document(self, ident, rev):
        """The full HTML document of the capture `rev` (what the sandboxed iframe loads)."""
        tab = self._tab(ident)
        if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
            raise BrowserError("revが不正です")
        with self._lock:
            capture, doc = tab.mirror_cache, tab.mirror_doc
        if capture is None or capture["rev"] != rev:
            raise MirrorGone("この表示は古くなりました。再取得します")
        if doc is not None and doc[0] == rev:
            return doc[1]
        text = rb_mirror.build_document(capture["css"], capture["html"], capture["html_attrs"],
                                        capture["body_attrs"], capture.get("preload", ""))
        with self._lock:
            if tab.mirror_cache is capture:
                tab.mirror_doc = (rev, text)
        self._timeline_mark(tab, "doc")
        return text

    def mirror_release(self, ident):
        """Leaves the mirror display: free everything it held (here and in the page)."""
        tab = self._tab(ident)
        was_on = tab.mirror_on
        tab.mirror_on = False
        self._mirror_clear_keep_world(tab)
        if was_on and tab.mirror_ctx is not None:
            try:
                self._mirror_eval(tab, rb_mirror.OFF_JS, timeout=PROBE_CDP_TIMEOUT)
            except BrowserError:
                pass
        if tab.mirror_css:
            tab.mirror_css = False
            for method in ("CSS.disable", "DOM.disable"):
                try:
                    self._call(method, session=tab.session, timeout=PROBE_CDP_TIMEOUT)
                except BrowserError:
                    pass
        return {"ok": True}

    def _mirror_clear_keep_world(self, tab):
        ctx, frame = tab.mirror_ctx, tab.mirror_frame
        self._mirror_clear(tab)
        tab.mirror_ctx, tab.mirror_frame = ctx, frame

    # ---- mirror resources
    def _mirror_tree_index(self, tab):
        """url -> (frame id, resource type, mime type) of the resources the page loaded (cached briefly)."""
        now = self._clock()
        cached = tab.mirror_tree
        if cached is not None and now - cached[0] < MIRROR_TREE_TTL:
            return cached[1]
        index = {}
        try:
            tree = self._call("Page.getResourceTree", session=tab.session, timeout=PROBE_CDP_TIMEOUT)
        except BrowserError:
            return index
        budget = [64]

        def walk(node):
            if not isinstance(node, dict) or budget[0] <= 0:
                return
            budget[0] -= 1
            frame = node.get("frame") if isinstance(node.get("frame"), dict) else {}
            for resource in node.get("resources") if isinstance(node.get("resources"), list) else []:
                if (isinstance(resource, dict) and isinstance(resource.get("url"), str) and
                        isinstance(frame.get("id"), str) and len(index) < 20000):
                    index.setdefault(resource["url"], (frame["id"], resource.get("type"), resource.get("mimeType")))
            for child in node.get("childFrames") if isinstance(node.get("childFrames"), list) else []:
                walk(child)
        walk(tree.get("frameTree"))
        tab.mirror_tree = (now, index)
        return index

    @staticmethod
    def _mirror_cap(kind):
        return rb_mirror.FONT_MAX_BYTES if kind == "font" else rb_mirror.IMAGE_MAX_BYTES

    def _mirror_from_tree(self, tab, url, kind, cap):
        found = self._mirror_tree_index(tab).get(url)
        if found is None or found[1] != ("Font" if kind == "font" else "Image"):
            return None
        try:
            content = self._call("Page.getResourceContent", {"frameId": found[0], "url": url},
                                 session=tab.session, timeout=PROBE_CDP_TIMEOUT)
        except BrowserError:
            return None
        text = content.get("content")
        if not isinstance(text, str) or not text:
            return None
        if content.get("base64Encoded") is True:
            if len(text) > cap * 4 // 3 + 8 or not BASE64.match(text):
                raise rb_mirror.ResourceRefused("大きすぎるか、形式が対応していません")
            data = base64.b64decode(text)
        else:
            data = text.encode("utf-8")
        if len(data) > cap:
            raise rb_mirror.ResourceRefused("大きすぎます")
        return data, found[2]

    def _mirror_from_page(self, tab, url, cap):
        """fetch() in the isolated world: the page's cookies apply, CORS decides what is readable."""
        try:
            value = self._mirror_eval(tab, MIRROR_FETCH_JS % (json.dumps(url), cap),
                                      timeout=IMAGE_CDP_TIMEOUT, await_promise=True)
        except BrowserError:
            return None
        if not isinstance(value, dict):
            return None
        if value.get("err") == "size":
            raise rb_mirror.ResourceRefused("大きすぎます")
        text = value.get("d")
        if value.get("err") or not isinstance(text, str) or not text:
            return None
        if len(text) > cap * 4 // 3 + 8 or not BASE64.match(text):
            return None
        return base64.b64decode(text), value.get("ct")

    def _mirror_from_network(self, tab, url, cap):
        """Network.loadNetworkResource: the browser loads it with its cookie jar, CORS does not apply."""
        frame = tab.mirror_frame
        if frame is None:
            return None
        try:
            loaded = self._call("Network.loadNetworkResource", {
                "frameId": frame, "url": url, "options": {"disableCache": False, "includeCredentials": True}},
                session=tab.session, timeout=IMAGE_CDP_TIMEOUT).get("resource")
        except BrowserError:
            return None
        if not isinstance(loaded, dict) or loaded.get("success") is not True or not isinstance(loaded.get("stream"), str):
            return None
        status = loaded.get("httpStatusCode")
        headers = loaded.get("headers") if isinstance(loaded.get("headers"), dict) else {}
        content_type = next((v for k, v in headers.items() if str(k).lower() == "content-type"), None)
        handle, chunks, total = loaded["stream"], [], 0
        try:
            while True:
                part = self._call("IO.read", {"handle": handle, "size": 65536}, session=tab.session,
                                  timeout=IMAGE_CDP_TIMEOUT)
                piece = part.get("data") if isinstance(part.get("data"), str) else ""
                raw = base64.b64decode(piece) if part.get("base64Encoded") is True else piece.encode("latin-1", "replace")
                total += len(raw)
                if total > cap:
                    raise rb_mirror.ResourceRefused("大きすぎます")
                chunks.append(raw)
                if part.get("eof") is True or not piece:
                    break
        except BrowserError:
            return None
        finally:
            try:
                self._call("IO.close", {"handle": handle}, session=tab.session, timeout=PROBE_CDP_TIMEOUT)
            except BrowserError:
                pass
        if isinstance(status, int) and not 200 <= status < 300:
            return None
        return b"".join(chunks), content_type

    def _mirror_fetch(self, tab, url, kind):
        cap = self._mirror_cap(kind)
        for source in (lambda: self._mirror_from_tree(tab, url, kind, cap),
                       lambda: self._mirror_from_page(tab, url, cap),
                       lambda: self._mirror_from_network(tab, url, cap)):
            got = source()
            if got is not None and got[0]:
                return got
        raise ImageUnavailable("リソースを取得できません")

    def _setting(self, key, default):
        """A boolean option of the store, `default` when there is no store (unit tests) or it fails."""
        if self.rules is None:
            return default
        try:
            value = self.rules.settings().get(key)
        except Exception:
            return default
        return value if isinstance(value, bool) else default

    def _mirror_prefetch(self, tab, capture):
        """v0.1.54: start fetching the first images (document order) and fonts of a fresh capture now, up to
        MIRROR_FETCH_PARALLEL at a time, so most of them are in the Hub's RAM when the phone asks."""
        if not tab.mirror_on:
            return
        tab.mirror_prefetch = capture["rev"]
        images, fonts = [], []
        for token, kind in tab.mirror_tokens.ordered():
            if self._mirror_res.get(tab.id, token) is not None:
                continue
            if kind == "font":
                fonts.append(token)
            elif len(images) < MIRROR_PREFETCH_IMAGES:
                images.append(token)
        queue = images + fonts[:MIRROR_PREFETCH_FONTS]
        if not queue:
            return
        lock = threading.Lock()

        def worker():
            while True:
                with lock:
                    if not queue:
                        return
                    token = queue.pop(0)
                if self._tabs.get(tab.id) is not tab or tab.mirror_tokens.get(token) is None:
                    return
                try:
                    self.mirror_resource(tab.id, token)
                except (BrowserError, ValueError):
                    pass
                except Exception:
                    pass
        for _ in range(min(MIRROR_FETCH_PARALLEL, len(queue))):
            self._spawn(worker)

    def mirror_resource_etag(self, ident, token):
        """Content hash (the ETag) of a mirror resource the Hub already holds, else None. Never fetches."""
        if not isinstance(token, str) or not MIRROR_TOKEN.match(token):
            return None
        try:
            tab = self._tab(ident)
        except BrowserError:
            return None
        if tab.mirror_tokens.get(token) is None:
            return None
        return self._mirror_res.known_hash(token)

    def mirror_resource(self, ident, token):
        """(bytes, mime type) of a mirror resource, read through the NAS browser and cached in RAM."""
        tab = self._tab(ident)
        if not isinstance(token, str) or not MIRROR_TOKEN.match(token):
            raise MirrorGone("このリソースは見つかりません")
        entry = tab.mirror_tokens.get(token)
        if entry is None:
            raise MirrorGone("このリソースは見つかりません。読み込み直してください")
        hit = self._mirror_res.get(tab.id, token)
        if hit is not None:
            return hit
        self._touch()
        kind = entry["kind"]
        started = time.monotonic()
        try:
            if entry.get("data") is not None:
                data, mime = rb_mirror.check_resource(kind, entry["data"])
                self._mirror_res.put(tab.id, token, data, mime)
                return data, mime
            with self._lock:
                event = tab.mirror_flight.get(token)
                leader = event is None
                if leader:
                    event = tab.mirror_flight[token] = threading.Event()
            if not leader:
                event.wait(IMAGE_CDP_TIMEOUT * 3)
                hit = self._mirror_res.get(tab.id, token)
                if hit is None:
                    raise ImageUnavailable("リソースを取得できません")
                return hit
            try:
                with tab.mirror_sem:
                    raw, content_type = self._mirror_fetch(tab, entry["url"], kind)
                data, mime = rb_mirror.check_resource(kind, raw, content_type)
                if tab.mirror_tokens.get(token) is entry:      # not cleared by a navigation meanwhile
                    self._mirror_res.put(tab.id, token, data, mime)
            finally:
                with self._lock:
                    tab.mirror_flight.pop(token, None)
                event.set()
        except rb_mirror.ResourceRefused as error:
            debuglog.log("render", "debug", "mirror resource refused", {"tab": tab.id, "kind": kind})
            raise ImageRefused(str(error)) from None
        debuglog.log("render", "debug", "mirror resource", {
            "tab": tab.id, "kind": kind, "kb": len(data) // 1024, "ms": int((time.monotonic() - started) * 1000)})
        return data, mime

    # ---- mirror click
    def mirror_click(self, ident, m, fx=0.5, fy=0.5):
        """A tap on element `m` of the capture: links answer {href} (the caller navigates), anything
        else is clicked at its position in the NAS page (a real mouse click, like a tap)."""
        if isinstance(m, bool) or not isinstance(m, int) or not 0 <= m <= 999999999:
            raise BrowserError("要素の指定が不正です")
        for value in (fx, fy):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise BrowserError("位置の指定が不正です")
        tab = self._tab(ident)
        self._touch()
        started = time.monotonic()
        value = self._mirror_eval(tab, rb_mirror.click_script(m, fx, fy), timeout=PROBE_CDP_TIMEOUT)
        if not isinstance(value, dict):
            raise BrowserError("クリックできませんでした")
        if value.get("err"):
            raise MirrorGone("この要素は見つかりません。再取得します")
        link = value.get("link")
        if link is not None:
            href = rb_mirror.public_http_url(link) if isinstance(link, str) else None
            if href is None:
                raise BrowserError("このリンクは開けません")
            debuglog.log("render", "info", "mirror click", {"tab": tab.id, "m": m, "kind": "link"})
            return {"ok": True, "navigated": False, "href": href}
        x, y = value.get("x"), value.get("y")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or v != v for v in (x, y)):
            raise BrowserError("クリックできませんでした")
        x = self._number(x, 0, MAX_COORD, "x")
        y = self._number(y, 0, MAX_COORD, "y")
        session = tab.session
        with self._lock:
            before_url, before_rev = tab.url, tab.rev
            tab.snap_gen += 1
        base = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self._call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, session)
        self._call("Input.dispatchMouseEvent", dict(base, type="mousePressed", buttons=1), session)
        self._call("Input.dispatchMouseEvent", dict(base, type="mouseReleased", buttons=0), session)
        self._refresh_editable(tab)
        started_nav = self._settle(tab, before_rev, wait_load=False)
        with self._lock:
            navigated = started_nav or tab.url != before_url
        debuglog.log("render", "info", "mirror click", {
            "tab": tab.id, "m": m, "kind": "element", "navigated": navigated,
            "ms": int((time.monotonic() - started) * 1000)})
        return {"ok": True, "navigated": navigated, "editable": tab.editable}

    # ------------------------------------------------------------ intercepts
    @staticmethod
    def _public_event(event):
        item = {"id": event["id"], "kind": event["kind"], "url": event["url"]}
        item.update({k: event[k] for k in EVENT_EXTRAS if k in event})
        return item

    def intercepts(self, ident, since=0):
        tab = self._tab(ident)
        self._touch()
        with self._lock:
            return [self._public_event(e) for e in tab.intercepts if e["id"] > since]

    def intercept(self, ident, event_id):
        tab = self._tab(ident)
        if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
            raise BrowserError("検出リンクの指定が不正です")
        self._touch()
        with self._lock:
            for e in tab.intercepts:
                if e["id"] == event_id:
                    return self._public_event(e)
        raise BrowserError("検出リンクは見つかりません（古いため消えました）")

    def open_intercept(self, ident, event_id):
        """Show an intercepted hoster page: that exact URL passes the Fetch pattern once."""
        item = self.intercept(ident, event_id)
        if item["kind"] not in ("hoster", "popup", "popup_opened"):
            raise BrowserError("ページとして開けるのはホスターとポップアップのリンクだけです")
        checked = browser_safety.validate(item["url"])
        tab = self._tab(ident)
        tab.snap_gen += 1
        with self._lock:
            if item["kind"] == "hoster":      # only hoster pages are paused by Fetch
                tab.bypass[checked] = self._clock() + BYPASS_TTL
            tab.blocked = None
        try:
            self._call("Page.navigate", {"url": checked}, session=tab.session)
        except BrowserError:
            with self._lock:
                tab.bypass.pop(checked, None)
            raise
        return {"id": tab.id, "url": checked}

    # ------------------------------------------------------------ link probe
    # A long press on a link follows it in a hidden background target (never
    # screencast, same browser context, closed in every case) and reports the
    # hoster links found on the way: Fetch-intercepted hoster pages, redirect
    # hops, popups the page opens, and anchors on the final page.
    def _probe_add(self, probe, url):
        with self._cv:
            if url not in probe.links and len(probe.links) < PROBE_LINK_CAP:
                probe.links.append(url)
            probe.activity = self._clock()
            self._cv.notify_all()

    def _probe_event(self, probe, method, params):
        with self._cv:
            probe.activity = self._clock()
            frame_id = params.get("frameId")
            if method == "Page.frameStartedLoading" and isinstance(frame_id, str):
                probe.started = True
                probe.loaded = False
                probe.loading.add(frame_id)
            elif method == "Page.frameStoppedLoading" and isinstance(frame_id, str):
                probe.loading.discard(frame_id)
            elif method == "Page.loadEventFired":
                probe.loaded = True
            elif method == "Network.requestWillBeSent" and params.get("type") == "Document":
                request = params.get("request") if isinstance(params.get("request"), dict) else {}
                url = request.get("url")
                if isinstance(url, str) and len(url) <= 4096:
                    probe.last_doc = url
                    if params.get("redirectResponse"):
                        probe.redirects += 1
                    normalized = rapidgator_extract.normalize(url)
                    if (normalized is not None and normalized not in probe.links and
                            len(probe.links) < PROBE_LINK_CAP):
                        probe.links.append(normalized)
            self._cv.notify_all()

    def _probe_paused(self, probe, request_id, url, resource_type, conn):
        normalized = rapidgator_extract.normalize(url) if len(url) <= 4096 else None
        try:
            if resource_type != "Document" or normalized is None or conn is None:
                if conn is not None:
                    conn.send_nowait("Fetch.continueRequest", {"requestId": request_id},
                                     session=probe.session)
                return
            conn.send_nowait("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"},
                             session=probe.session)
        except CDPError:
            return
        self._probe_add(probe, normalized)

    def _probe_target_event(self, method, info, conn):
        """True when this target is a popup opened (directly or not) by a probe page."""
        target = info["targetId"]
        with self._cv:
            probe = self._probe_popups.get(target)
            if probe is None and method == "Target.targetCreated":
                opener = info.get("openerId")
                probe = self._probe_targets.get(opener) or self._probe_popups.get(opener)
                if probe is not None:
                    self._probe_popups[target] = probe
                    probe.popups.add(target)
        if probe is None:
            return False
        url = info.get("url") if isinstance(info.get("url"), str) else ""
        if self._real_url(url):
            normalized = rapidgator_extract.normalize(url) if len(url) <= 4096 else None
            if normalized is not None:
                self._probe_add(probe, normalized)
        # Never closed here: the mapping must survive so popups of popups stay
        # attributed to the probe; _probe_close closes every one of them.
        return True

    def _wait_probe(self, probe, deadline):
        with self._cv:
            while True:
                if probe.links:
                    return "hoster"
                if self._state != "running":
                    return "lost"
                now = self._clock()
                if probe.loaded and not probe.loading and now - probe.activity >= PROBE_SETTLE:
                    return "settled"
                remaining = deadline - now
                if remaining <= 0:
                    return "timeout"
                self._cv.wait(min(remaining, 0.25))

    def _probe_read_popups(self, probe):
        """Collect hoster anchors of still-open probe popups (best effort, read only)."""
        with self._lock:
            targets = list(probe.popups)
        for target in targets:
            if len(probe.links) >= PROBE_LINK_CAP:
                return
            try:
                session = self._call("Target.attachToTarget", {"targetId": target, "flatten": True},
                                     timeout=PROBE_CDP_TIMEOUT).get("sessionId")
                if not isinstance(session, str) or not session:
                    continue
                hrefs = (self._call("Runtime.evaluate", {
                    "expression": LINKS_JS, "returnByValue": True, "timeout": 3000},
                    session=session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
                for url in self.extract_links(hrefs[:LINK_CAP] if isinstance(hrefs, list) else []):
                    self._probe_add(probe, url)
            except BrowserError as error:
                self.log("probe popup read failed: %s" % error)

    def _probe_close(self, probe):
        """Close the probe target and every popup it spawned; verify nothing is left."""
        with self._cv:
            ids = set(probe.popups)
            if probe.target:
                ids.add(probe.target)
        for target in sorted(ids):
            try:
                self._call("Target.closeTarget", {"targetId": target}, timeout=5.0)
            except BrowserError as error:
                self.log("probe target close failed: %s" % error)
        # Explicit check: anything of ours (or opened by it, transitively) that is still listed.
        try:
            infos = self._call("Target.getTargets", timeout=5.0).get("targetInfos", [])
        except BrowserError as error:
            self.log("probe leftover check failed: %s" % error)
            infos = []
        with self._cv:
            known = set(ids) | set(self._probe_popups_of(probe))
        leftovers = []
        changed = True
        while changed:
            changed = False
            for info in infos:
                if not isinstance(info, dict):
                    continue
                tid = info.get("targetId")
                if (isinstance(tid, str) and tid not in leftovers and
                        (tid in known or info.get("openerId") in known)):
                    known.add(tid)
                    leftovers.append(tid)
                    changed = True
        for target in leftovers:
            try:
                self._call("Target.closeTarget", {"targetId": target}, timeout=5.0)
            except BrowserError as error:
                self.log("probe leftover close failed: %s" % error)
        with self._cv:
            self._probe_targets.pop(probe.target, None)
            self._probe_sessions.pop(probe.session, None)
            for target in list(self._probe_popups_of(probe)):
                self._probe_popups.pop(target, None)
            probe.popups.clear()

    def _probe_popups_of(self, probe):
        return [t for t, p in self._probe_popups.items() if p is probe]

    def _probe_slot_free(self, ticket):
        """FIFO turn (caller holds the lock): the first queued probe whose tab has room."""
        if self._probes >= MAX_PROBES:
            return False
        for other in self._probe_queue:
            if other["tab"].probing < MAX_PROBES_PER_TAB:
                return other is ticket
        return False

    def _probe_acquire(self, tab):
        """Reserve a probe slot, waiting FIFO behind the per-tab and overall limits.

        Then the memory guard: while it is not met the probe keeps its slot and waits up
        to PROBE_QUEUE_WAIT, after which it fails with the guard's message.
        """
        ticket = {"tab": tab}
        with self._cv:
            if tab.probe_waiting >= PROBE_QUEUE_PER_TAB:
                raise ProbeQueueFull("確認待ちが多すぎます")
            tab.probe_waiting += 1
            self._probe_queue.append(ticket)
            try:
                while not self._probe_slot_free(ticket):
                    if self._state != "running" or self._tabs.get(tab.id) is not tab:
                        raise TabGone("タブが閉じられたため確認を中止しました")
                    self._cv.wait(0.25)
            except BaseException:
                self._probe_queue.remove(ticket)
                tab.probe_waiting -= 1
                self._cv.notify_all()
                raise
            self._probe_queue.remove(ticket)
            tab.probe_waiting -= 1
            tab.probing += 1
            self._probes += 1
            self._cv.notify_all()
        try:
            deadline = self._clock() + PROBE_QUEUE_WAIT
            while True:
                reason = self.probe_memory_reason()
                if not reason:
                    return
                if self._clock() >= deadline:
                    raise BrowserError(reason)
                with self._cv:
                    if self._state != "running" or self._tabs.get(tab.id) is not tab:
                        raise TabGone("タブが閉じられたため確認を中止しました")
                    self._cv.wait(0.5)
        except BaseException:
            self._probe_release(tab)
            raise

    def _probe_release(self, tab):
        with self._cv:
            tab.probing -= 1
            self._probes -= 1
            self._cv.notify_all()

    def probe(self, ident, x, y, url_check=None, href=None, snap=None):
        """Follow the link under (x, y) in a hidden target; see the section comment.

        `url_check(url)` may raise ValueError to refuse a start URL (the Hub's own
        origin). Several probes run at once (per tab and overall limits, the rest wait
        in a FIFO queue); each has its own hidden target. Returns
        {start_url, final_url, links, redirects}.
        """
        tab = self._tab(ident)
        self._touch()
        if href is not None:
            # Fast display: the phone already knows the link under the finger (from the
            # snapshot's link map). It is text from a remote page, so it gets exactly the
            # same validation as any address before the hidden page opens it.
            if not isinstance(href, str) or not href or len(href) > SNAP_URL_MAX:
                raise BrowserError("リンク先の指定が不正です")
        else:
            x = self._number(x, 0, MAX_COORD, "x")
            y = self._number(y, 0, MAX_COORD, "y")
            if snap is not None:
                # Fast display: (x, y) are page coordinates of that snapshot.
                x, y = self._viewport_point(tab, snap, x, y)
            # The link under (or, within LINK_NEAR_PX, next to) the finger is resolved right away,
            # before any queueing.
            result = self._call("Runtime.evaluate", {
                "expression": PROBE_JS % (x, y, LINK_NEAR_PX), "returnByValue": True, "timeout": 5000},
                session=tab.session, timeout=PROBE_CDP_TIMEOUT)
            href = (result.get("result") or {}).get("value")
            if not isinstance(href, str) or not href:
                raise BrowserError("この位置にリンクが見つかりません")
        try:
            start = browser_safety.validate(href)
            if url_check is not None:
                url_check(start)
        except ValueError:
            raise BrowserError("リンク先は公開されたhttpsのアドレスだけ確認できます") from None
        direct = rapidgator_extract.normalize(start)
        if direct is not None:
            return {"start_url": start, "final_url": start, "links": [direct], "redirects": 0}
        self._probe_acquire(tab)
        try:
            return self._run_probe(tab, start)
        finally:
            self._probe_release(tab)

    def _run_probe(self, tab, start):
        probe = Probe(tab.id, self._clock())
        try:
            created = self._call("Target.createTarget", {"url": "about:blank", "background": True})
            target = created.get("targetId")
            if not isinstance(target, str) or not TARGET_ID.match(target):
                raise BrowserError("リンク先の確認用ページを作成できません")
            with self._lock:
                probe.target = target
                self._probe_targets[target] = probe
            session = self._call("Target.attachToTarget",
                                 {"targetId": target, "flatten": True}).get("sessionId")
            if not isinstance(session, str) or not session:
                raise BrowserError("リンク先の確認用ページに接続できません")
            with self._lock:
                probe.session = session
                self._probe_sessions[session] = probe
            self._call("Page.enable", session=session)
            self._call("Fetch.enable", {"patterns": [dict(p) for p in FETCH_PATTERNS]},
                       session=session)
            if self._ads_wanted(tab.page_host):
                try:
                    for method, params in self._ads_commands(True):
                        self._call(method, params, session=session)
                except BrowserError as error:
                    self.log("probe ad blocking failed: %s" % error)
            self._call("Page.navigate", {"url": start}, session=session)
            self._wait_probe(probe, self._clock() + PROBE_TIMEOUT)
            if not probe.links:
                self._probe_read_popups(probe)
            final = None
            if not probe.links:
                # No hoster URL on the way: read the anchors of the page we ended on.
                try:
                    value = (self._call("Runtime.evaluate", {
                        "expression": LOCATION_JS, "returnByValue": True, "timeout": 3000},
                        session=session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
                    final = value if isinstance(value, str) else None
                    hrefs = (self._call("Runtime.evaluate", {
                        "expression": LINKS_JS, "returnByValue": True, "timeout": 5000},
                        session=session, timeout=PROBE_CDP_TIMEOUT).get("result") or {}).get("value")
                    for url in self.extract_links(hrefs[:LINK_CAP] if isinstance(hrefs, list) else []):
                        self._probe_add(probe, url)
                except BrowserError as error:
                    self.log("probe page read failed: %s" % error)
            with self._lock:
                links = list(probe.links)[:PROBE_LINK_CAP]
                last_doc, redirects = probe.last_doc, probe.redirects
            final_url = start
            for candidate in (final, last_doc, links[0] if links else None):
                if not isinstance(candidate, str) or candidate == "about:blank":
                    continue
                try:
                    final_url = browser_safety.validate(candidate)
                    break
                except ValueError:
                    continue
            return {"start_url": start, "final_url": final_url, "links": links,
                    "redirects": redirects}
        finally:
            self._probe_close(probe)
