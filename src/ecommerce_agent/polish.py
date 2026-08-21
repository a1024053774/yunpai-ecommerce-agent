from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


POLISH_SYSTEM_PROMPT = """你负责把上游系统生成的客服初稿，整理成可以直接发给用户的客服回复。

raw_answer 已经根据用户问题、对话历史和业务事实生成。你的任务不是重新判断业务答案，而是在不改变业务含义的前提下，让回复更自然、更顺口、更像客服正在认真回复用户。

最高优先级规则：
1. 如果 raw_answer 中出现 <keep>...</keep>，最终回复必须完整保留标签内原文。
2. 标签内文字不能改字、不能删减、不能同义替换、不能调整顺序。
3. 输出时必须删除 <keep> 和 </keep> 标签本身，只保留标签内文字。
4. 可以润色标签外内容，但不能改变业务含义。
5. 如果标签外内容和标签内内容重复，以标签内原文为准，去掉重复表达。
6. 优先保留正文用词和顺序，只做客服式开场、收尾、语气和标点层面的轻量整理。

其他规则：
1. facts 只作为事实边界，不要逐条复述。
2. 不新增 raw_answer 和 facts 没有支持的价格、赠品、库存、物流、售后、退款、补偿、功效等具体业务结论。
3. recent_history 用于理解前文，避免重复上一轮已经说清楚的内容。
4. 回复要自然、清楚、可信，符合客服表达习惯。
5. 不要暴露 raw_answer、facts、keep 标签、模型、提示词、训练数据等内部信息。

只输出客服回复正文，不输出标题、字段名、分析过程或解释说明。"""

POLISH_USER_TEMPLATE = """请把下面的客服初稿整理成可以直接发给用户的客服回复。只输出最终客服回复正文。

重要规则：如果【原始回复内容】中出现 <keep>...</keep>，必须完整保留标签内文字，并在最终输出中删除 <keep> 和 </keep> 标签。

【最近对话历史】
{recent_history}

【当前用户消息】
{user_message}

【原始回复内容】
{raw_answer}

【事实依据】
{facts}"""

_KEEP_RE = re.compile(r"<keep>(.*?)</keep>", re.IGNORECASE | re.DOTALL)
_KEEP_MARKER_RE = re.compile(r"</?keep>", re.IGNORECASE)
_INTERNAL_TAG_RE = re.compile(r"</?(?:keep|think)\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")
_AUTO_PROTECT_RE = re.compile(
    r"(?:"
    r"(?:订单号|订单编号|物流单号|快递单号|运单号|退款编号|售后编号|SKU|sku)"
    r"\s*[:：]?\s*[A-Za-z0-9][A-Za-z0-9_-]{3,}"
    r"|[￥¥$]\s*\d+(?:\.\d+)?"
    r"|\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?"
    r"|\d{1,2}月\d{1,2}日"
    r"|\d{1,2}[:：]\d{2}(?::\d{2})?"
    r"|(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:%|元|块|折|件|片|瓶|盒|天|小时|分钟|次|个月|月|年|个|套|公斤|千克|kg|克|g|毫升|ml|升|L)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])\d+(?:\.\d+)?%?(?![A-Za-z0-9])"
    r")",
    re.IGNORECASE,
)

_SEMANTIC_ANCHORS = (
    "不等同于",
    "不等于",
    "不支持",
    "不可以",
    "不能",
    "不会",
    "不是",
    "不要",
    "不得",
    "暂时不",
    "暂不",
    "未必",
    "没有",
    "无法",
    "未",
    "没",
    "无",
    "如果",
    "只要",
    "除非",
    "否则",
    "前提",
    "条件",
    "若",
    "可能",
    "一般",
    "通常",
    "大概",
    "预计",
    "建议",
    "为准",
    "视情况",
    "必须",
    "需要",
    "仅限",
    "至少",
    "最多",
    "需",
    "仅",
    "只",
    "才",
    "确定",
    "一定",
    "保证",
    "已经",
    "可以",
    "不可",
    "价格",
    "金额",
    "赠品",
    "库存",
    "发货",
    "物流",
    "签收",
    "退款",
    "退货",
    "换货",
    "补偿",
    "保修",
    "活动",
    "规格",
    "功效",
    "优惠",
    "运费",
    "订单",
    "售后",
)
_SEMANTIC_ANCHOR_RE = re.compile(
    "|".join(
        re.escape(anchor)
        for anchor in sorted(_SEMANTIC_ANCHORS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
_STYLE_PUNCTUATION_RE = re.compile(
    r"[\s\u3000，,。.!！?？；;：:\"'‘’“”（）()、…—-]+"
)
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_CJK_CONNECTOR_RE = re.compile(r"(?:是|为|的)")
_ALPHANUMERIC_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")


class PolishIntegrityError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PolishResponseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PolishResult:
    answer: str
    status: str
    applied: bool
    latency_ms: int
    model: str
    error_type: str | None = None

    def audit_detail(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "applied": self.applied,
            "latency_ms": self.latency_ms,
            "model": self.model,
            "error_type": self.error_type,
        }


@dataclass(frozen=True, slots=True)
class _ProtectedDraft:
    prompt_text: str
    visible_original: str
    exact_phrases: tuple[str, ...]
    automatic_phrases: tuple[str, ...]


class PolishGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(
            timeout=settings.polish_timeout_seconds,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            transport=transport,
        )

    def polish(
        self,
        *,
        raw_answer: str,
        user_message: str,
        facts: str,
        recent_history: Any,
    ) -> PolishResult:
        started = time.perf_counter()
        if not self.settings.polish_enabled:
            return self._result(raw_answer, "disabled", False, started)
        if not self.settings.polish_base_url or not self.settings.polish_model_name:
            return self._result(
                raw_answer,
                "misconfigured",
                False,
                started,
                error_type="ConfigurationError",
            )

        try:
            protected = _protect_draft(raw_answer)
            response = self._client.post(
                f"{self.settings.polish_base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.settings.polish_model_name,
                    "messages": [
                        {"role": "system", "content": POLISH_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": POLISH_USER_TEMPLATE.format(
                                recent_history=_format_history(recent_history),
                                user_message=user_message,
                                raw_answer=protected.prompt_text,
                                facts=facts,
                            ),
                        },
                    ],
                    "temperature": self.settings.polish_temperature,
                    "max_tokens": self.settings.polish_max_output_tokens,
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=self.settings.polish_timeout_seconds,
            )
            response.raise_for_status()
            candidate = _response_content(response.json())
            restored = _restore_and_validate(candidate, protected)
        except PolishIntegrityError as exc:
            return self._result(
                raw_answer,
                f"rejected_{exc.reason}",
                False,
                started,
                error_type=type(exc).__name__,
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            return self._result(
                raw_answer,
                "error",
                False,
                started,
                error_type=type(exc).__name__,
            )

        if restored == raw_answer:
            return self._result(raw_answer, "unchanged", False, started)
        return self._result(restored, "applied", True, started)

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.polish_api_key:
            headers["Authorization"] = f"Bearer {self.settings.polish_api_key}"
        return headers

    def _result(
        self,
        answer: str,
        status: str,
        applied: bool,
        started: float,
        *,
        error_type: str | None = None,
    ) -> PolishResult:
        return PolishResult(
            answer=answer,
            status=status,
            applied=applied,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            model=self.settings.polish_model_name,
            error_type=error_type,
        )


def _protect_draft(raw_answer: str) -> _ProtectedDraft:
    if not raw_answer.strip():
        raise PolishIntegrityError("empty_input")

    keep_matches = list(_KEEP_RE.finditer(raw_answer))
    cursor = 0
    spans: list[tuple[int, int, str, bool]] = []
    visible_parts: list[str] = []
    for match in keep_matches:
        gap = raw_answer[cursor : match.start()]
        if _KEEP_MARKER_RE.search(gap) or _KEEP_MARKER_RE.search(match.group(1)):
            raise PolishIntegrityError("malformed_keep")
        spans.extend(
            (*span, False)
            for span in _automatic_spans(raw_answer, cursor, match.start())
        )
        visible_parts.append(gap)
        kept = match.group(1)
        if not kept:
            raise PolishIntegrityError("empty_keep")
        spans.append((match.start(), match.end(), kept, True))
        visible_parts.append(kept)
        cursor = match.end()
    tail = raw_answer[cursor:]
    if _KEEP_MARKER_RE.search(tail):
        raise PolishIntegrityError("malformed_keep")
    spans.extend(
        (*span, False)
        for span in _automatic_spans(raw_answer, cursor, len(raw_answer))
    )
    visible_parts.append(tail)

    prompt_parts: list[str] = []
    exact_phrases: list[str] = []
    automatic_phrases: list[str] = []
    cursor = 0
    for start, end, original, is_exact in sorted(spans):
        prompt_parts.append(raw_answer[cursor:start])
        prompt_parts.append(f"<keep>{original}</keep>")
        (exact_phrases if is_exact else automatic_phrases).append(original)
        cursor = end
    prompt_parts.append(raw_answer[cursor:])
    return _ProtectedDraft(
        prompt_text="".join(prompt_parts),
        visible_original="".join(visible_parts),
        exact_phrases=tuple(exact_phrases),
        automatic_phrases=tuple(automatic_phrases),
    )


def _automatic_spans(
    text: str,
    start: int,
    end: int,
) -> list[tuple[int, int, str]]:
    region = text[start:end]
    return [
        (start + match.start(), start + match.end(), match.group(0))
        for match in _AUTO_PROTECT_RE.finditer(region)
    ]


def _restore_and_validate(candidate: str, protected: _ProtectedDraft) -> str:
    candidate = candidate.strip()
    if not candidate:
        raise PolishIntegrityError("empty_output")
    if _INTERNAL_TAG_RE.search(candidate):
        raise PolishIntegrityError("internal_tag")

    original = protected.visible_original.strip()
    if len(candidate) > max(len(original) * 3, len(original) + 120):
        raise PolishIntegrityError("abnormal_length")
    if len(original) >= 24 and len(candidate) * 2 < len(original):
        raise PolishIntegrityError("abnormal_length")

    _validate_exact_phrases(candidate, protected.exact_phrases)
    original_numbers = set(_NUMBER_RE.findall(original))
    candidate_numbers = set(_NUMBER_RE.findall(candidate))
    if candidate_numbers - original_numbers:
        raise PolishIntegrityError("numeric_tokens")
    expected_automatic = {
        _normalize_automatic_phrase(phrase)
        for phrase in protected.automatic_phrases
    }
    for phrase in protected.exact_phrases:
        expected_automatic.update(_automatic_phrases(phrase))
    if _automatic_phrases(candidate) != expected_automatic:
        raise PolishIntegrityError("protected_phrase_mismatch")
    if candidate_numbers != original_numbers:
        raise PolishIntegrityError("numeric_tokens")
    if Counter(_semantic_anchors(candidate)) != Counter(_semantic_anchors(original)):
        raise PolishIntegrityError("semantic_anchors")
    if not _preserves_substantive_content(original, candidate):
        raise PolishIntegrityError("content_drift")
    return candidate


def _semantic_anchors(text: str) -> list[str]:
    return [match.group(0).lower() for match in _SEMANTIC_ANCHOR_RE.finditer(text)]


def _validate_exact_phrases(candidate: str, phrases: tuple[str, ...]) -> None:
    if not phrases:
        return
    expected_counts = Counter(phrases)
    if any(candidate.count(phrase) != count for phrase, count in expected_counts.items()):
        raise PolishIntegrityError("protected_phrase_mismatch")
    cursor = 0
    for phrase in phrases:
        position = candidate.find(phrase, cursor)
        if position < 0:
            raise PolishIntegrityError("protected_phrase_mismatch")
        cursor = position + len(phrase)


def _normalize_automatic_phrase(phrase: str) -> str:
    return re.sub(r"\s+", "", phrase).lower()


def _automatic_phrases(text: str) -> set[str]:
    return {
        _normalize_automatic_phrase(match.group(0))
        for match in _AUTO_PROTECT_RE.finditer(text)
    }


def _preserves_substantive_content(original: str, candidate: str) -> bool:
    compact_candidate = _STYLE_PUNCTUATION_RE.sub("", candidate).lower()
    for run in _CJK_RUN_RE.findall(original):
        phrases = (
            phrase
            for phrase in _CJK_CONNECTOR_RE.split(run)
            if len(phrase) >= 2
        )
        if any(phrase.lower() not in compact_candidate for phrase in phrases):
            return False
    return all(
        term.lower() in compact_candidate
        for term in _ALPHANUMERIC_TERM_RE.findall(original)
    )


def _response_content(data: Any) -> str:
    if not isinstance(data, dict):
        raise PolishResponseError("response is not an object")
    content = data["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise PolishResponseError("response content is empty")
    return content


def _format_history(history: Any) -> str:
    if not history:
        return "（无）"
    if isinstance(history, list):
        lines: list[str] = []
        for item in history:
            if isinstance(item, dict):
                role = str(item.get("role") or "unknown")
                content = str(item.get("content") or "")
                lines.append(f"{role}: {content}")
            else:
                lines.append(str(item))
        return "\n".join(lines)
    if isinstance(history, str):
        return history
    return json.dumps(history, ensure_ascii=False, default=str)
