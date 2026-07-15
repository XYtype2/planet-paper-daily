#!/usr/bin/env python3
"""每日文献推荐:抓取 arXiv + Crossref,按分类筛选,生成中文摘要,输出静态网页。

在 GitHub Actions 上定时运行;也可本地运行:
    ANTHROPIC_API_KEY=sk-... python scripts/build.py
无 API key 时降级为显示英文原摘要。
"""
import html
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
HISTORY_FILE = ROOT / "data" / "history.json"
DOCS = ROOT / "docs"
UA = {"User-Agent": "paper-daily/1.0 (personal literature feed)"}

CST = timezone(timedelta(hours=8))  # 北京时间
TODAY = datetime.now(CST).strftime("%Y-%m-%d")
CAT_ORDER = [c["name"] for c in CONFIG["categories"]]


def http_get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  retry {i+1} {url[:80]}: {e}", file=sys.stderr)
            time.sleep(3 * (i + 1))
    return None


def _norm(s):
    return re.sub(r"[\s\-–—]+", " ", s.lower())


def classify(title, abstract):
    """返回 (分类名, 命中关键词列表);被排除或未命中返回 (None, [])。"""
    t_title = _norm(title)
    for ex in CONFIG.get("exclude_title") or []:
        if _norm(ex) in t_title:
            return None, []
    text = _norm(title + " " + abstract)
    req = CONFIG.get("require_any") or []
    if req and not any(_norm(w) in text for w in req):
        return None, []
    best, best_hits = None, []
    for cat in CONFIG["categories"]:
        hits = [kw for kw in cat["keywords"] if _norm(kw) in text]
        if len(hits) > len(best_hits):
            best, best_hits = cat["name"], hits
    return best, best_hits


def clean_text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# ---------------- arXiv ----------------

def fetch_arxiv():
    papers = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG["arxiv"]["days_back"])
    for cat in CONFIG["arxiv"]["categories"]:
        q = urllib.parse.urlencode({
            "search_query": f"cat:{cat}",
            "sortBy": "submittedDate", "sortOrder": "descending",
            "max_results": CONFIG["arxiv"]["max_fetch"],
        })
        raw = http_get(f"https://export.arxiv.org/api/query?{q}")
        if not raw:
            continue
        for e in ET.fromstring(raw).findall("a:entry", ns):
            title = clean_text(e.findtext("a:title", "", ns))
            abstract = clean_text(e.findtext("a:summary", "", ns))
            published = e.findtext("a:published", "", ns)
            link = e.findtext("a:id", "", ns).replace("http://", "https://")
            authors = [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)]
            try:
                pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                continue
            if pub_dt < cutoff:
                continue
            category, hits = classify(title, abstract)
            if not category:
                continue
            arxiv_id = link.rsplit("/", 1)[-1]
            papers.append({
                "id": f"arxiv:{re.sub(r'v[0-9]+$', '', arxiv_id)}",
                "title": title, "abstract": abstract,
                "authors": authors, "link": link,
                "source": f"arXiv ({cat})",
                "category": category, "matched": hits,
            })
        time.sleep(1)
    return papers


# ---------------- Crossref (期刊) ----------------

def fetch_journals():
    papers = []
    since = (datetime.now(timezone.utc)
             - timedelta(days=CONFIG["journals_days_back"])).strftime("%Y-%m-%d")
    for j in CONFIG.get("journals") or []:
        q = urllib.parse.urlencode({
            "filter": f"from-pub-date:{since},type:journal-article",
            "rows": 150, "sort": "published", "order": "desc",
            "select": "title,DOI,abstract,author,container-title,URL",
        })
        raw = http_get(f"https://api.crossref.org/journals/{j['issn']}/works?{q}")
        if not raw:
            continue
        try:
            items = json.loads(raw)["message"]["items"]
        except (KeyError, json.JSONDecodeError):
            continue
        for it in items:
            title = clean_text(" ".join(it.get("title") or []))
            abstract = clean_text(it.get("abstract") or "")
            if not title:
                continue
            category, hits = classify(title, abstract)
            if not category:
                continue
            authors = [" ".join(filter(None, [a.get("given"), a.get("family")]))
                       for a in (it.get("author") or [])]
            papers.append({
                "id": f"doi:{it['DOI'].lower()}",
                "title": title, "abstract": abstract,
                "authors": authors,
                "link": f"https://doi.org/{it['DOI']}",
                "source": j["name"],
                "category": category, "matched": hits,
            })
        time.sleep(1)
    return papers


# ---------------- 去重 ----------------

def norm_title(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def dedupe(papers, seen_ids, seen_titles):
    out, titles = [], set()
    for p in papers:
        nt = norm_title(p["title"])
        if p["id"] in seen_ids or nt in seen_titles or nt in titles:
            continue
        titles.add(nt)
        out.append(p)
    return out


# ---------------- 中文摘要 (Claude API) ----------------

def summarize(papers):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("! 未设置 ANTHROPIC_API_KEY,将显示英文原摘要", file=sys.stderr)
        for p in papers:
            p["summary_zh"] = ""
        return
    for p in papers:
        prompt = (f"请用{CONFIG['summary']['style']}。只输出摘要正文,不要前缀。\n\n"
                  f"标题: {p['title']}\n\n英文摘要: {p['abstract'][:4000]}")
        body = json.dumps({
            "model": CONFIG["summary"]["model"], "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                p["summary_zh"] = json.loads(r.read())["content"][0]["text"].strip()
        except Exception as e:
            print(f"  摘要失败 {p['id']}: {e}", file=sys.stderr)
            p["summary_zh"] = ""
        time.sleep(0.5)


# ---------------- 网页生成:19 世纪天文教学挂图风格 ----------------

INK = "#332e26"       # 墨线色
DARK = "#26231d"      # 黑灰版面底色
WHT = "#f1ead6"       # 版面上的白线色

# ============================================================
# ★ 可替换插图区(共 5 幅,整段替换对应变量即可,页面其余部分不受影响)
#   1. EMBLEM    — 页首横幅:黑灰底白线石版画(月面 + 彗星 + 星点)
#                  月亮按当天真实月相绘制(<!--MOON--> 处自动填入阴影)
#   2. STAR      — 日期分隔线两侧的小星
#   3. PLANET    — 分类图标兜底(小土星);CAT_ICONS 里未定义的分类用它
#      CAT_ICONS — 各分类专属小图标(键名 = config.yaml 的分类名)
#   4. LOGO      — 页面底部圆形小徽标(黑底白线,与页首同风格)
#   5. EMPTY_ART — 无文献日的小插画(望远镜与星空)
# ============================================================

EMBLEM = f"""<svg viewBox="0 0 680 150" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="页首饰图">
<rect width="680" height="150" fill="{DARK}"/>
<rect x=".8" y=".8" width="678.4" height="148.4" fill="none" stroke="{INK}" stroke-width="1.6"/>

<!-- 星点与十字小星 -->
<g fill="{WHT}">
<circle cx="42" cy="30" r="1.3"/><circle cx="95" cy="98" r="1"/><circle cx="150" cy="22" r="1.1"/>
<circle cx="235" cy="45" r="1.4"/><circle cx="300" cy="18" r="1"/><circle cx="330" cy="120" r="1.2"/>
<circle cx="600" cy="25" r="1.2"/><circle cx="640" cy="90" r="1"/><circle cx="380" cy="70" r="1"/>
<circle cx="65" cy="128" r="1"/>
</g>
<g stroke="{WHT}" stroke-width=".7">
<line x1="262" y1="95" x2="262" y2="103"/><line x1="258" y1="99" x2="266" y2="99"/>
<line x1="118" y1="55" x2="118" y2="62"/><line x1="114.5" y1="58.5" x2="121.5" y2="58.5"/>
<line x1="623" y1="118" x2="623" y2="124"/><line x1="620" y1="121" x2="626" y2="121"/>
</g>

<!-- 彗星:白色头部 + 三线尾迹 -->
<g stroke="{WHT}" fill="none">
<path d="M186,58 Q120,34 52,26" stroke-width="1" opacity=".85"/>
<path d="M188,64 Q122,48 56,44" stroke-width=".8" opacity=".6"/>
<path d="M183,53 Q126,22 66,12" stroke-width=".7" opacity=".45"/>
</g>
<circle cx="188" cy="59" r="5" fill="{WHT}"/>
<circle cx="188" cy="59" r="7.5" fill="none" stroke="{WHT}" stroke-width=".7" opacity=".5"/>

<!-- 虚线轨道弧与小行星 -->
<path d="M20,128 Q210,92 405,118" fill="none" stroke="{WHT}" stroke-width=".7"
 stroke-dasharray="3 3.5" opacity=".55"/>
<circle cx="340" cy="103" r="2.6" fill="{WHT}"/>

<!-- 月面:白线刻画,月海灰面 + 环形山 + 辐射纹 -->
<g transform="translate(505,75)">
<circle r="56" fill="#2e2b24" stroke="{WHT}" stroke-width="1.3"/>
<g fill="#4b463c" opacity=".85">
<path d="M-30,-28 Q-8,-40 8,-28 Q18,-16 4,-8 Q-14,-2 -28,-10 Q-38,-18 -30,-28 Z"/>
<path d="M14,-4 Q30,-10 38,2 Q40,14 26,16 Q12,14 12,6 Q11,0 14,-4 Z"/>
<path d="M-34,12 Q-22,8 -18,18 Q-20,28 -32,26 Q-40,20 -34,12 Z"/>
</g>
<g stroke="{WHT}" fill="none" stroke-width=".8">
<circle cx="-22" cy="-22" r="6"/><circle cx="12" cy="-33" r="4"/>
<circle cx="34" cy="-12" r="6.5"/><circle cx="-38" cy="0" r="4.5"/>
<circle cx="-8" cy="8" r="7.5"/><circle cx="28" cy="26" r="4"/>
<circle cx="-24" cy="34" r="3"/><circle cx="44" cy="10" r="2.6"/>
<circle cx="2" cy="-14" r="2.4"/><circle cx="-45" cy="-14" r="2.2"/>
</g>
<g stroke="{WHT}" fill="none" stroke-width=".5" opacity=".6">
<path d="M-25,-19 A6,6 0 0 0 -17,-21"/><path d="M-11,11 A7.5,7.5 0 0 0 -2,12"/>
<path d="M31,-9 A6.5,6.5 0 0 0 39,-11"/>
</g>
<!-- 辐射纹(第谷式) -->
<g stroke="{WHT}" stroke-width=".55" opacity=".65">
<line x1="-6" y1="42" x2="-2" y2="53"/><line x1="-14" y1="40" x2="-22" y2="50"/>
<line x1="2" y1="40" x2="10" y2="50"/><line x1="-10" y1="43" x2="-12" y2="54"/>
<line x1="8" y1="37" x2="18" y2="44"/>
</g>
<circle cx="-4" cy="37" r="4" fill="none" stroke="{WHT}" stroke-width=".8"/>
<!--MOON-->
</g>
</svg>"""

EMPTY_ART = f"""<svg viewBox="0 0 340 120" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="今日无文献">
<rect width="340" height="120" fill="{DARK}"/>
<rect x=".7" y=".7" width="338.6" height="118.6" fill="none" stroke="{INK}" stroke-width="1.4"/>
<g fill="{WHT}">
<circle cx="40" cy="26" r="1.2"/><circle cx="90" cy="14" r="1"/><circle cx="150" cy="30" r="1.1"/>
<circle cx="210" cy="16" r="1"/><circle cx="305" cy="52" r="1"/><circle cx="60" cy="58" r="1"/>
<circle cx="288" cy="20" r="1.4"/>
</g>
<g stroke="{WHT}" stroke-width=".7">
<line x1="255" y1="30" x2="255" y2="38"/><line x1="251" y1="34" x2="259" y2="34"/>
</g>
<path d="M115,102 Q170,94 225,102" fill="none" stroke="{WHT}" stroke-width="1"/>
<g stroke="{WHT}" stroke-width=".6" stroke-dasharray="2.5 3" fill="none">
<line x1="207" y1="52" x2="284" y2="23"/>
</g>
<g transform="translate(170,72)">
<g transform="rotate(-21)">
<rect x="-34" y="-5.5" width="66" height="11" rx="2" fill="{DARK}" stroke="{WHT}" stroke-width="1.1"/>
<line x1="18" y1="-5.5" x2="18" y2="5.5" stroke="{WHT}" stroke-width=".7"/>
<rect x="-42" y="-3.4" width="8" height="6.8" fill="{DARK}" stroke="{WHT}" stroke-width="1"/>
</g>
<circle cx="0" cy="9" r="2.4" fill="none" stroke="{WHT}" stroke-width=".9"/>
<g stroke="{WHT}" stroke-width="1" fill="none">
<line x1="0" y1="11" x2="-20" y2="34"/><line x1="0" y1="11" x2="19" y2="34"/>
<line x1="0" y1="11" x2="2" y2="34"/>
</g>
</g>
<g transform="translate(46,88) rotate(18)">
<path d="M0,-7 A7,7 0 1 0 0,7 A5.4,5.4 0 1 1 0,-7 Z" fill="{WHT}"/>
</g>
</svg>"""


def moon_shadow():
    """按当天真实月相生成月面阴影 path(嵌入 EMBLEM 的 <!--MOON--> 处)。"""
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)  # 已知新月
    syn = 29.530588853
    p = ((datetime.now(timezone.utc) - ref).total_seconds() / 86400 % syn) / syn
    if p < 0.02 or p > 0.98:  # 新月:整面阴影
        d = "M0,-56 A56,56 0 0 0 0,56 A56,56 0 0 0 0,-56 Z"
    elif abs(p - 0.5) < 0.02:  # 满月:无阴影
        return ""
    else:
        rx = 56 * abs(math.cos(2 * math.pi * p))
        if p < 0.5:  # 盈月,亮面在右
            s1, s2 = 0, (0 if p < 0.25 else 1)
        else:        # 亏月,亮面在左
            s1, s2 = 1, (0 if p < 0.75 else 1)
        d = f"M0,-56 A56,56 0 0 {s1} 0,56 A{rx:.1f},56 0 0 {s2} 0,-56 Z"
    return f'<path d="{d}" fill="#1c1a14" opacity=".88"/>'

STAR = f"""<svg viewBox="0 0 16 16" width="12" height="12" xmlns="http://www.w3.org/2000/svg">
<path d="M8,1.5 L9.8,5.4 L14,5.4 L10.7,7.9 L11.7,12 L8,9.8 L4.3,12 L5.3,7.9 L2,5.4 L6.2,5.4 Z"
 fill="#f2e3ac" stroke="{INK}" stroke-width="1" stroke-linejoin="round"/></svg>"""

PLANET = f"""<svg viewBox="0 0 28 17" width="24" height="15" xmlns="http://www.w3.org/2000/svg">
<g transform="translate(14,8.5) rotate(-16)">
<ellipse rx="12" ry="4" fill="none" stroke="{INK}" stroke-width="1.1"/>
<circle r="5.2" fill="#c2d6cc" stroke="{INK}" stroke-width="1.1"/>
<ellipse rx="9" ry="2.8" fill="none" stroke="{INK}" stroke-width=".6"/>
</g></svg>"""

# 各分类专属小图标(键名须与 config.yaml 的分类名一致;未列出的分类用上面的 PLANET)
CAT_ICONS = {
    # 带芒恒星 + 贴身行星与短周期轨道
    "热木星与短周期行星": f"""<svg viewBox="0 0 28 18" width="24" height="15" xmlns="http://www.w3.org/2000/svg">
<ellipse cx="10" cy="9" rx="9.5" ry="4.6" fill="none" stroke="{INK}" stroke-width=".7" stroke-dasharray="2 2.2"/>
<g stroke="{INK}" stroke-width="1" stroke-linecap="round">
<line x1="10" y1="3.6" x2="10" y2="1.6"/><line x1="10" y1="14.4" x2="10" y2="16.4"/>
<line x1="4.6" y1="9" x2="2.6" y2="9"/><line x1="15.4" y1="9" x2="17.4" y2="9"/>
<line x1="6.2" y1="5.2" x2="4.8" y2="3.8"/><line x1="13.8" y1="12.8" x2="15.2" y2="14.2"/>
<line x1="6.2" y1="12.8" x2="4.8" y2="14.2"/><line x1="13.8" y1="5.2" x2="15.2" y2="3.8"/>
</g>
<circle cx="10" cy="9" r="3.6" fill="#f2e3ac" stroke="{INK}" stroke-width="1.1"/>
<circle cx="19.5" cy="9" r="2.1" fill="#c2d6cc" stroke="{INK}" stroke-width="1"/>
</svg>""",
    # 两条交叠轨道 + 两颗行星
    "散射与动力学演化": f"""<svg viewBox="0 0 28 18" width="24" height="15" xmlns="http://www.w3.org/2000/svg">
<g fill="none" stroke="{INK}" stroke-width=".9">
<ellipse cx="14" cy="9" rx="11" ry="4.2" transform="rotate(18 14 9)"/>
<ellipse cx="14" cy="9" rx="11" ry="4.2" transform="rotate(-18 14 9)"/>
</g>
<circle cx="14" cy="9" r="1.6" fill="#f2e3ac" stroke="{INK}" stroke-width=".8"/>
<circle cx="23.2" cy="5.8" r="1.9" fill="#c2d6cc" stroke="{INK}" stroke-width="1"/>
<circle cx="4.8" cy="5.8" r="1.6" fill="#c2d6cc" stroke="{INK}" stroke-width="1"/>
</svg>""",
    # 目镜十字分划 + 视场中的星
    "系外行星观测": f"""<svg viewBox="0 0 28 18" width="24" height="15" xmlns="http://www.w3.org/2000/svg">
<circle cx="14" cy="9" r="7" fill="none" stroke="{INK}" stroke-width="1.1"/>
<g stroke="{INK}" stroke-width=".7">
<line x1="14" y1="2" x2="14" y2="5"/><line x1="14" y1="13" x2="14" y2="16"/>
<line x1="7" y1="9" x2="10" y2="9"/><line x1="18" y1="9" x2="21" y2="9"/>
</g>
<circle cx="14" cy="9" r="1.4" fill="{INK}"/>
</svg>""",
    # 侧视吸积盘 + 中心恒星
    "行星形成与盘演化": f"""<svg viewBox="0 0 28 18" width="24" height="15" xmlns="http://www.w3.org/2000/svg">
<ellipse cx="14" cy="9" rx="11" ry="3.4" fill="#c2d6cc" stroke="{INK}" stroke-width="1.1"/>
<ellipse cx="14" cy="9" rx="7" ry="2" fill="none" stroke="{INK}" stroke-width=".55" opacity=".7"/>
<circle cx="14" cy="9" r="2.1" fill="#f2e3ac" stroke="{INK}" stroke-width=".9"/>
</svg>""",
    # 带云带的行星圆面(参考图木星画法)
    "行星大气": f"""<svg viewBox="0 0 28 18" width="24" height="15" xmlns="http://www.w3.org/2000/svg">
<defs><clipPath id="icAtm"><circle cx="14" cy="9" r="6.8"/></clipPath></defs>
<circle cx="14" cy="9" r="6.8" fill="#f2e3ac" stroke="{INK}" stroke-width="1.1"/>
<g clip-path="url(#icAtm)" fill="none" stroke="{INK}" stroke-width=".7">
<path d="M7,6.2 Q11,7.4 14,6.2 Q17,5 21,6.4"/>
<path d="M7,9.4 Q11,10.6 14,9.4 Q17,8.2 21,9.6"/>
<path d="M7,12.4 Q11,13.4 14,12.4 Q17,11.4 21,12.6"/>
</g>
</svg>""",
}

LOGO = f"""<svg viewBox="0 0 48 48" width="42" height="42" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="徽标">
<circle cx="24" cy="24" r="22.5" fill="{DARK}" stroke="{INK}" stroke-width="1.2"/>
<circle cx="24" cy="24" r="18.5" fill="none" stroke="{WHT}" stroke-width=".6" opacity=".7"/>
<path d="M28,11 A13.5,13.5 0 1 0 28,37 A10.5,10.5 0 1 1 28,11 Z" fill="{WHT}"/>
<g stroke="{WHT}" stroke-width=".8">
<line x1="33" y1="15" x2="33" y2="21"/><line x1="30" y1="18" x2="36" y2="18"/>
</g>
<circle cx="35" cy="28" r="1"/>
<circle cx="35" cy="28" r="1" fill="{WHT}"/>
<circle cx="31" cy="33" r=".8" fill="{WHT}"/>
</svg>"""

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#fffcf3">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="manifest.json">
<title>{title}</title>
<style>
:root {{
  --paper:#fffcf3; --panel:#fffcf3; --ink:#332e26; --faint:#867d6a;
  --gold:#f2e3ac; --teal:#c2d6cc; --rust:#a4643a; --rule:#3d3831;
}}
* {{ box-sizing:border-box }}
html {{ background:var(--paper) }}
body {{ margin:0; color:var(--ink); background:var(--paper);
  font:15.5px/1.75 Futura,"Avenir Next","Century Gothic","Trebuchet MS",
    "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  -webkit-font-smoothing:antialiased }}
.sheet {{ max-width:960px; margin:22px auto 34px; padding:0 14px }}
@media (min-width:800px) {{ .frame {{ padding:30px 44px 34px }} }}
.plate-top {{ display:flex; justify-content:space-between; gap:8px;
  font-size:11.5px; color:var(--faint); font-style:italic;
  padding:0 4px 5px; letter-spacing:.5px }}
.plate-bottom {{ text-align:right; font-size:11.5px; color:var(--faint);
  font-style:italic; padding:5px 4px 0; letter-spacing:.5px }}
.frame {{ border:2.4px solid var(--rule); background:var(--panel);
  padding:22px 22px 26px }}
@media (max-width:480px) {{ .frame {{ padding:16px 13px 22px }} }}
header {{ text-align:center; margin-bottom:8px }}
.emblem {{ margin:0 0 16px }}
.emblem svg {{ width:100%; height:auto; display:block }}
h1 {{ font-size:30px; font-weight:700; letter-spacing:14px; text-indent:14px;
  margin:6px 0 4px; font-family:"PingFang SC","Hiragino Sans GB","Helvetica Neue",
  "Microsoft YaHei",sans-serif }}
.subtitle {{ font-size:11px; letter-spacing:3.5px; color:var(--faint);
  text-transform:uppercase; margin-bottom:2px }}
.date {{ font-size:12.5px; color:var(--faint) }}
.hr {{ display:flex; align-items:center; gap:10px; margin:30px 0 4px }}
.hr .line {{ flex:1; border-top:1px solid var(--rule) }}
.hr .label {{ font-size:14.5px; letter-spacing:2px; white-space:nowrap;
  display:flex; align-items:center; gap:8px }}
.cat {{ display:flex; align-items:baseline; gap:8px; margin:20px 0 10px;
  font-size:14.5px; letter-spacing:1.5px }}
.cat svg {{ transform:translateY(2.5px) }}
.cat .n {{ font-size:11.5px; color:var(--faint); font-style:italic }}
.card {{ position:relative; background:var(--panel);
  border:1.1px solid var(--rule); padding:14px 16px 13px; margin-bottom:13px }}
.card h2 {{ font-size:15.5px; font-weight:600; margin:0 30px 5px 0; line-height:1.55 }}
.card h2 a {{ color:var(--ink); text-decoration:none }}
.card h2 a:hover {{ color:var(--rust) }}
.card.major h2 a {{ color:#a2352a }}
.card.major h2 a:hover {{ color:#7c2419 }}
.meta {{ font-size:12px; color:var(--faint); margin-bottom:6px; font-style:italic }}
.kwbar {{ margin-top:8px; font-size:11.5px; letter-spacing:.5px;
  color:var(--faint); font-style:italic }}
.kwbar::before {{ content:""; display:inline-block; width:8px; height:8px;
  background:#b6cdd1; border:.8px solid var(--rule); margin-right:7px }}
.zh {{ font-size:14.5px; text-align:justify; color:#111 }}
.en {{ font-size:13px; color:var(--faint); margin-top:6px; text-align:justify }}
details summary {{ cursor:pointer; color:var(--rust); font-size:12.5px; margin-top:7px;
  list-style:none }}
details summary::-webkit-details-marker {{ display:none }}
details summary::before {{ content:"✶ " }}
details[open] summary::before {{ content:"★ " }}
.empty {{ text-align:center; padding:14px 0 6px }}
.empty svg {{ width:min(320px,82%); height:auto }}
.empty-t {{ color:var(--faint); font-size:12px; font-style:italic; margin-top:7px }}
.logo {{ text-align:center; margin-top:30px }}
</style></head><body>
<div class="sheet">
<div class="plate-top"><span>The universe writes; we read.</span><span>Plan {plan_no}. {latest_day}.</span></div>
<div class="frame">
<div class="emblem">{emblem}</div>
<header>
<div class="subtitle">Ephemerides Litterarum</div>
<h1>{title}</h1>
<div class="date">更新于 {updated}(北京时间)</div>
</header>
{body}
<div class="logo">{logo}</div>
</div>
<div class="plate-bottom"><span>Sic itur ad astra.</span></div>
</div>
</body></html>"""


def roman(n):
    vals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def render_paper(p, no):
    au = ", ".join(p["authors"][:4]) + (" et al." if len(p["authors"]) > 4 else "")
    kws = " · ".join(html.escape(k) for k in p["matched"])
    zh = (f'<div class="zh">{html.escape(p["summary_zh"])}</div>' if p.get("summary_zh")
          else f'<div class="en">{html.escape(p["abstract"][:600])}</div>')
    en = (f'<details><summary>英文原摘要</summary><div class="en">'
          f'{html.escape(p["abstract"])}</div></details>'
          if p.get("summary_zh") and p["abstract"] else "")
    major = " major" if p["source"] in (CONFIG.get("highlight_sources") or []) else ""
    return (f'<div class="card{major}">'
            f'<h2><a href="{html.escape(p["link"])}" target="_blank">'
            f'{html.escape(p["title"])}</a></h2>'
            f'<div class="meta">{html.escape(au)} · {html.escape(p["source"])}</div>'
            f'{zh}{en}'
            f'<div class="kwbar">{kws}</div></div>')


def render_day(date, papers):
    head = "今日推荐" if date == TODAY else date
    parts = [f'<div class="hr"><span class="line"></span>'
             f'<span class="label">{STAR} {head} · {len(papers)} 篇 {STAR}</span>'
             f'<span class="line"></span></div>']
    if not papers:
        parts.append(f'<div class="empty">{EMPTY_ART}'
                     f'<div class="empty-t">Caelum silet. · 今日无匹配文献</div></div>')
    else:
        groups = {}
        for p in papers:
            groups.setdefault(p.get("category") or "其他", []).append(p)
        order = [c for c in CAT_ORDER if c in groups] + \
                [c for c in groups if c not in CAT_ORDER]
        for cat in order:
            icon = CAT_ICONS.get(cat, PLANET)
            parts.append(f'<div class="cat">{icon}<span>{html.escape(cat)}</span>'
                         f'<span class="n">{len(groups[cat])} 篇</span></div>')
            parts.extend(render_paper(p, i + 1) for i, p in enumerate(groups[cat]))
    return "".join(parts)


def render(history):
    days = sorted(history.keys(), reverse=True)[: CONFIG["site"]["archive_days"]]
    body = "".join(render_day(d, history[d]) for d in days) \
        or '<div class="empty">暂无数据</div>'
    latest = days[0] if days else TODAY
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(PAGE.format(
        title=CONFIG["site"]["title"],
        emblem=EMBLEM.replace("<!--MOON-->", moon_shadow()), logo=LOGO,
        plan_no=roman(max(1, datetime.now(CST).isocalendar()[1])),
        latest_day=latest,
        updated=datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        body=body), encoding="utf-8")
    (DOCS / "manifest.json").write_text(json.dumps({
        "name": CONFIG["site"]["title"], "short_name": CONFIG["site"]["title"],
        "start_url": ".", "display": "standalone",
        "background_color": "#fffcf3", "theme_color": "#fffcf3",
        "icons": [{"src": "apple-touch-icon.png", "sizes": "180x180",
                   "type": "image/png"}],
    }, ensure_ascii=False), encoding="utf-8")


def main():
    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else {}
    seen_ids = {p["id"] for ps in history.values() for p in ps}
    seen_titles = {norm_title(p["title"]) for ps in history.values() for p in ps}

    print("== arXiv ==")
    papers = fetch_arxiv()
    print(f"  命中 {len(papers)} 篇")
    print("== 期刊 (Crossref) ==")
    jp = fetch_journals()
    print(f"  命中 {len(jp)} 篇")

    new = dedupe(papers + jp, seen_ids, seen_titles)
    print(f"== 去重后新增 {len(new)} 篇 ==")
    summarize(new)

    history[TODAY] = new + history.get(TODAY, [])
    keep = sorted(history.keys(), reverse=True)[: CONFIG["site"]["archive_days"] * 2]
    history = {d: history[d] for d in keep}
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")

    render(history)
    print("== 完成:docs/index.html ==")


if __name__ == "__main__":
    main()
