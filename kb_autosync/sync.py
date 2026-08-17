"""飞书知识库同步 —— 使用本包自带的 kb_autosync.feishu（自包含，无外部依赖）。

流程：把文章写成带 frontmatter 的 .md 暂存文件 → 调用 feishu.sync_file
（解析知识空间、建 docx 节点、块写入、重试/待队列）→ 记录飞书节点回数据库。
"""
import os
import json
import logging
from typing import List, Dict, Any, Optional

from .feishu import sync_file as _feishu_sync_file

logger = logging.getLogger(__name__)


def _feishu_opts(cfg) -> Dict[str, Any]:
    """从本项目配置构造飞书连接参数。"""
    f = cfg.get("feishu", default={}) or {}
    staging = cfg._abs(cfg.get("sync", "staging_dir", default="data/staging"))
    log_dir = os.path.join(os.path.dirname(staging), ".cache")
    return {
        "app_id": f.get("app_id", ""),
        "app_secret": f.get("app_secret", ""),
        "space_id": f.get("space_id", ""),
        "parent_node_token": f.get("parent_node_token", ""),
        "categories": f.get("categories", {}) or {},
        "default_category": f.get("default_category", "未分类"),
        "log_dir": log_dir,
    }


def _stage_md(cfg, article: Dict[str, Any]) -> str:
    staging = cfg._abs(cfg.get("sync", "staging_dir", default="data/staging"))
    os.makedirs(staging, exist_ok=True)
    safe = "".join(c if (c.isalnum() or c in " -_") else "_" for c in (article.get("title") or "untitled"))[:60]
    path = os.path.join(staging, f"{article['id']:04d}_{safe}.md")
    fm = (
        f"---\n"
        f"title: \"{article.get('title', '').replace(chr(34), '')}\"\n"
        f"author: {article.get('author', '—')}\n"
        f"date: {article.get('publish_time', '')[:10]}\n"
        f"category: {article.get('category', '未分类')}\n"
        f"tags: {'、'.join(article.get('tags') or [])}\n"
        f"source_url: {article.get('url', '')}\n"
        f"---\n\n"
    )
    body = article.get("content_md") or ""
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm + body + "\n")
    return path


def sync_article(cfg, db, article: Dict[str, Any], dry_run: Optional[bool] = None) -> Optional[Dict[str, Any]]:
    dry = dry_run if dry_run is not None else cfg.get("sync", "dry_run", default=True)
    path = _stage_md(cfg, article)
    opts = _feishu_opts(cfg)
    opts.update({
        "file": path,
        "category": article.get("category", "未分类"),
        "title": article.get("title"),
        "dry_run": dry,
    })
    try:
        node = _feishu_sync_file(opts)
    except Exception as e:
        logger.error("[sync] 同步报错: %s", e)
        return None
    if node:
        db.mark_synced(article["id"], node)
        logger.info("[sync] 已同步《%s》→ %s", article.get("title"), node.get("url"))
        return node
    logger.warning("[sync] 《%s》未返回节点（可能 dry_run 或失败，见日志）", article.get("title"))
    return None


def push_topics_to_kb(cfg, recs: List[Dict[str, Any]], dry_run: Optional[bool] = None) -> Dict[str, Any]:
    """把选题池作为一篇文档推到飞书（分类=选题池，需在 config.feishu.categories 配置节点）。

    返回 {"pushed": bool, "url": ..., "staged": path}。best-effort。
    """
    from .recommender import format_recs
    dry = dry_run if dry_run is not None else cfg.get("sync", "dry_run", default=True)
    staging = cfg._abs(cfg.get("sync", "staging_dir", default="data/staging"))
    os.makedirs(staging, exist_ok=True)
    date = datetime_now()
    path = os.path.join(staging, f"选题池_{date}.md")
    body = format_recs(recs)
    fm = (
        f"---\ntitle: \"次日选题池 {date}\"\n"
        f"author: kb_autosync\ndate: {date}\ncategory: 选题池\ntags: 选题\n---\n\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm + body + "\n")

    opts = _feishu_opts(cfg)
    opts.update({"file": path, "category": "选题池", "dry_run": dry})
    try:
        node = _feishu_sync_file(opts)
    except Exception as e:
        logger.error("[sync] 选题池推送失败: %s", e)
        return {"pushed": False, "staged": path}
    if node:
        logger.info("[sync] 选题池已推送 → %s", node.get("url"))
        return {"pushed": True, "url": node.get("url"), "staged": path}
    return {"pushed": False, "staged": path}


def datetime_now() -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d")


def sync_new(cfg, db, categories: Optional[List[str]] = None, dry_run: Optional[bool] = None) -> Dict[str, Any]:
    """同步尚未同步的文章。categories=None 表示同步全部（忽略 trigger_categories）。"""
    if categories is None:
        trigger = cfg.get("sync", "trigger_categories", default=[])
        categories = trigger or None
    pending = db.unsynced(categories)
    logger.info("[sync] 待同步 %d 篇（categories=%s）", len(pending), categories or "全部")
    ok, fail = 0, 0
    for art in pending:
        node = sync_article(cfg, db, art, dry_run=dry_run)
        if node:
            ok += 1
        else:
            fail += 1
    return {"pending": len(pending), "synced": ok, "skipped_or_failed": fail}
