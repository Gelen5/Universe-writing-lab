"""采集器 —— 把公众号文章拉进系统，并回填数据。

支持四种采集模式（config.collector.mode）：
  mp_api      公众号所有者：用 appid/secret 调微信 MP 官方接口，列出已发布文章 + 读数据立方
  url_feed    把待采集文章 URL 丢进 data/source_urls.json（{url, category?, tags?}），本模式抓取
  local_folder 扫描本地已下载的 .md 文章目录（复用 wechat-account-collector 思路）
  demo        离线演示：生成带 7+ 天数据的样例文章，用于跑通整条流水线

无论哪种模式，最终都 upsert 进 SQLite，并返回"本次新增/更新"的文章列表。
"""
import os
import re
import json
import time
import random
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)


# ───────────────────────── 网络抓取（stdlib，4 级降级） ─────────────────────────
def fetch_html(url: str) -> Optional[str]:
    strategies = [
        ("桌面UA", {"User-Agent": UA_DESKTOP, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}),
        ("移动UA", {"User-Agent": UA_MOBILE, "Accept": "text/html,*/*;q=0.8"}),
    ]
    last_err = None
    for name, headers in strategies:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                # 微信页面多为 utf-8
                try:
                    html = raw.decode("utf-8")
                except Exception:
                    html = raw.decode("utf-8", "ignore")
                if len(html) > 800:
                    logger.info("[fetch] %s 成功 (%d 字符)", name, len(html))
                    return html
        except Exception as e:  # noqa
            last_err = e
            logger.debug("[fetch] %s 失败: %s", name, e)
    logger.error("[fetch] 所有策略失败: %s", last_err)
    return None


def extract_content(html: str) -> Dict[str, str]:
    result = {"title": "", "author": "", "publish_time": "", "content_html": ""}
    # 标题：兼容 class 末尾空格 / 多 class 的情况
    m = re.search(r'<h1[^>]*class="[^"]*rich_media_title[^"]*"[^>]*>(.*?)</h1>', html, re.DOTALL)
    if not m:
        m = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
    if m:
        result["title"] = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    # 作者：兼容多 class，再退 var nickname
    m = re.search(r'class="[^"]*rich_media_meta_nickname[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', html, re.DOTALL)
    if not m:
        m = re.search(r'var nickname\s*=\s*"(.*?)"', html)
    if m:
        result["author"] = re.sub(r'<[^>]+>', '', m.group(1)).strip()
    m = re.search(r'var ct\s*=\s*"(\d+)"', html)
    if m:
        ts = int(m.group(1))
        result["publish_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    # 正文：从 rich_media_content 截到评论/相关区(rich_media_area_extra)，避免卷进页脚与广告
    m = re.search(
        r'class="[^"]*rich_media_content[^"]*"[^>]*>(.*?)(?:class="[^"]*rich_media_area_extra|id="js_sg_bar"|<!--\s*相关推荐)',
        html, re.DOTALL)
    if m:
        result["content_html"] = m.group(1).strip()
    else:
        m = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL)
        if m:
            result["content_html"] = m.group(1).strip()
    return result


def extract_stats(html: str) -> Dict[str, int]:
    """尽力从页面提取阅读/点赞/评论/分享。微信官方不直接暴露，多数情况下取不到。"""
    def g(pat):
        m = re.search(pat, html)
        return int(m.group(1)) if m else 0
    return {
        "reads": g(r'var readNum\s*=\s*(\d+)'),
        "likes": g(r'var likeNum\s*=\s*(\d+)'),
        "comments": g(r'var commentCount\s*=\s*(\d+)'),
        "shares": g(r'var shareNum\s*=\s*(\d+)'),
    }


def html_to_markdown(html: str) -> str:
    text = html
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    for i in range(1, 7):
        text = re.sub(
            rf'<h{i}[^>]*>(.*?)</h{i}>',
            lambda m: '#' * i + ' ' + re.sub(r'<[^>]+>', '', m.group(1)).strip(),
            text, flags=re.DOTALL,
        )
    text = re.sub(r'<p[^>]*>(.*?)</p>', lambda m: re.sub(r'<[^>]+>', '', m.group(1)).strip() + '\n\n', text, flags=re.DOTALL)
    text = re.sub(r'<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<(?:em|i)[^>]*>(.*?)</(?:em|i)>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)
    text = re.sub(r'<img[^>]*data-src="([^"]*)"[^>]*/?>', r'![](\1)', text)
    text = re.sub(r'<img[^>]*src="([^"]*)"[^>]*/?>', r'![](\1)', text)
    text = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1', text, flags=re.DOTALL)
    text = re.sub(r'</?[uo]l[^>]*>', '', text)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    for ent, ch in {'&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'", '&nbsp;': ' '}.items():
        text = text.replace(ent, ch)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ───────────────────────── 分类推断 ─────────────────────────
CATEGORY_KEYWORDS = {
    "Skill": ["skill", "技能", "提示词", "prompt", "教程", "玩法", "实操"],
    "AI": ["ai", "人工智能", "大模型", "gpt", "agent", "智能体"],
    "副业": ["副业", "变现", "赚钱", "收入", "搞钱"],
    "工具": ["工具", "软件", "app", "神器", "插件"],
    "观点": ["观点", "思考", "认知", "复盘", "聊"],
}


def infer_category(title: str, tags: List[str]) -> str:
    blob = (title + " " + " ".join(tags)).lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(k in blob for k in kws):
            return cat
    return "未分类"


# ───────────────────────── 各模式实现 ─────────────────────────
def _collect_mp_api(cfg, db) -> List[Dict[str, Any]]:
    """公众号所有者模式：调微信 MP 官方接口。需要 account.appid/secret。"""
    appid = cfg.get("account", "appid", default="")
    secret = cfg.get("account", "secret", default="")
    if not appid or not secret:
        logger.warning("[mp_api] 未配置 appid/secret，跳过（请在 config.json 的 account 下填写）")
        return []
    token = _mp_token(appid, secret)
    if not token:
        return []
    articles = _mp_list_published(token)
    out = []
    for art in articles:
        url = art.get("article_url") or art.get("url")
        if not url:
            continue
        existing = db.get_article_by_url(url)
        if existing:
            out.append(existing)
            continue
        md = html_to_markdown(art.get("content", ""))
        category = art.get("category") or infer_category(art.get("title", ""), art.get("tags", []))
        rec = {
            "url": url,
            "title": art.get("title"),
            "author": cfg.get("account", "name", default=""),
            "publish_time": art.get("publish_time", ""),
            "category": category,
            "tags": art.get("tags", []),
            "content_md": md,
        }
        aid = db.upsert_article(rec)
        out.append(db.get_article(aid))
        # 数据立方回填
        stats = _mp_stats(token, url)
        if stats:
            db.add_daily_stat(aid, datetime.now().strftime("%Y-%m-%d"), stats)
    return out


def _mp_token(appid: str, secret: str) -> Optional[str]:
    url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("access_token")
    except Exception as e:
        logger.error("[mp_api] 获取 token 失败: %s", e)
        return None


def _mp_list_published(token: str) -> List[Dict[str, Any]]:
    # freepublish/batchget —— 列出已发布图文
    url = f"https://api.weixin.qq.com/cgi-bin/freepublish/batchget?access_token={token}"
    body = json.dumps({"offset": 0, "count": 20, "no_content": 0}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = []
        for item in data.get("item", []):
            for art in item.get("content", {}).get("news_item", []):
                out.append({
                    "title": art.get("title"),
                    "url": art.get("url"),
                    "content": art.get("content"),
                    "publish_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.get("publish_time", 0))),
                })
        return out
    except Exception as e:
        logger.error("[mp_api] 列举已发布失败: %s", e)
        return []


def _mp_stats(token: str, url: str) -> Dict[str, int]:
    # datacube/getarticletotal —— 累计图文数据（含阅读/点赞/评论/分享）
    api = f"https://api.weixin.qq.com/datacube/getarticletotal?access_token={token}"
    today = datetime.now()
    body = json.dumps({
        "begin_date": (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        "end_date": today.strftime("%Y-%m-%d"),
    }).encode("utf-8")
    try:
        req = urllib.request.Request(api, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        for item in data.get("list", []):
            if item.get("int_page_read_user") is not None:
                return {
                    "reads": item.get("int_page_read_count", 0),
                    "likes": item.get("share_user", 0),
                    "comments": item.get("add_page_comment_count", 0),
                    "shares": item.get("share_count", 0),
                }
    except Exception as e:
        logger.debug("[mp_api] 读数据立方失败: %s", e)
    return {}


def _collect_url_feed(cfg, db) -> List[Dict[str, Any]]:
    feed_file = cfg._abs(cfg.get("collector", "source_urls_file", default="data/source_urls.json"))
    if not os.path.exists(feed_file):
        logger.warning("[url_feed] 未找到 %s，跳过", feed_file)
        return []
    entries = json.load(open(feed_file, "r", encoding="utf-8"))
    out = []
    for e in entries:
        url = e.get("url")
        if not url:
            continue
        existing = db.get_article_by_url(url)
        if existing:
            out.append(existing)
            continue
        html = fetch_html(url)
        if not html:
            continue
        c = extract_content(html)
        md = html_to_markdown(c["content_html"])
        category = e.get("category") or infer_category(c["title"], e.get("tags", []))
        tags = e.get("tags", [])
        if c["title"] and "测试" not in c["title"]:
            rec = {
                "url": url, "title": c["title"], "author": c["author"] or e.get("author", ""),
                "publish_time": c["publish_time"] or e.get("publish_time", ""),
                "category": category, "tags": tags, "content_md": md,
            }
            aid = db.upsert_article(rec)
            if cfg.get("collector", "stats_enabled", default=True):
                stats = extract_stats(html)
                if any(stats.values()):
                    db.add_daily_stat(aid, datetime.now().strftime("%Y-%m-%d"), stats)
            out.append(db.get_article(aid))
    return out


def _collect_local_folder(cfg, db) -> List[Dict[str, Any]]:
    folder = cfg.get("collector", "local_folder", default="")
    if not folder or not os.path.isdir(folder):
        logger.warning("[local_folder] 未配置或目录不存在: %s", folder)
        return []
    out = []
    for fn in os.listdir(folder):
        if not fn.lower().endswith(".md"):
            continue
        path = os.path.join(folder, fn)
        raw = open(path, "r", encoding="utf-8", errors="ignore").read()
        # 简易 frontmatter 解析
        fm, body = ({}, raw)
        m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$", raw)
        if m:
            fm, body = {}, m.group(2)
            for line in m.group(1).split("\n"):
                kv = re.match(r"^(\w[\w_]*):\s*(.*)$", line)
                if kv:
                    fm[kv.group(1)] = kv.group(2).strip()
        title = fm.get("title") or re.sub(r'\.md$', '', fn)
        # 虚拟 URL 用路径保证唯一
        url = "local://" + os.path.relpath(path, folder)
        existing = db.get_article_by_url(url)
        if existing:
            out.append(existing)
            continue
        category = fm.get("category") or infer_category(title, [])
        tags = [t.strip() for t in str(fm.get("tags", "")).split("、") if t.strip()] if fm.get("tags") else []
        aid = db.upsert_article({
            "url": url, "title": title, "author": fm.get("author", ""),
            "publish_time": fm.get("date", "") or time.strftime("%Y-%m-%d"),
            "category": category, "tags": tags, "content_md": body,
        })
        out.append(db.get_article(aid))
    return out


# ───────────────────────── demo 模式（离线样例 + 7天数据） ─────────────────────────
DEMO_TITLES = [
    ("用 WorkBuddy 把公众号变成会自动涨粉的自动化工厂", "Skill", ["WorkBuddy", "自动化", "公众号"]),
    ("我做了 8 年 AI 内容，总结了 3 条不踩坑的副业铁律", "副业", ["副业", "AI", "经验"]),
    ("提示词不是写出来的是改出来的：一个实战迭代框架", "Skill", ["提示词", "prompt", "实操"]),
    ("盘点 2026 年最值得装的效率工具 Top 10", "工具", ["工具", "效率", "盘点"]),
    ("为什么你的 AI 文章没人看？聊聊内容定位这件事", "观点", ["观点", "定位", "复盘"]),
    ("从 0 到 1 搭一个属于你的知识库：飞书 + 微信闭环", "Skill", ["知识库", "飞书", "闭环"]),
    ("AI 副业变现的 5 种路径，哪一种适合你", "副业", ["变现", "副业", "路径"]),
    ("用 Python 自动抓取公众号数据做增长复盘", "工具", ["Python", "数据", "增长"]),
    ("智能体 Agent 到底是什么？普通人怎么用起来", "AI", ["Agent", "智能体", "科普"]),
    ("内容创作者的反 AI 检测焦虑，我这样化解", "观点", ["反AI", "创作", "认知"]),
    ("一个能自己写选题的选题系统长什么样", "Skill", ["选题", "自动化", "系统"]),
    ("别再盲目日更了：用数据决定发什么", "观点", ["数据", "复盘", "策略"]),
]


def _seed_demo(cfg, db) -> List[Dict[str, Any]]:
    """生成 12 篇跨 10 天的样例文章，并铺设带增长趋势的每日数据，
    让 analyze / recommend 立刻产出真实感输出。"""
    today = datetime.now()
    out = []
    rng = random.Random(20260817)
    for i, (title, cat, tags) in enumerate(DEMO_TITLES):
        days_ago = rng.randint(0, 9)
        pub = today - timedelta(days=days_ago, hours=rng.randint(0, 23))
        url = f"https://mp.weixin.qq.com/s/demo{i:03d}"
        aid = db.upsert_article({
            "url": url, "title": title,
            "author": cfg.get("account", "name", default="宇宙第一公众号"),
            "publish_time": pub.strftime("%Y-%m-%d %H:%M:%S"),
            "category": cat, "tags": tags,
            "content_md": f"# {title}\n\n（demo 正文占位）这是一篇关于{cat}的样例文章。\n",
        })
        # 铺设发布日到今天的累计数据，制造增长 + 分类差异
        base = rng.randint(800, 4000) * (1.5 if cat in ("Skill", "副业") else 1.0)
        cum = 0
        for d in range(days_ago, -1, -1):
            day = (today - timedelta(days=d)).strftime("%Y-%m-%d")
            daily_growth = int(base * (0.15 + rng.random() * 0.25) * (1 + (9 - days_ago) * 0.05))
            cum += daily_growth
            db.add_daily_stat(aid, day, {
                "reads": daily_growth,
                "likes": int(daily_growth * rng.uniform(0.03, 0.08)),
                "comments": int(daily_growth * rng.uniform(0.005, 0.02)),
                "shares": int(daily_growth * rng.uniform(0.01, 0.04)),
            })
        out.append(db.get_article(aid))
    logger.info("[demo] 已生成 %d 篇样例文章 + 每日数据", len(out))
    return out


# ───────────────────────── 入口 ─────────────────────────
def collect(cfg, db, mode: Optional[str] = None, since: Optional[str] = None) -> List[Dict[str, Any]]:
    mode = mode or cfg.get("collector", "mode", default="url_feed")
    logger.info("开始采集（mode=%s）", mode)
    if mode == "mp_api":
        arts = _collect_mp_api(cfg, db)
    elif mode == "url_feed":
        arts = _collect_url_feed(cfg, db)
    elif mode == "local_folder":
        arts = _collect_local_folder(cfg, db)
    elif mode == "demo":
        arts = _seed_demo(cfg, db)
    else:
        logger.error("未知采集模式: %s", mode)
        arts = []
    if since:
        arts = [a for a in arts if (a.get("publish_time") or "").startswith(since)]
    logger.info("采集完成，本次涉及 %d 篇", len(arts))
    return arts
