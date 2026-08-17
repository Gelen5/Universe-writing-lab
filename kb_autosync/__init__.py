"""kb_autosync — 微信公众号 × 飞书知识库 自增长自动化同步与数据分析系统。

核心闭环：
  发布文章 → 采集(Collector) → 入库(SQLite) → 同步飞书(Sync) →
  每日统计(Analyzer) → 7天复盘 → 22:00 选题推荐(Recommender) →
  次日发布 → 回到采集。知识库与数据随时间自我滚雪球。
"""
__version__ = "0.1.0"
