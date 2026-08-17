"""自动检测引擎 —— 消除"手动贴链接"的核心。

思路：微信公众号没有"文章发布"的 webhook/push 事件，所以"自动识别最近发布的文章"
只能靠主动询问微信官方接口：用 account.appid/secret 调 freepublish/batchget 列出
已群发文章，再与本系统数据库比对，挑出"尚未入库"的新文，自动抓取正文并入库。

注意（沙箱限制）：该接口要求调用方 IP 在公众号「IP 白名单」内。在用户本机跑（且加了
白名单）即可稳定自动检测；在 WorkBuddy 沙箱里会被微信拒绝（errcode 40164），此时
detect 会优雅返回空列表（不报错），真正的零延迟捕获由 publisher 发布钩子 bridge 承担
（它持有发布正文，无需白名单）。

对标"使用 A 技能即自动捕获"的双保险：
  1) publisher 发布钩子（bridge）—— 发布瞬间即时捕获，零延迟、无需白名单；
  2) 本检测引擎（detect）—— 周期性轮询，兜底捕获"在后台直接群发"的文章。
"""
import logging
from typing import List, Dict, Any
from datetime import datetime

from . import collector
from .collector import _mp_token, _mp_list_published, html_to_markdown, infer_category

logger = logging.getLogger(__name__)


def detect_new(cfg, db) -> List[Dict[str, Any]]:
    """列出已发布文章，挑出未入库的新文并写入数据库。返回本次新增的文章列表。

    返回空列表的几种情况（均不报错）：
      - 未配置 appid/secret
      - 微信 token 获取失败（IP 不在白名单 / 凭证错）
      - 接口未返回已群发文章
      - 全部已入库
    """
    appid = cfg.get("account", "appid", default="")
    secret = cfg.get("account", "secret", default="")
    if not appid or not secret:
        logger.warning("[detect] 未配置 account.appid/secret，无法自动检测（请在 config.json 填写）")
        return []
    token = _mp_token(appid, secret)
    if not token:
        logger.warning("[detect] 获取微信 token 失败（检查 IP 白名单 / appid / secret）")
        return []
    items = _mp_list_published(token)
    if not items:
        logger.info("[detect] 接口未返回已发布文章（可能无已群发，或 IP 不在白名单）")
        return []
    new: List[Dict[str, Any]] = []
    for art in items:
        url = art.get("url")
        if not url:
            continue
        if db.get_article_by_url(url):
            continue  # 已入库，跳过（幂等）
        md = html_to_markdown(art.get("content") or "")
        category = infer_category(art.get("title", ""), [])
        rec = {
            "url": url,
            "title": art.get("title"),
            "author": cfg.get("account", "name", default=""),
            "publish_time": art.get("publish_time", ""),
            "category": category,
            "tags": [],
            "content_md": md,
        }
        aid = db.upsert_article(rec)
        stats = collector._mp_stats(token, url)
        if stats:
            db.add_daily_stat(aid, datetime.now().strftime("%Y-%m-%d"), stats)
        new.append(db.get_article(aid))
        logger.info("[detect] 发现新文章：%s", art.get("title"))
    logger.info("[detect] 本次新增 %d 篇", len(new))
    return new
