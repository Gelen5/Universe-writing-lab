#!/usr/bin/env python3
"""飞书知识库同步 —— 自包含实现（从 wechat-publisher-ultimate 抽取，无外部依赖）。

本模块是 kb_autosync 的一部分，**不依赖任何外部 skill / 个人路径**，clone 后即可使用。

对外 API：
  - FeishuAPI：tenant_access_token（带缓存 + 5 分钟缓冲、失效刷新）、
    建知识库节点、块写入、统一重试。
  - sync_file(opts)：把一个带 YAML frontmatter 的 .md 文件同步到飞书知识库。
  - retry_pending(log_dir)：重试上一次失败的待同步队列。

opts（sync_file 参数）说明：
  file               必填，待同步的 .md 文件路径（可带 frontmatter）
  app_id / app_secret 飞书自建应用凭证（也可用环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET）
  space_id           知识库 URL 中的节点 token（不是数字 space_id）
  parent_node_token  可选，指定归档父节点；留空则自动解析为 space_id 对应节点
  categories         可选 {分类名: 节点token}，命中则归档到对应目录
  default_category   可选，默认 "未分类"
  category           可选，本篇文章的分类（用于路由）
  title              可选，覆盖标题
  dry_run            True 时只预览不调 API
  use_convert        默认 False（块写入更稳定）；True 尝试 markdown 全文 convert
  log_dir            可选，日志/待重试队列目录，默认 <staging 同级>/.cache

注意：convert 接口在飞书当前 API 面下不稳定，默认走块写入（已验证可靠）。
"""

import os
import re
import json
import time
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

FEISHU_DOMAIN = "https://open.feishu.cn"
TOKEN_BUFFER = 5 * 60  # 秒

# 可重试错误码（token 失效 / 频率限制 / 系统忙）
RETRYABLE_CODES = {99991663, 40001, 40003, 99991672, 99992413}


class FeishuError(Exception):
    """飞书业务错误（code != 0）"""

    def __init__(self, code: int, msg: str):
        super().__init__(f"飞书API错误: {msg} (code={code})")
        self.code = code


# ───────────────────────── markdown → 飞书块 ─────────────────────────

def _text_run(content: str) -> Dict[str, Any]:
    return {"text_run": {"content": content}}


def markdown_to_blocks(md: str) -> List[Dict[str, Any]]:
    """将 markdown 解析为飞书 docx 块数组（标题/段落/列表/代码/引用/分割线）。"""
    lines = md.split("\n")
    blocks: List[Dict[str, Any]] = []
    list_buffer: Optional[Dict[str, Any]] = None

    def flush_list():
        nonlocal list_buffer
        if not list_buffer:
            return
        for item in list_buffer["items"]:
            if list_buffer["type"] == "bullet":
                blocks.append({"block_type": 12, "bullet": {"elements": [_text_run(item)]}})
            else:
                blocks.append({"block_type": 13, "ordered": {"elements": [_text_run(item)]}})
        list_buffer = None

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip().startswith("```"):
            flush_list()
            code_lines: List[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append({
                "block_type": 14,
                "code": {"style": {"language": 1}, "elements": [_text_run("\n".join(code_lines))]},
            })
            continue
        if line.startswith("> "):
            flush_list()
            blocks.append({"block_type": 15, "quote": {"elements": [_text_run(line[2:])]}})
            i += 1
            continue
        if line.strip() == "---":
            flush_list()
            blocks.append({"block_type": 22, "divider": {}})
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            flush_list()
            level = len(h.group(1))
            content = h.group(2)
            blocks.append({
                "block_type": 2 + level,
                f"heading{level}": {"elements": [_text_run(content)]},
            })
            i += 1
            continue
        ul = re.match(r"^\s*[-*]\s+(.*)$", line)
        ol = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if ul or ol:
            t = "bullet" if ul else "ordered"
            content = (ul or ol).group(1)
            if not list_buffer or list_buffer["type"] != t:
                flush_list()
                list_buffer = {"type": t, "items": []}
            list_buffer["items"].append(content)
            i += 1
            continue
        if line.strip() == "":
            flush_list()
            i += 1
            continue
        flush_list()
        para = [line]
        i += 1
        while i < n and lines[i].strip() != "" and not re.match(
            r"^(#{1,6}\s|> |^\s*[-*]\s|^\s*\d+\.\s|^\s*```|^\s*---\s*$)", lines[i]
        ):
            para.append(lines[i])
            i += 1
        blocks.append({"block_type": 2, "text": {"elements": [_text_run("\n".join(para))]}})
    flush_list()
    return blocks


# ───────────────────────── 飞书 API 封装 ─────────────────────────

class FeishuAPI:
    def __init__(self, app_id: str = "", app_secret: str = "", parent_node_token: str = ""):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self.parent_node_token = parent_node_token or os.getenv("FEISHU_PARENT_NODE", "")
        self._token_cache: Optional[Dict[str, Any]] = None
        self.session = requests.Session()
        self.session.timeout = 20

    def get_tenant_access_token(self) -> str:
        now = time.time()
        if self._token_cache and self._token_cache["expires_at"] > now + TOKEN_BUFFER:
            return self._token_cache["token"]
        resp = self.session.post(
            f"{FEISHU_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuError(data.get("code", -1), data.get("msg", "未知错误"))
        self._token_cache = {
            "token": data["tenant_access_token"],
            "expires_at": now + data.get("expire", 7200),
        }
        return data["tenant_access_token"]

    def _request(self, method: str, url: str, body: Optional[Dict] = None, attempts: int = 3):
        token = self.get_tenant_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = self.session.request(method, url, headers=headers, json=body)
            data = resp.json()
            if data.get("code") == 0:
                return data
            if data.get("code") in RETRYABLE_CODES and attempts > 1:
                if data.get("code") in {99991663, 40001, 40003}:
                    self._token_cache = None
                return self._request(method, url, body, attempts - 1)
            raise FeishuError(data.get("code", -1), data.get("msg", ""))
        except FeishuError:
            raise
        except Exception as e:
            if attempts > 1 and isinstance(e, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
                time.sleep(0.8 * (4 - attempts))
                return self._request(method, url, body, attempts - 1)
            raise

    def create_wiki_doc(self, space_id: str, parent_node_token: str, title: str) -> Dict[str, str]:
        # 端点：POST /wiki/v2/spaces/{space_id}/nodes
        data = self._request(
            "POST",
            f"{FEISHU_DOMAIN}/open-apis/wiki/v2/spaces/{space_id}/nodes",
            {"parent_node_token": parent_node_token, "obj_type": "docx", "node_type": "origin", "title": title[:100]},
        )
        node = (data.get("data") or {}).get("node") or {}
        document_id = node.get("obj_token")
        node_token = node.get("node_token")
        if not document_id:
            raise FeishuError(-1, "创建知识库节点未返回 obj_token")
        return {
            "node_token": node_token,
            "document_id": document_id,
            "url": f"https://www.feishu.cn/wiki/{node_token}",
        }

    def resolve_space(self, space_id_token: str):
        """给定 wiki URL 里的节点 token，反查数字 space_id，并返回该节点自身 token 作父节点。

        返回 (numeric_space_id, parent_node_token)。
        """
        try:
            data = self._request(
                "GET",
                f"{FEISHU_DOMAIN}/open-apis/wiki/v2/spaces/get_node?token={quote(space_id_token)}",
            )
            node = (data.get("data") or {}).get("node") or {}
            sid = node.get("space_id")
            nt = node.get("node_token") or space_id_token
            if sid:
                return sid, nt
        except Exception:
            pass
        parent = self.parent_node_token or os.getenv("FEISHU_PARENT_NODE", "")
        if parent:
            return space_id_token, parent
        raise FeishuError(
            -1,
            "无法解析知识空间：feishu.space_id 应为知识库 URL 中的节点 token（非数字 space_id）；"
            "若填的是数字 space_id，请同时配置 parent_node_token",
        )

    def convert_markdown(self, document_id: str, markdown: str):
        self._request(
            "POST",
            f"{FEISHU_DOMAIN}/open-apis/docx/v1/documents/{document_id}/convert",
            {"content": markdown, "content_type": "markdown"},
        )

    def append_blocks(self, document_id: str, children: List[Dict], batch_size: int = 50) -> List[str]:
        ids: List[str] = []
        for start in range(0, len(children), batch_size):
            batch = children[start:start + batch_size]
            data = self._request(
                "POST",
                f"{FEISHU_DOMAIN}/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children?document_revision_id=-1",
                {"children": batch, "index": -1},
            )
            for c in (data.get("data") or {}).get("children") or []:
                if c.get("block_id"):
                    ids.append(c["block_id"])
        return ids

    def write_blocks_from_markdown(self, document_id: str, markdown: str) -> List[str]:
        blocks = markdown_to_blocks(markdown)
        return self.append_blocks(document_id, blocks)


# ───────────────────────── 文章解析 / 格式化 ─────────────────────────

def parse_frontmatter(content: str):
    """解析 YAML frontmatter，返回 (frontmatter: dict, body: str)"""
    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$", content)
    if not m:
        return {}, content
    fm: Dict[str, Any] = {}
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        for line in m.group(1).split("\n"):
            kv = re.match(r"^(\w[\w_]*):\s*(.*)$", line)
            if kv:
                fm[kv.group(1)] = kv.group(2).strip().strip('"').strip("'")
    return fm, m.group(2)


def html_to_markdown(html: str) -> str:
    try:
        import html2text
        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_links = False
        h.ignore_images = False
        return h.handle(html).strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def preprocess_images(md: str) -> str:
    def repl(_m):
        alt = _m.group(1) or ""
        url = _m.group(2)
        label = alt.strip() if alt.strip() else "图片"
        return f"🖼️ {label}：{url}"
    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, md)


def build_feishu_markdown(fm: Dict[str, Any], body_md: str, category_label: str) -> str:
    title = fm.get("title") or "未命名文章"
    author = fm.get("author") or "—"
    date = fm.get("date") or time.strftime("%Y-%m-%d")
    tags = fm.get("tags")
    if isinstance(tags, list):
        tags = "、".join(str(t) for t in tags)
    tags = tags or "—"

    header = (
        f"# {title}\n\n"
        f"> **来源**：微信公众号文章 · 由 kb_autosync 自动沉淀\n"
        f"> **作者**：{author}\n"
        f"> **发布日期**：{date}\n"
        f"> **标签**：{tags}\n"
        f"> **分类**：{category_label}\n\n"
        "---\n\n"
    )
    body = preprocess_images(body_md.strip())
    return header + body + "\n"


def resolve_target_node(opts: Dict[str, Any], fm: Dict[str, Any]) -> Dict[str, str]:
    categories = opts.get("categories") or {}
    default_category = opts.get("default_category") or "未分类"
    key = opts.get("category") or fm.get("category")
    if key and categories.get(key):
        return {"node_token": categories[key], "category_label": key}
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split("、") if t.strip()]
    for t in tags:
        if categories.get(t):
            return {"node_token": categories[t], "category_label": t}
    return {
        "node_token": opts.get("parent_node_token") or "",
        "category_label": default_category,
    }


# ───────────────────────── 日志 / 待重试队列 ─────────────────────────

def _log_path(log_dir: str, name: str) -> str:
    return os.path.join(log_dir, name)


def _append_log(log_dir: str, entry: Dict[str, Any]):
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = _log_path(log_dir, "feishu_sync_log.json")
        arr = []
        if os.path.exists(log_path):
            try:
                arr = json.load(open(log_path, "r", encoding="utf-8"))
            except Exception:
                arr = []
        arr.append(entry)
        if len(arr) > 500:
            arr = arr[-500:]
        json.dump(arr, open(log_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("写入飞书同步日志失败: %s", e)


def _enqueue_pending(log_dir: str, item: Dict[str, Any]):
    try:
        os.makedirs(log_dir, exist_ok=True)
        p_path = _log_path(log_dir, "feishu_pending.json")
        arr = []
        if os.path.exists(p_path):
            try:
                arr = json.load(open(p_path, "r", encoding="utf-8"))
            except Exception:
                arr = []
        item["enqueuedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        arr.append(item)
        json.dump(arr, open(p_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        logger.warning("已加入待重试队列: %s", item.get("file"))
    except Exception as e:
        logger.warning("写入待重试队列失败: %s", e)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ───────────────────────── 主入口 ─────────────────────────

def sync_file(opts: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """同步单篇文章到飞书知识库。返回节点信息 dict 或 None。"""
    file = opts["file"]
    if not os.path.exists(file):
        logger.error("文件不存在: %s", file)
        return None

    log_dir = opts.get("log_dir") or os.path.join(os.path.dirname(os.path.abspath(file)), ".cache")

    ext = os.path.splitext(file)[1].lower()
    fm: Dict[str, Any] = {}
    body_md = ""

    if ext == ".md":
        raw = open(file, "r", encoding="utf-8").read()
        fm, body_md = parse_frontmatter(raw)
    elif ext in (".html", ".htm"):
        sibling = re.sub(r"\.(html?|htm)$", ".md", file, flags=re.I)
        if sibling != file and os.path.exists(sibling):
            raw = open(sibling, "r", encoding="utf-8").read()
            fm, body_md = parse_frontmatter(raw)
        else:
            raw = open(file, "r", encoding="utf-8").read()
            fm, body_md = parse_frontmatter(raw)
            body_md = html_to_markdown(body_md)
    else:
        logger.error("不支持的文件格式: %s", ext)
        return None

    if opts.get("title"):
        fm["title"] = opts["title"]

    target = resolve_target_node(opts, fm)
    final_md = build_feishu_markdown(fm, body_md, target["category_label"])
    title = fm.get("title") or os.path.splitext(os.path.basename(file))[0]

    if opts.get("dry_run"):
        print("=== [dry-run] 飞书同步预览 ===")
        print(f"空间: {opts.get('space_id') or '(未配置)'}")
        print(f"目标节点: {target['node_token'] or '(将自动归档到空间根目录)'} (分类: {target['category_label']})")
        print(f"标题: {title}")
        print(f"正文长度: {len(final_md)} 字符")
        print("--- 文档开头 ---")
        print(final_md[:600])
        return None

    app_id = opts.get("app_id") or os.getenv("FEISHU_APP_ID", "")
    app_secret = opts.get("app_secret") or os.getenv("FEISHU_APP_SECRET", "")
    space_id = opts.get("space_id") or ""
    if not app_id or not app_secret:
        msg = "未配置飞书凭证（config.json 的 feishu.app_id/app_secret 或环境变量 FEISHU_APP_ID/FEISHU_APP_SECRET）"
        logger.error(msg)
        _append_log(log_dir, {"time": _now(), "file": file, "title": title, "status": "failed", "error": msg})
        return None
    if not space_id:
        msg = "未配置飞书知识空间（space_id）"
        logger.error(msg)
        _append_log(log_dir, {"time": _now(), "file": file, "title": title, "status": "failed", "error": msg})
        return None

    api = FeishuAPI(app_id, app_secret, parent_node_token=opts.get("parent_node_token", ""))
    parent_token = target["node_token"] or space_id
    numeric_space_id = None
    try:
        numeric_space_id, parent_token = api.resolve_space(space_id)
        print(f"已解析知识空间 space_id={numeric_space_id}，归档到节点: {parent_token}")
    except Exception as e:
        msg = f"无法解析知识空间（请确认 feishu.space_id 为知识库 URL 中的节点 token，且应用已加入该知识空间）：{e}"
        logger.error(msg)
        _append_log(log_dir, {"time": _now(), "file": file, "title": title, "status": "failed", "error": msg})
        return None

    node = None
    last_err = None
    for attempt in range(1, 3):
        try:
            node = api.create_wiki_doc(numeric_space_id, parent_token, title)
            if opts.get("use_convert", False):
                try:
                    api.convert_markdown(node["document_id"], final_md)
                except Exception as conv_err:
                    logger.warning("convert 写入失败，回退手动块写入: %s", conv_err)
                    api.write_blocks_from_markdown(node["document_id"], final_md)
            else:
                api.write_blocks_from_markdown(node["document_id"], final_md)
            break
        except Exception as e:
            last_err = e
            logger.warning("飞书同步第 %s 次尝试失败: %s", attempt, e)
            api._token_cache = None

    if node:
        _append_log(log_dir, {
            "time": _now(), "file": file, "title": title, "status": "success",
            "url": node["url"], "documentId": node["document_id"], "nodeToken": node["node_token"],
            "category": target["category_label"], "targetNode": parent_token,
        })
        logger.info("已同步到飞书知识库: %s", node["url"])
        return node

    code = last_err.code if isinstance(last_err, FeishuError) else None
    _append_log(log_dir, {
        "time": _now(), "file": file, "title": title, "status": "failed",
        "category": target["category_label"], "targetNode": parent_token,
        "error": str(last_err), "errorCode": code,
    })
    _enqueue_pending(log_dir, {
        "file": file, "title": title, "spaceId": space_id,
        "parentNodeToken": parent_token, "category": target["category_label"],
    })
    logger.error("飞书同步失败（已记录日志与待重试队列，不影响文章交付）")
    return None


def retry_pending(log_dir: str) -> None:
    p_path = os.path.join(log_dir, "feishu_pending.json")
    if not os.path.exists(p_path):
        print("没有待重试的飞书同步任务")
        return
    try:
        arr = json.load(open(p_path, "r", encoding="utf-8"))
    except Exception:
        logger.error("待重试队列损坏")
        return
    if not arr:
        print("待重试队列为空")
        return
    remaining = []
    for item in arr:
        print(f"重试: {item.get('file')}")
        node = sync_file({
            "file": item["file"], "space_id": item.get("spaceId"),
            "parent_node_token": item.get("parentNodeToken"), "category": item.get("category"),
            "title": item.get("title"),
        })
        if not node:
            remaining.append(item)
    json.dump(remaining, open(p_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"重试完成：成功 {len(arr) - len(remaining)} 个，剩余 {len(remaining)} 个")
