"""多平台热点抓取 —— 选题推荐的"外部信号源"。

抓取：微博热搜 / 头条热榜 / 百度热搜
处理：指数衰减归一化打分 → 合并去重（同源/跨源）→ 返回 TopN

纯 stdlib（urllib + json + re），无第三方依赖。
"""
import json
import time
import logging
import urllib.request
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"}


def _get_json(url: str, timeout: int = 10) -> Optional[Any]:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.debug("[hotspot] 请求失败 %s: %s", url, e)
        return None


def fetch_weibo() -> List[Dict[str, Any]]:
    data = _get_json("https://weibo.com/ajax/side/hotSearch")
    if not data:
        return []
    out = []
    for it in data.get("data", {}).get("realtime", [])[:50]:
        out.append({"title": it.get("note", ""), "hot": it.get("num", 0), "source": "weibo"})
    return out


def fetch_toutiao() -> List[Dict[str, Any]]:
    data = _get_json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc")
    if not data:
        return []
    out = []
    for it in data.get("data", [])[:50]:
        out.append({"title": it.get("Title", ""), "hot": it.get("HotValue", 0), "source": "toutiao"})
    return out


def fetch_baidu() -> List[Dict[str, Any]]:
    import re
    try:
        req = urllib.request.Request("https://top.baidu.com/board?tab=realtime", headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8")
        m = re.search(r'<!--s-data:(.*?)-->', html, re.DOTALL)
        if not m:
            return []
        data = json.loads(m.group(1))
    except Exception as e:
        logger.debug("[hotspot] 百度失败: %s", e)
        return []
    out = []
    for card in data.get("data", {}).get("cards", []):
        for it in card.get("content", [])[:50]:
            out.append({"title": it.get("query", ""), "hot": it.get("hotScore", 0),
                        "desc": it.get("desc", ""), "source": "baidu"})
    return out


FETCHERS = {"weibo": fetch_weibo, "toutiao": fetch_toutiao, "baidu": fetch_baidu}


def _normalize(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not items:
        return items
    sorted_items = sorted(items, key=lambda x: x.get("hot", 0), reverse=True)
    decay = 0.95
    for i, it in enumerate(sorted_items):
        rank_score = (len(sorted_items) - i) / len(sorted_items)
        decay_score = decay ** i
        it["score"] = round((rank_score * 0.4 + decay_score * 0.6) * 100, 2)
    return sorted_items


def _dedup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    title_map: Dict[str, Dict[str, Any]] = {}
    for it in items:
        t = (it.get("title") or "").strip()
        if not t:
            continue
        if t in title_map:
            title_map[t]["sources"].append(it["source"])
            title_map[t]["score"] = round(title_map[t]["score"] * 0.7 + it.get("score", 0) * 0.3, 2)
        else:
            title_map[t] = {"title": t, "sources": [it["source"]], "score": it.get("score", 0),
                            "hot": it.get("hot", 0), "desc": it.get("desc", "")}
    return sorted(title_map.values(), key=lambda x: x["score"], reverse=True)


def fetch_hotspots(sources: List[str] = None, limit: int = 20, retries: int = 2) -> List[Dict[str, Any]]:
    sources = sources or list(FETCHERS.keys())
    all_items: List[Dict[str, Any]] = []
    for s in sources:
        fn = FETCHERS.get(s)
        if not fn:
            continue
        items = None
        for attempt in range(retries):
            items = fn()
            if items:
                break
            time.sleep(0.8 * (attempt + 1))
        if items:
            logger.info("[hotspot] %s 获取 %d 条", s, len(items))
            all_items.extend(items)
        else:
            logger.warning("[hotspot] %s 本次未取到", s)
    all_items = _normalize(all_items)
    merged = _dedup(all_items)
    return merged[:limit]
