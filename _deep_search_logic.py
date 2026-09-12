"""深度搜索代理核心逻辑（纯函数，独立于 N.E.K.O SDK 与网络，便于测试）

流程：搜索结果卡片解析 → LLM 筛选候选 → 抓取页面正文 → LLM 逐页分析 →
**兑换码与页面原文交叉核对**（防幻觉）→ 汇总报告。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# ── 搜索结果卡片解析（anysearch format_results 的文本格式）─────────

_CARD_NUM_RE = re.compile(r"^(\d+)\.\s*(.+)$")
_URL_LINE_RE = re.compile(r"^\s*(https?://\S+)\s*$")


def parse_search_cards(text: str) -> list[dict[str, Any]]:
    """把 anysearch 的格式化搜索结果解析回结构化卡片。

    格式形如::

        1. 标题
           https://url
           描述（可多行/可无）
           来源: xxx · 质量分 0.85

    解析失败返回空表，由上层降级。
    """
    cards: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line in (text or "").splitlines():
        numbered = _CARD_NUM_RE.match(line.strip())
        url_only = _URL_LINE_RE.match(line)
        if url_only and current is not None:
            current["url"] = url_only.group(1)
            continue
        if numbered:
            if current and current.get("url"):
                cards.append(current)
            current = {"index": int(numbered.group(1)), "title": numbered.group(2).strip(), "url": "", "desc": ""}
            continue
        if current is not None:
            stripped = line.strip()
            if not stripped or stripped.startswith("来源:") or stripped.startswith("🔍"):
                continue
            if current["desc"]:
                current["desc"] += " " + stripped
            else:
                current["desc"] = stripped
    if current and current.get("url"):
        cards.append(current)
    return cards


# ── 网页正文提取 ─────────────────────────────────────────────────

_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg|iframe|head)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")
_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|apos|nbsp|#\d+|#x[0-9a-fA-F]+);")

_BASIC_ENTITIES = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}


def _decode_entity(match: re.Match) -> str:
    body = match.group(1)
    if body in _BASIC_ENTITIES:
        return _BASIC_ENTITIES[body]
    if body.startswith("#x") or body.startswith("#X"):
        try:
            return chr(int(body[2:], 16))
        except ValueError:
            return match.group(0)
    if body.startswith("#"):
        try:
            return chr(int(body[1:]))
        except ValueError:
            return match.group(0)
    return match.group(0)


def html_to_text(html: str, max_chars: int = 6000) -> str:
    """把 HTML 压成可读正文：去脚本/样式/注释/标签，解实体，压空白。"""
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub("\n", text)
    text = _ENTITY_RE.sub(_decode_entity, text)
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…（已截断）"
    return text


def decode_page_body(body: bytes, content_type: str = "") -> str:
    """按响应头/meta/试探顺序解码 HTML 字节。"""
    charset = ""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    candidates = [charset.lower()] if charset else []
    head = body[:2048].decode("ascii", errors="ignore")
    meta = re.search(r'charset=["\']?([\w\-]+)', head, re.IGNORECASE)
    if meta:
        candidates.append(meta.group(1).lower())
    candidates += ["utf-8", "gbk", "big5"]
    seen: set[str] = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


# ── 兑换码候选与交叉核对（防幻觉的关键）───────────────────────────

# 游戏兑换码的常见形态：8-14 位大写字母数字混合（原神/星穹铁道/绝区零都是这个范围）
_CODE_RE = re.compile(r"\b(?=[A-Z0-9-]{8,14}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{8,14}\b")
# 明显不是兑换码的词：全字母无数字的英文单词、常见干扰词
_CODE_STOPWORDS = {
    "GENERATION", "REDEEMCODE", "PRIMOGEMS", "MOSAIC", "PASSWORD", "COPYRIGHT",
    "HOMEDIRECT", "JAVASCRIPT", "CHARSET", "VERSION", "DOWNGRADE",
}


def extract_code_candidates(text: str) -> list[str]:
    """从文本中抽取形似游戏兑换码的 token（只做候选，须再交叉核对）。"""
    found: list[str] = []
    seen: set[str] = set()
    for token in _CODE_RE.findall((text or "").upper()):
        if token in _CODE_STOPWORDS or token in seen:
            continue
        # 前缀命中干扰词（如 VERSION2024）也算干扰
        if any(token.startswith(word) for word in _CODE_STOPWORDS):
            continue
        # 至少 2 个字母 + 2 个数字，避免误伤普通编号
        letters = sum(1 for ch in token if ch.isalpha())
        digits = sum(1 for ch in token if ch.isdigit())
        if letters >= 2 and digits >= 2:
            seen.add(token)
            found.append(token)
    return found


def cross_check_codes(codes: list[str], page_text: str) -> list[str]:
    """只保留在页面原文中**逐字出现**的码——模型说的不算，原文说了算。"""
    if not codes:
        return []
    haystack = (page_text or "").upper()
    return [code for code in codes if code in haystack]


# ── LLM 提示词与输出解析 ─────────────────────────────────────────

def build_select_prompt(question: str, cards: list[dict[str, Any]], max_pick: int = 4) -> str:
    """筛选提示词：让模型按标题/摘要判断哪几条最可能藏着答案。"""
    lines = [
        f"{i}. {c.get('title', '')}\n   URL: {c.get('url', '')}\n   摘要: {c.get('desc', '')[:200]}"
        for i, c in enumerate(cards, 1)
    ]
    return f"""你是搜索代理的筛选引擎。用户问题：{question}

搜索结果（只有标题/URL/摘要，还没看页面内容）：
{chr(10).join(lines) or "（无结果）"}

请严格只返回 JSON：
{{"direct_answer": "若仅凭摘要就能确定答案就写出来，否则留空", "selected": [{{"index": 1, "why": "为什么点它"}}]}}

规则：
- selected 最多 {max_pick} 个，按可能性排序；标题含"最新/兑换码/直播/日期新鲜"的优先
- 摘要与问题明显无关的不要选
- 如果所有结果都不可能包含答案，selected 留空数组
"""


def parse_selection(raw: Any, cards: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """解析筛选结果：返回 (选中的卡片, direct_answer)。"""
    if isinstance(raw, str):
        try:
            from ._command_logic import extract_json_object  # 宿主包内
        except ImportError:  # 独立加载（测试）
            from neko_natural_command_logic import extract_json_object  # type: ignore
        raw_json = extract_json_object(raw)
        if not raw_json:
            return [], ""
        try:
            raw = json.loads(raw_json)
        except json.JSONDecodeError:
            return [], ""
    if not isinstance(raw, dict):
        return [], ""
    direct = str(raw.get("direct_answer") or "").strip()
    selected: list[dict[str, Any]] = []
    for item in raw.get("selected") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        # 模型返回的是 1-based 序号（对应卡片编号）
        for card in cards:
            if card.get("index") == idx and card not in selected:
                selected.append(card)
                break
    return selected, direct


def build_page_prompt(question: str, card: dict[str, Any], page_text: str) -> str:
    """逐页分析提示词：判断相关性并抽取答案/兑换码。"""
    return f"""你是搜索代理的阅读引擎。用户问题：{question}

正在阅读网页：
标题：{card.get('title', '')}
URL：{card.get('url', '')}

页面正文：
{page_text[:5500]}

请严格只返回 JSON：
{{"relevant": true/false, "answer": "若该页包含答案，完整摘录相关原文片段", "codes": ["页面里出现的兑换码原文"], "summary": "一句话概括这页讲了什么"}}

规则：
- codes 只能抄录页面正文里**逐字出现**的兑换码，一个都不许编造；没有就给空数组
- answer 引用原文，不要自己改写码的字母数字
- 页面与问题无关时 relevant=false，其余字段留空
"""


def parse_page_analysis(raw: Any) -> dict[str, Any]:
    """解析逐页分析输出；解析失败返回不相关空结果（宁可放弃不硬编）。"""
    if isinstance(raw, str):
        try:
            from ._command_logic import extract_json_object
        except ImportError:
            from neko_natural_command_logic import extract_json_object  # type: ignore
        raw_json = extract_json_object(raw)
        if not raw_json:
            return {"relevant": False, "answer": "", "codes": [], "summary": ""}
        try:
            raw = json.loads(raw_json)
        except json.JSONDecodeError:
            return {"relevant": False, "answer": "", "codes": [], "summary": ""}
    if not isinstance(raw, dict):
        return {"relevant": False, "answer": "", "codes": [], "summary": ""}
    codes = raw.get("codes")
    return {
        "relevant": bool(raw.get("relevant")),
        "answer": str(raw.get("answer") or "").strip(),
        "codes": [str(c).strip().upper() for c in codes] if isinstance(codes, list) else [],
        "summary": str(raw.get("summary") or "").strip(),
    }


def build_final_report(question: str, analyses: list[dict[str, Any]], catgirl_name: str = "猫娘") -> str:
    """本地汇总最终报告：只引用核实过的内容，不经过再创作。"""
    verified: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for item in analyses:
        card = item.get("card", {})
        analysis = item.get("analysis", {})
        if item.get("verified_codes"):
            for code in item["verified_codes"]:
                verified.setdefault(code, {"url": card.get("url", ""), "title": card.get("title", "")})
        elif analysis.get("relevant"):
            notes.append(f"- {card.get('title', '')}：{analysis.get('summary') or analysis.get('answer', '')[:80]}")
    if not verified:
        lines = [f"呜…本喵翻了 {len(analyses)} 个页面，没有找到能逐字核实的兑换码喵。"]
        if notes:
            lines.append("比较接近的页面：")
            lines.extend(notes[:3])
        lines.append("可能是码已过期被撤，或藏在视频/图片里。要不再试试别的关键词喵？")
        return "\n".join(lines)
    lines = [f"找到啦喵！关于「{question}」，本喵核实的兑换码："]
    for code, source in verified.items():
        lines.append(f"🎁 {code}")
        lines.append(f"   来源：{source['title'][:50]} {source['url'][:70]}")
    lines.append("（每个码都和页面原文逐字核对过；兑换请尽快，码会过期喵～）")
    return "\n".join(lines)
