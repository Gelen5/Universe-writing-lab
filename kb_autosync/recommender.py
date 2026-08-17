"""选题推荐器 —— 系统的"大脑"，让知识库会自己想选题。

逻辑：
  1. 读近 7 天复盘，给每个分类打"值得做"的分（阅读占比 × 互动率）。
  2. 抓多平台热点，用领域关键词过滤出与账号相关的外部信号。
  3. 组合：历史高分分类 × 当前热点 = 具体可发的选题标题 + 理由。
  4. 落库（recommendations + topics_pool），并可选推一份"选题池"到飞书。
"""
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from . import analyzer, hotspots, sync as sync_mod
from .collector import infer_category

logger = logging.getLogger(__name__)

TITLE_TEMPLATES = {
    "Skill": "用「{hot}」写一个能马上落地的 {kw} 实操工作流",
    "副业": "「{hot}」背后，普通人能抓住的 3 个变现机会",
    "工具": "盘点借「{hot}」火起来的效率工具，别再用旧的了",
    "AI": "「{hot}」与 AI：普通人真正该关心的 3 件事",
    "观点": "聊聊「{hot}」：一个内容创作者的真实判断",
    "未分类": "关于「{hot}」，我想说几句真话",
}
HISTORY_TEMPLATE = {
    "Skill": "你的 {kw} 类内容近 7 天数据最稳，继续深做一篇进阶玩法",
    "副业": "{kw} 方向互动率最高，再出一篇更落地的变现拆解",
    "工具": "{kw} 类一直有稳定读者，补一篇年度精选",
    "AI": "AI 是基本盘，用一篇更贴近生活的科普稳住流量",
    "观点": "观点类带来高互动，借势写一篇引发共鸣的复盘",
    "未分类": "围绕你最擅长的方向，再写一篇",
}


def _actual_categories(db) -> Dict[str, int]:
    """从已采集文章推导真实分类及其篇数（数据稀疏时作为保底）。"""
    cats: Dict[str, int] = {}
    for a in db.list_articles():
        c = a.get("category") or "未分类"
        cats[c] = cats.get(c, 0) + 1
    return cats


def _pick(templates: Dict[str, str], category: str) -> str:
    """模板模糊匹配：精确 → 子串双向 → 未分类兜底。分类名用户自定义，不能硬对齐。"""
    if category in templates:
        return templates[category]
    for k, v in templates.items():
        if k and (k in category or category in k):
            return v
    return templates.get("未分类", next(iter(templates.values())))


def _category_scores(analysis: Dict[str, Any], db) -> Dict[str, float]:
    cats = analysis.get("category_performance", [])
    if cats:
        max_reads = max([c["reads"] for c in cats] or [1], default=1)
        scores = {}
        for c in cats:
            read_score = (c["reads"] / max_reads) * 100 if max_reads else 0
            eng_score = min(c["engagement_rate"] * 5, 100)  # 20% 互动率→满分
            scores[c["category"]] = round(0.6 * read_score + 0.4 * eng_score, 2)
        return scores
    # 稀疏兜底：用真实分类，按篇数给基础分（越多越值得做），避免开局 0 选题
    actual = _actual_categories(db)
    if actual:
        maxn = max(actual.values())
        return {k: round(40 + 50 * (v / maxn), 2) for k, v in actual.items()}
    return {"Skill": 60, "副业": 55, "工具": 50, "AI": 50, "观点": 45}


def _build_rec(category: str, cat_scores: Dict[str, float], hot: Optional[Dict[str, Any]],
               hw: float, sw: float, date: str) -> Dict[str, Any]:
    kw = category
    if hot:
        hot_title = hot["title"]
        title = _pick(TITLE_TEMPLATES, category).format(hot=hot_title, kw=kw)
        sources = hot.get("sources", [])
        hotspot_score = hot.get("score", 50)
        rationale = (
            f"外部热点「{hot_title}」（来源：{'/'.join(sources)}，热度分 {hotspot_score}）"
            f"与你的高分分类「{category}」结合；历史看该分类阅读与互动双高，借势转化更稳。"
        )
    else:
        title = _pick(HISTORY_TEMPLATE, category).format(kw=kw)
        sources = ["history"]
        hotspot_score = 0
        rationale = (
            f"近 7 天「{category}」分类综合表现最佳（历史分 {cat_scores.get(category, 0)}），"
            f"无需蹭热点，深挖该方向即可维持基本盘。"
        )
    cat_score = cat_scores.get(category, 50)
    score = round(hw * cat_score + sw * hotspot_score, 2)
    return {
        "date": date, "title": title, "category": category,
        "rationale": rationale, "sources": sources, "score": score,
        "status": "pending",
    }


def _continuation_recs(db, cat_scores: Dict[str, float], date: str) -> List[Dict[str, Any]]:
    """内容延展选题：基于用户已发布文章做续集/清单/深挖，数据稀疏时最对味。"""
    arts = db.list_articles()[:6]
    primary = max(cat_scores, key=cat_scores.get) if cat_scores else "副业手记"
    templates = [
        "读完《{t}》，读者最想追问的 3 件事（下一篇就写它）",
        "《{t}》没说完的：一个更扎心的后续",
        "把《{t}》里的观点，整理成一份能照做的清单",
    ]
    out = []
    for i, a in enumerate(arts[:3]):
        t = a.get("title", "")
        if not t:
            continue
        cat = a.get("category") or primary
        title = templates[i % len(templates)].format(t=t)
        out.append({
            "date": date, "title": title, "category": cat,
            "rationale": f"基于你已发布的《{t}》做续集/延展——真实经历类内容的回访与转发最高，"
                         f"顺着已有爆点往下挖性价比远高于硬蹭热点。",
            "sources": ["your_content"],
            "score": round(cat_scores.get(cat, 50) * 0.92, 2),
            "status": "pending",
        })
    return out


def recommend(db, cfg, top_n: Optional[int] = None, end: datetime = None,
              write_kb: bool = False, dry_run: Optional[bool] = None) -> List[Dict[str, Any]]:
    top_n = top_n or cfg.get("recommender", "top_n", default=5)
    days = cfg.get("analyzer", "history_days", default=7)
    hw = cfg.get("recommender", "history_weight", default=0.5)
    sw = cfg.get("recommender", "hotspot_weight", default=0.5)
    domain_kw = cfg.get("recommender", "domain_keywords", default=[])
    sources = cfg.get("recommender", "hotspot_sources", default=["weibo", "toutiao", "baidu"])
    date = (end or datetime.now()).strftime("%Y-%m-%d")

    analysis = analyzer.analyze_7day(db, days, end)
    cat_scores = _category_scores(analysis, db)
    primary_cat = max(cat_scores, key=cat_scores.get)

    # 外部热点（限流、失败不致命）
    hot_list = hotspots.fetch_hotspots(sources=sources, limit=30)
    relevant = [h for h in hot_list if any(kw.lower() in h["title"].lower() for kw in domain_kw)]
    logger.info("[recommend] 热点 %d 条，领域相关 %d 条", len(hot_list), len(relevant))

    recs: List[Dict[str, Any]] = []
    # 1) 历史/真实分类驱动：给表现最好的分类各来一条（有数据时按表现，稀疏时按真实分类）
    cats_perf = analysis.get("category_performance", [])
    if cats_perf:
        cats_iter = [c["category"] for c in cats_perf[:3]]
    else:
        cats_iter = list(_actual_categories(db).keys())[:3] or [primary_cat]
    for c in cats_iter:
        recs.append(_build_rec(c, cat_scores, None, hw, sw, date))

    # 2) 热点驱动：领域相关优先；若 0 条则退化为「热度最高」并按主分类改写角度，保证有外部信号
    if relevant:
        hot_iter = relevant
    else:
        hot_iter = sorted(hot_list, key=lambda h: h.get("score", 0), reverse=True)[: top_n + 2]
        logger.info("[recommend] 领域相关热点为 0，退化使用热度最高的 %d 条并按主分类改写", len(hot_iter))
    for h in hot_iter[: top_n + 2]:
        cat = infer_category(h["title"], []) or primary_cat
        if cat not in cat_scores:
            cat = primary_cat
        rec = _build_rec(cat, cat_scores, h, hw, sw, date)
        recs.append(rec)

    # 3) 内容延展：基于已发布文章做续集/清单（始终产出，最贴合个人知识库）
    recs.extend(_continuation_recs(db, cat_scores, date))

    # 打分排序 + 去重（同标题只留最高分）
    seen = {}
    for r in recs:
        if r["title"] not in seen or r["score"] > seen[r["title"]]["score"]:
            seen[r["title"]] = r
    recs = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:top_n]

    # 落库 + 选题池
    for r in recs:
        rid = db.add_recommendation(r)
        db.add_topic(r["title"], status="idea", source_rec_id=rid, note=r["category"])

    logger.info("[recommend] 生成 %d 条选题", len(recs))

    if write_kb and recs:
        pushed = sync_mod.push_topics_to_kb(cfg, recs, dry_run=dry_run)
        logger.info("[recommend] 选题池推送飞书：%s", pushed)
    return recs


def format_recs(recs: List[Dict[str, Any]]) -> str:
    lines = ["# 次日选题推荐（自动生成）\n"]
    for i, r in enumerate(recs, 1):
        lines.append(f"## {i}. {r['title']}  （综合分 {r['score']}）")
        lines.append(f"- 分类：{r['category']}")
        lines.append(f"- 来源：{'/'.join(r.get('sources', []))}")
        lines.append(f"- 理由：{r['rationale']}\n")
    return "\n".join(lines)
