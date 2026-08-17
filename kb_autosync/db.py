"""SQLite 数据层 —— 整个自增长系统的"记忆体"。

五张表：
  articles            已采集文章（含同步状态）
  article_daily_stats 每日滚动数据（阅读/点赞/评论/分享）
  runs                每次任务执行记录（早/晚/夜）
  recommendations     每晚生成的选题推荐
  topics_pool         选题池（idea→approved→published 闭环）
"""
import os
import json
import sqlite3
import time
from typing import Optional, List, Dict, Any


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _unjs(s: Optional[str]):
    if not s:
        return []
    try:
        return json.loads(s)
    except Exception:
        return []


class DB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        c = self.conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                author TEXT,
                publish_time TEXT,
                category TEXT DEFAULT '未分类',
                tags TEXT DEFAULT '[]',
                content_md TEXT,
                created_at TEXT,
                feishu_node TEXT,
                synced INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS article_daily_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                date TEXT,
                reads INTEGER DEFAULT 0,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                collected_at TEXT,
                UNIQUE(article_id, date)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT,
                started_at TEXT,
                finished_at TEXT,
                status TEXT,
                summary TEXT
            );
            CREATE TABLE IF NOT EXISTS recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                title TEXT,
                rationale TEXT,
                sources TEXT DEFAULT '[]',
                score REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                published_article_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS topics_pool (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                status TEXT DEFAULT 'idea',
                source_rec_id INTEGER,
                created_at TEXT,
                note TEXT
            );
            """
        )
        c.commit()

    # ───────────────────────── articles ─────────────────────────
    def upsert_article(self, article: Dict[str, Any]) -> int:
        cur = self.conn.cursor()
        url = article.get("url")
        cur.execute("SELECT id FROM articles WHERE url=?", (url,))
        row = cur.fetchone()
        if row:
            aid = row["id"]
            cur.execute(
                """UPDATE articles SET title=?, author=?, publish_time=?, category=?,
                   tags=?, content_md=?, created_at=? WHERE id=?""",
                (
                    article.get("title"),
                    article.get("author"),
                    article.get("publish_time"),
                    article.get("category", "未分类"),
                    _js(article.get("tags", [])),
                    article.get("content_md"),
                    _now(),
                    aid,
                ),
            )
            self.conn.commit()
            return aid
        cur.execute(
            """INSERT INTO articles
               (url, title, author, publish_time, category, tags, content_md, created_at, synced)
               VALUES (?,?,?,?,?,?,?,?,0)""",
            (
                url,
                article.get("title"),
                article.get("author"),
                article.get("publish_time"),
                article.get("category", "未分类"),
                _js(article.get("tags", [])),
                article.get("content_md"),
                _now(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_article(self, aid: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM articles WHERE id=?", (aid,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_article_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM articles WHERE url=?", (url,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_articles(self, since: Optional[str] = None, category: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM articles"
        where, args = [], []
        if since:
            where.append("publish_time >= ?")
            args.append(since)
        if category:
            where.append("category = ?")
            args.append(category)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY publish_time DESC"
        cur = self.conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]

    def unsynced(self, trigger_categories: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if trigger_categories:
            ph = ",".join("?" * len(trigger_categories))
            cur = self.conn.execute(
                f"SELECT * FROM articles WHERE synced=0 AND category IN ({ph}) ORDER BY id",
                trigger_categories,
            )
        else:
            cur = self.conn.execute("SELECT * FROM articles WHERE synced=0 ORDER BY id")
        return [dict(r) for r in cur.fetchall()]

    def mark_synced(self, aid: int, feishu_node: Dict[str, Any]):
        self.conn.execute(
            "UPDATE articles SET synced=1, feishu_node=? WHERE id=?",
            (_js(feishu_node), aid),
        )
        self.conn.commit()

    # ───────────────────── article_daily_stats ─────────────────────
    def add_daily_stat(self, article_id: int, date: str, stats: Dict[str, int]) -> None:
        self.conn.execute(
            """INSERT INTO article_daily_stats
               (article_id, date, reads, likes, comments, shares, collected_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(article_id, date) DO UPDATE SET
               reads=excluded.reads, likes=excluded.likes,
               comments=excluded.comments, shares=excluded.shares,
               collected_at=excluded.collected_at""",
            (
                article_id,
                date,
                stats.get("reads", 0),
                stats.get("likes", 0),
                stats.get("comments", 0),
                stats.get("shares", 0),
                _now(),
            ),
        )
        self.conn.commit()

    def get_latest_stat(self, article_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM article_daily_stats WHERE article_id=? ORDER BY date DESC LIMIT 1",
            (article_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_stats_in_range(self, start: str, end: str) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM article_daily_stats WHERE date>=? AND date<=? ORDER BY date",
            (start, end),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_daily_snapshot(self, date: str) -> Dict[str, Any]:
        """某一天发布的文章 + 当天累计数据快照。"""
        articles = self.list_articles(since=date)
        # 只保留当天发布的（publish_time 以 YYYY-MM-DD 开头）
        day_arts = [a for a in articles if (a.get("publish_time") or "").startswith(date)]
        rows = self.conn.execute(
            """SELECT a.id, a.title, a.category, s.reads, s.likes, s.comments, s.shares
               FROM articles a LEFT JOIN article_daily_stats s
               ON s.article_id=a.id AND s.date=?
               WHERE a.publish_time LIKE ?""",
            (date, date + "%"),
        ).fetchall()
        return {
            "date": date,
            "published_count": len(day_arts),
            "articles": [dict(r) for r in rows],
            "totals": {
                "reads": sum((r["reads"] or 0) for r in rows),
                "likes": sum((r["likes"] or 0) for r in rows),
                "comments": sum((r["comments"] or 0) for r in rows),
                "shares": sum((r["shares"] or 0) for r in rows),
            },
        }

    # ───────────────────────── runs ─────────────────────────
    def record_run(self, job_type: str, started_at: str, status: str, summary: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (job_type, started_at, finished_at, status, summary) VALUES (?,?,?,?,?)",
            (job_type, started_at, _now(), status, _js(summary)),
        )
        self.conn.commit()
        return cur.lastrowid

    def last_run(self, job_type: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM runs WHERE job_type=? ORDER BY finished_at DESC LIMIT 1", (job_type,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ───────────────────── recommendations / topics ─────────────────────
    def add_recommendation(self, rec: Dict[str, Any]) -> int:
        cur = self.conn.execute(
            """INSERT INTO recommendations
               (date, title, rationale, sources, score, status, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                rec.get("date"),
                rec.get("title"),
                rec.get("rationale"),
                _js(rec.get("sources", [])),
                rec.get("score", 0),
                rec.get("status", "pending"),
                _now(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_recommendations(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        if date:
            cur = self.conn.execute("SELECT * FROM recommendations WHERE date=? ORDER BY score DESC", (date,))
        else:
            cur = self.conn.execute("SELECT * FROM recommendations ORDER BY id DESC")
        out = []
        for r in cur.fetchall():
            d = dict(r)
            d["sources"] = _unjs(d.get("sources"))
            out.append(d)
        return out

    def add_topic(self, topic: str, status: str = "idea", source_rec_id: Optional[int] = None, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO topics_pool (topic, status, source_rec_id, created_at, note) VALUES (?,?,?,?,?)",
            (topic, status, source_rec_id, _now(), note),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_topics(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            cur = self.conn.execute("SELECT * FROM topics_pool WHERE status=? ORDER BY id DESC", (status,))
        else:
            cur = self.conn.execute("SELECT * FROM topics_pool ORDER BY id DESC")
        return [dict(r) for r in cur.fetchall()]

    def mark_topic_published(self, topic_id: int, article_id: int) -> None:
        self.conn.execute(
            "UPDATE topics_pool SET status='published' WHERE id=?", (topic_id,)
        )
        self.conn.execute(
            "UPDATE recommendations SET status='published', published_article_id=? WHERE id=(SELECT source_rec_id FROM topics_pool WHERE id=?)",
            (article_id, topic_id),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
