"""分析器 —— 把滚动数据变成可读的结论。

  daily_summary(date)   某一天发布了几篇、当天各篇数据、当日总量
  analyze_7day(days)    近 N 天整体表现：总量 / 分类表现 / Top 文章 / 环比增长
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


def _date_range(end: datetime, days: int):
    end_s = end.strftime("%Y-%m-%d")
    start = end - timedelta(days=days - 1)
    start_s = start.strftime("%Y-%m-%d")
    return start_s, end_s


def daily_summary(db, date: str = None) -> Dict[str, Any]:
    date = date or datetime.now().strftime("%Y-%m-%d")
    snap = db.get_daily_snapshot(date)
    logger.info("[analyze] %s 当日发布 %d 篇，阅读 %d", date, snap["published_count"], snap["totals"]["reads"])
    return snap


def analyze_7day(db, days: int = 7, end: datetime = None) -> Dict[str, Any]:
    end = end or datetime.now()
    start_s, end_s = _date_range(end, days)
    prev_start = (end - timedelta(days=2 * days - 1)).strftime("%Y-%m-%d")
    prev_end = (end - timedelta(days=days)).strftime("%Y-%m-%d")

    arts = {a["id"]: a for a in db.list_articles()}
    cur_rows = db.get_stats_in_range(start_s, end_s)
    prev_rows = db.get_stats_in_range(prev_start, prev_end)

    def aggregate(rows):
        totals = {"reads": 0, "likes": 0, "comments": 0, "shares": 0}
        per_cat: Dict[str, Dict[str, Any]] = {}
        per_article: Dict[int, Dict[str, int]] = {}
        for r in rows:
            a = arts.get(r["article_id"])
            cat = a["category"] if a else "未分类"
            for k in totals:
                totals[k] += (r.get(k) or 0)
            c = per_cat.setdefault(cat, {"reads": 0, "likes": 0, "comments": 0, "shares": 0, "articles": set()})
            for k in totals:
                c[k] += (r.get(k) or 0)
            c["articles"].add(r["article_id"])
            pa = per_article.setdefault(r["article_id"], {"reads": 0, "likes": 0, "comments": 0, "shares": 0})
            for k in totals:
                pa[k] += (r.get(k) or 0)
        return totals, per_cat, per_article

    cur_tot, cur_cat, cur_art = aggregate(cur_rows)
    prev_tot, _, _ = aggregate(prev_rows)

    # 分类表现（按阅读量排序）
    cat_perf = []
    for cat, c in cur_cat.items():
        reads = c["reads"]
        eng = ((c["likes"] + c["comments"] + c["shares"]) / reads) if reads else 0
        cat_perf.append({
            "category": cat,
            "reads": reads,
            "likes": c["likes"],
            "comments": c["comments"],
            "shares": c["shares"],
            "articles": len(c["articles"]),
            "engagement_rate": round(eng * 100, 2),
        })
    cat_perf.sort(key=lambda x: x["reads"], reverse=True)

    # Top 文章
    top_articles = []
    for aid, pa in sorted(cur_art.items(), key=lambda kv: kv[1]["reads"], reverse=True)[:10]:
        a = arts.get(aid, {})
        top_articles.append({
            "id": aid, "title": a.get("title", ""), "category": a.get("category", ""),
            "reads": pa["reads"], "likes": pa["likes"],
            "engagement_rate": round(((pa["likes"] + pa["comments"] + pa["shares"]) / pa["reads"] * 100), 2) if pa["reads"] else 0,
        })

    # 环比增长
    def growth(cur, prev):
        return round((cur - prev) / prev * 100, 2) if prev else (100.0 if cur else 0.0)

    result = {
        "window": {"start": start_s, "end": end_s, "days": days},
        "totals": cur_tot,
        "prev_totals": prev_tot,
        "growth": {
            "reads": growth(cur_tot["reads"], prev_tot["reads"]),
            "likes": growth(cur_tot["likes"], prev_tot["likes"]),
            "comments": growth(cur_tot["comments"], prev_tot["comments"]),
            "shares": growth(cur_tot["shares"], prev_tot["shares"]),
        },
        "overall_engagement_rate": round(
            ((cur_tot["likes"] + cur_tot["comments"] + cur_tot["shares"]) / cur_tot["reads"] * 100), 2
        ) if cur_tot["reads"] else 0,
        "category_performance": cat_perf,
        "top_articles": top_articles,
        "articles_in_window": len(cur_art),
    }
    logger.info("[analyze] 近 %d 天：阅读 %d（环比 %s%%），互动率 %s%%",
                days, cur_tot["reads"], result["growth"]["reads"], result["overall_engagement_rate"])
    return result
