"""发布钩子（bridge）—— 让"使用 A 技能发布 = 自动捕获进知识库"。

当 wechat-publisher-ultimate 发布（或群发）文章后，它会调用本模块的
capture_after_publish()，把刚发布的文章即时捕获进 kb_autosync：
  - 入库（SQLite，幂等去重）
  - 同步飞书知识库

为什么这是"零手动链接"最稳的路径：
  发布流程本身已经持有了文章正文（html/title），钩子直接用这些内容，
  既不需要重新抓网，也不需要公众号 IP 白名单 —— 任何环境都能即时捕获。
相比之下 detect（mp_api 轮询）需要白名单，是发布流程之外的兜底。

设计原则：
  - 非致命：kb_autosync 缺失/报错时仅记录 warning，绝不影响原发布流程。
  - 幂等：同一篇文章（按 url 或 title）重复调用不会重复入库。
  - 路径可配置：KB_AUTOSYNC_DIR 环境变量 > 默认猜测路径。
"""
import os
import sys
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# 默认 kb_autosync 目录（本机已知位置）；可用环境变量 KB_AUTOSYNC_DIR 覆盖
_DEFAULT_KB_DIR = r"C:/Users/16972/WorkBuddy/2026-08-17-19-48-46/kb_autosync"


def _resolve_kb_dir() -> Optional[str]:
    d = os.environ.get("KB_AUTOSYNC_DIR") or _DEFAULT_KB_DIR
    return d if os.path.isdir(d) else None


def _load_kb():
    """定位并加载 kb_autosync，返回 (cfg, DB) 或 None。"""
    base = _resolve_kb_dir()
    if not base:
        return None
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        from kb_autosync import config as kb_cfg, db as kb_db
        cfg = kb_cfg.load()
        return cfg, kb_db.DB(cfg.db_path)
    except Exception as e:  # noqa
        logger.warning("[bridge] 加载 kb_autosync 失败：%s", e)
        return None


def _coerce_tags(tags) -> List[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.replace("、", ",").split(",") if t.strip()]
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return []


def capture_after_publish(
    *,
    url: Optional[str] = None,
    title: Optional[str] = None,
    content_md: Optional[str] = None,
    category: Optional[str] = None,
    tags=None,
    author: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """捕获一篇已发布文章进知识库。

    优先级：
      - content_md 直接提供（发布钩子最常用，最快）→ 直接用
      - 否则若给了 url → 抓取正文
      - 已存在（按 url）→ 跳过（幂等）
    """
    loaded = _load_kb()
    if not loaded:
        return {"status": "skipped", "reason": "kb_autosync 未找到"}
    cfg, db = loaded
    try:
        if url and db.get_article_by_url(url):
            return {"status": "already", "url": url}

        if not content_md and url:
            # 仅有链接时回退到抓取
            from kb_autosync.collector import (
                fetch_html, extract_content, html_to_markdown, infer_category,
            )
            html = fetch_html(url)
            if html:
                c = extract_content(html)
                title = title or c["title"]
                author = author or c["author"]
                content_md = html_to_markdown(c["content_html"])
                category = category or infer_category(title or "", _coerce_tags(tags))

        if not content_md:
            return {"status": "skipped", "reason": "无正文可入库"}

        from kb_autosync import sync as sync_mod
        rec = {
            "url": url or ("local://publish/" + (title or "untitled")),
            "title": title or "未命名",
            "author": author or cfg.get("account", "name", default=""),
            "publish_time": "",
            "category": category or "未分类",
            "tags": _coerce_tags(tags),
            "content_md": content_md,
        }
        aid = db.upsert_article(rec)
        node = None
        if cfg.get("sync", "enabled", default=True):
            node = sync_mod.sync_article(cfg, db, db.get_article(aid), dry_run=dry_run)
        feishu_url = node.get("url") if isinstance(node, dict) else None
        logger.info("[bridge] 捕获文章《%s》→ 飞书 %s（dry=%s）",
                    rec["title"], feishu_url or "预览", dry_run)
        return {
            "status": "captured",
            "article_id": aid,
            "title": rec["title"],
            "feishu": feishu_url,
            "dry_run": dry_run,
        }
    finally:
        db.close()
