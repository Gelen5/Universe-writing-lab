"""调度与任务编排 —— 把各模块按触发时间串成流水线。

触发时间（config.schedule，可改）：
  早 08:00  morning  采集 + 同步飞书 + 当日数据梳理
  晚 20:00  evening  采集 + 同步飞书 + 当日数据梳理
  晚 22:00  night    采集 + 同步飞书 + 当日梳理 + 近7天复盘 + 次日选题推荐

Scheduler 为纯 stdlib 定时循环（无需额外依赖），也可由 WorkBuddy 自动化触发 run_job。
"""
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

from . import collector, sync, analyzer, recommender

logger = logging.getLogger(__name__)


def run_job(cfg, db, job: str, dry_run: Optional[bool] = None) -> dict:
    """执行一个定时任务。返回本次执行的摘要 dict。"""
    started = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    summary: dict = {"job": job, "steps": []}

    # 1) 采集
    mode = cfg.get("collector", "mode", default="url_feed")
    arts = collector.collect(cfg, db, mode=mode)
    summary["steps"].append({"collect": {"mode": mode, "items": len(arts)}})

    # 2) 同步飞书（仅 trigger_categories，或 night 全量）
    if cfg.get("sync", "enabled", default=True):
        cats = None if job == "night" else (cfg.get("sync", "trigger_categories", default=[]) or None)
        sres = sync.sync_new(cfg, db, categories=cats, dry_run=dry_run)
        summary["steps"].append({"sync": sres})

    # 3) 当日梳理（morning/evening/night 都有）
    day = analyzer.daily_summary(db)
    summary["steps"].append({"daily": {"published": day["published_count"], "reads": day["totals"]["reads"]}})

    # 4) 7天复盘 + 选题（仅 night）
    if job == "night":
        rep = analyzer.analyze_7day(db, cfg.get("analyzer", "history_days", default=7))
        summary["steps"].append({
            "7day": {
                "reads": rep["totals"]["reads"],
                "growth_reads": rep["growth"]["reads"],
                "engagement_rate": rep["overall_engagement_rate"],
                "top_category": (rep["category_performance"][0]["category"] if rep["category_performance"] else "-"),
            }
        })
        recs = recommender.recommend(
            db, cfg,
            write_kb=cfg.get("sync", "enabled", default=True),
            dry_run=dry_run,
        )
        summary["steps"].append({"recommend": {"count": len(recs),
                                               "top": recs[0]["title"] if recs else "-"}})
    else:
        # evening 也顺手出一份 7 天概览（不写选题）
        rep = analyzer.analyze_7day(db, cfg.get("analyzer", "history_days", default=7))
        summary["steps"].append({"7day_preview": {"reads": rep["totals"]["reads"],
                                                  "growth_reads": rep["growth"]["reads"]}})

    db.record_run(job, started, "success", summary)
    logger.info("[job:%s] 完成 steps=%d", job, len(summary["steps"]))
    return summary


class Scheduler:
    """极简定时循环：在配置的 HH:MM 触发对应 job。"""

    def __init__(self, cfg, db, dry_run: Optional[bool] = None):
        self.cfg = cfg
        self.db = db
        self.dry_run = dry_run
        self.jobs = {
            "morning": cfg.get("schedule", "morning", default="08:00"),
            "evening": cfg.get("schedule", "evening", default="20:00"),
            "night": cfg.get("schedule", "night", default="22:00"),
        }
        self._stop = False

    def _next_trigger(self, hhmm: str) -> datetime:
        h, m = (int(x) for x in hhmm.split(":"))
        now = datetime.now()
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand

    def start(self):
        logger.info("[scheduler] 启动，任务：%s", self.jobs)
        print(f"调度器已启动（早 {self.jobs['morning']} / 晚 {self.jobs['evening']} / 夜 {self.jobs['night']}）。Ctrl+C 退出。")
        try:
            while not self._stop:
                now = datetime.now()
                # 找出最近的一次触发
                upcoming = []
                for job, hhmm in self.jobs.items():
                    t = self._next_trigger(hhmm)
                    if t > now:
                        upcoming.append((t, job))
                if not upcoming:
                    time.sleep(30)
                    continue
                upcoming.sort(key=lambda x: x[0])
                next_t, next_job = upcoming[0]
                wait = (next_t - datetime.now()).total_seconds()
                if wait > 1:
                    time.sleep(min(wait, 60))
                    continue
                # 到点执行
                try:
                    run_job(self.cfg, self.db, next_job, self.dry_run)
                except Exception as e:
                    logger.exception("[scheduler] job %s 失败: %s", next_job, e)
                time.sleep(61)  # 避免同一分钟重复触发
        except KeyboardInterrupt:
            print("\n调度器已停止。")
        finally:
            self.db.close()

    def stop(self):
        self._stop = True


def watch(cfg, db, interval_min: Optional[int] = None, dry_run: Optional[bool] = None):
    """轮询模式：每隔 N 分钟采集一次，命中 trigger_categories 的新文章立即同步飞书。

    用于弥补微信没有"发布 webhook"的缺口——近似"发布后自动同步"。
    """
    interval = interval_min or cfg.get("schedule", "watch_interval_min", default=15)
    logger.info("[watch] 每 %d 分钟轮询一次新文章并同步", interval)
    print(f"watch 模式：每 {interval} 分钟轮询（Ctrl+C 退出）。")
    try:
        while True:
            try:
                arts = collector.collect(cfg, db, mode=cfg.get("collector", "mode", default="url_feed"))
                if cfg.get("sync", "enabled", default=True):
                    sync.sync_new(cfg, db, dry_run=dry_run)
            except Exception as e:
                logger.exception("[watch] 轮询出错: %s", e)
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        print("\nwatch 已停止。")
    finally:
        db.close()
