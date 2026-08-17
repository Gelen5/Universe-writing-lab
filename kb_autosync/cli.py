#!/usr/bin/env python3
"""kb_autosync 命令行入口。

子命令：
  collect    采集文章（--mode demo|mp_api|url_feed|local_folder）
  sync       同步未推送的文章到飞书（--all 同步全部分类，默认仅 trigger_categories）
  analyze    当日梳理 / 近 N 天复盘（--days 7）
  recommend  生成次日选题（--top-n 5，--no-kb 不推飞书）
  run        执行一个定时任务 job=morning|evening|night
  demo       离线跑通整条流水线（demo 数据 → 同步 → 分析 → 选题）
  scheduler  常驻定时循环（早8/晚8/晚10）
  watch      轮询模式，命中新文章立即同步飞书
  status     数据库概览

示例：
  python -m kb_autosync.cli demo
  python -m kb_autosync.cli run --job night
  python -m kb_autosync.cli scheduler
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime

# 允许直接 `python cli.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kb_autosync import config as cfgmod
from kb_autosync import db as dbmod
from kb_autosync import collector, sync, analyzer, recommender, scheduler as sched_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("kb_autosync")


def _db(cfg):
    return dbmod.DB(cfg.db_path)


def cmd_collect(args, cfg):
    d = _db(cfg)
    arts = collector.collect(cfg, d, mode=args.mode, since=args.since)
    print(f"采集完成：涉及 {len(arts)} 篇")
    for a in arts[:20]:
        print(f"  - [{a.get('category')}] {a.get('title')}  ({a.get('publish_time','')[:10]})")
    d.close()


def cmd_sync(args, cfg):
    d = _db(cfg)
    dry = not args.no_dry_run
    cats = None if args.all else (cfg.get("sync", "trigger_categories", default=[]) or None)
    res = sync.sync_new(cfg, d, categories=cats, dry_run=dry)
    print(f"同步结果：待同步 {res['pending']} → 成功 {res['synced']}，跳过/失败 {res['skipped_or_failed']}"
          f"（{'预览' if dry else '已推送飞书'}）")
    d.close()


def cmd_analyze(args, cfg):
    d = _db(cfg)
    if args.daily:
        rep = analyzer.daily_summary(d, args.date)
    else:
        rep = analyzer.analyze_7day(d, args.days)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_analysis(rep, daily=args.daily)
    d.close()


def _print_analysis(rep, daily=False):
    if daily:
        print(f"\n=== 当日数据梳理 {rep['date']} ===")
        print(f"发布文章：{rep['published_count']} 篇")
        print(f"当日总量：阅读 {rep['totals']['reads']} / 点赞 {rep['totals']['likes']} / "
              f"评论 {rep['totals']['comments']} / 分享 {rep['totals']['shares']}")
        for a in rep["articles"][:10]:
            print(f"  - {a['title'][:30]}  阅读{a['reads']} 赞{a['likes']}")
        return
    print(f"\n=== 近 {rep['window']['days']} 天整体表现（{rep['window']['start']} ~ {rep['window']['end']}）===")
    print(f"总量：阅读 {rep['totals']['reads']} / 点赞 {rep['totals']['likes']} / "
          f"评论 {rep['totals']['comments']} / 分享 {rep['totals']['shares']}")
    g = rep["growth"]
    print(f"环比增长：阅读 {g['reads']}% / 点赞 {g['likes']}% / 评论 {g['comments']}% / 分享 {g['shares']}%")
    print(f"整体互动率：{rep['overall_engagement_rate']}%")
    print("\n分类表现（按阅读）：")
    for c in rep["category_performance"]:
        print(f"  - {c['category']:>6}  阅读{c['reads']:>6}  互动率{c['engagement_rate']}%  篇数{c['articles']}")
    print("\nTop 文章：")
    for a in rep["top_articles"][:5]:
        print(f"  - {a['title'][:34]:<34} 阅读{a['reads']:>6} 互动{a['engagement_rate']}%")


def cmd_recommend(args, cfg):
    d = _db(cfg)
    recs = recommender.recommend(
        d, cfg, top_n=args.top_n,
        write_kb=not args.no_kb,
        dry_run=not args.no_dry_run,
    )
    if args.json:
        print(json.dumps(recs, ensure_ascii=False, indent=2))
    else:
        print(recommender.format_recs(recs))
    d.close()


def cmd_run(args, cfg):
    d = _db(cfg)
    dry = not args.no_dry_run
    summary = sched_mod.run_job(cfg, d, args.job, dry_run=dry)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    d.close()


def cmd_demo(args, cfg):
    # demo 用独立数据库，避免污染生产数据
    import os as _os
    demo_db = _os.path.join(cfg.data_dir, "kb_demo.db")
    d = dbmod.DB(demo_db)
    print(">>> [demo] 1/4 采集（demo 模式，含 7+ 天样例数据）")
    arts = collector.collect(cfg, d, mode="demo")
    print(f"    生成 {len(arts)} 篇样例文章")
    print(">>> [demo] 2/4 同步到飞书（dry_run 预览）")
    sres = sync.sync_new(cfg, d, categories=None, dry_run=True)
    print(f"    同步预览：{sres}")
    print(">>> [demo] 3/4 近 7 天复盘")
    rep = analyzer.analyze_7day(d, 7)
    _print_analysis(rep, daily=False)
    print(">>> [demo] 4/4 次日选题推荐")
    recs = recommender.recommend(d, cfg, write_kb=False, dry_run=True)
    print(recommender.format_recs(recs))
    print("\n[demo] 完成。数据库位于:", cfg.db_path)
    d.close()


def cmd_scheduler(args, cfg):
    d = _db(cfg)
    dry = not args.no_dry_run
    s = sched_mod.Scheduler(cfg, d, dry_run=dry)
    s.start()


def cmd_watch(args, cfg):
    d = _db(cfg)
    dry = not args.no_dry_run
    sched_mod.watch(cfg, d, interval_min=args.interval, dry_run=dry)


def cmd_status(args, cfg):
    d = _db(cfg)
    arts = d.list_articles()
    recs = d.list_recommendations()
    topics = d.list_topics()
    print(f"数据库：{cfg.db_path}")
    print(f"文章总数：{len(arts)}（已同步飞书 {sum(1 for a in arts if a['synced'])}）")
    print(f"选题推荐：{len(recs)} 条；选题池：{len(topics)} 条")
    last_night = d.last_run("night")
    if last_night:
        print(f"上次 night 任务：{last_night['finished_at']} ({last_night['status']})")
    d.close()


def build_parser():
    p = argparse.ArgumentParser(prog="kb_autosync", description="公众号×飞书 自增长知识库系统")
    p.add_argument("--config", default=None, help="配置路径（默认 config.json）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("collect", help="采集文章")
    sp.add_argument("--mode", default=None, choices=["demo", "mp_api", "url_feed", "local_folder"])
    sp.add_argument("--since", default=None, help="YYYY-MM-DD 起")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("sync", help="同步到飞书")
    sp.add_argument("--all", action="store_true", help="同步全部分类（默认仅 trigger_categories）")
    sp.add_argument("--no-dry-run", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("analyze", help="数据分析")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--daily", action="store_true", help="仅当日梳理")
    sp.add_argument("--date", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("recommend", help="选题推荐")
    sp.add_argument("--top-n", type=int, default=None)
    sp.add_argument("--no-kb", action="store_true", help="不推送到飞书")
    sp.add_argument("--no-dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_recommend)

    sp = sub.add_parser("run", help="执行定时任务")
    sp.add_argument("--job", required=True, choices=["morning", "evening", "night"])
    sp.add_argument("--no-dry-run", action="store_true")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("demo", help="离线跑通整条流水线")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("scheduler", help="常驻定时循环")
    sp.add_argument("--no-dry-run", action="store_true")
    sp.set_defaults(func=cmd_scheduler)

    sp = sub.add_parser("watch", help="轮询同步新文章")
    sp.add_argument("--interval", type=int, default=None)
    sp.add_argument("--no-dry-run", action="store_true")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("status", help="数据库概览")
    sp.set_defaults(func=cmd_status)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = cfgmod.load(args.config) if args.config else cfgmod.load()
    args.func(args, cfg)


if __name__ == "__main__":
    main()
