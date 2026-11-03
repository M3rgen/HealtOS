"""Connection-local, extractive hints; never a factual or persistent model memory.

Only user text enters the window. Response *delivery* is tracked without reading the
model's prose: neither an assistant claim nor a tool payload can become a user fact.
Preference matching is deliberately conservative; the original thread stays authoritative.
"""

import json
import re
from collections import deque
from typing import Any

MAX_SUMMARY_BYTES = 8192
MAX_QUOTE_BYTES = 384  # JSON-encoded, including escapes and the truncation marker
MAX_REQUESTS = 8
MAX_TOPICS = 2
SUMMARY_LABEL = "Bağlantı özeti (güvenilmeyen kullanıcı alıntıları):\n"

# A mention alone (e.g. "kısa uyku") is not an explicit presentation preference. "Özet"
# names a requested artifact more often than a length ("özet istemiyorum, nabzıma bak").
DIRECTIVE = re.compile(
    r"\b(?:anlat\w*|açıkla\w*|yaz\w*|söyle\w*|konuş\w*|yanıtla\w*|"
    r"istiyorum|istemiyorum|kullan\w*)\b"
)
PREFERENCE_WORDS = {
    "length": r"kısa|kısaca|uzun|ayrıntılı|detaylı|tek cümlede|tek cümle",
    "imagery": r"düz|sade|şiirsel|mitolojik|imgeli|imge|mecaz",
    "format": r"tablo|liste|madde|başlık|paragraf",
    "address": r"sen|siz|resmi|resmî|samimi",
}
PREFERENCES = {
    name: re.compile(r"\b(?:" + words + r")\b") for name, words in PREFERENCE_WORDS.items()
}
FILLERS = re.compile(r"\b(?:lütfen|artık|bundan sonra|hayır|daha|biraz|çok|ve|ama|bir|bana)\b")
FOLLOWUP = re.compile(r"^(?:peki|ya|bunu|buna|aynı|bir de|geçen ay|geçen hafta)\b")


def _quote(text: str) -> dict[str, Any]:
    """Bound the stored quote as well as its wire size, even for escaped Unicode."""
    if len(json.dumps(text)) <= MAX_QUOTE_BYTES:
        return {"quote": text, "truncated": False}
    left, right = 0, len(text)
    while left < right:
        middle = (left + right + 1) // 2
        if len(json.dumps(text[:middle] + "…")) <= MAX_QUOTE_BYTES:
            left = middle
        else:
            right = middle - 1
    return {"quote": text[:left] + "…", "truncated": True}


class ConversationWindow:
    """Bounded user quotes plus transport outcomes, owned by exactly one ChatRelay.

    Topic anchors survive short followups; latest explicit preferences replace each
    dimension independently. Recent requests include completed turns because transport
    completion cannot tell us whether every subquestion was answered. Unanswered older
    requests get a separate bounded slot so routine followups do not immediately evict
    them. All eviction applies only to these extra hints, never to the model thread.
    """

    def __init__(self) -> None:
        self.topics: deque[dict[str, Any]] = deque(maxlen=MAX_TOPICS)
        self.preferences: dict[str, dict[str, Any]] = {}
        self.requests: deque[dict[str, Any]] = deque(maxlen=MAX_REQUESTS)
        self.unanswered: deque[dict[str, Any]] = deque(maxlen=MAX_TOPICS)
        self.current: dict[str, Any] | None = None
        self.received_response = False
        self.cancelled = False

    def begin(self, text: str) -> None:
        # Split only at sentence boundaries; keep the user's wording, not generated facts.
        for clause in re.split(r"(?<=[.!?;\n])\s*", text.strip()):
            normalized = clause.lower().replace("i̇", "i")
            matches = []
            if DIRECTIVE.search(normalized):
                for name, pattern in PREFERENCES.items():
                    markers = list(pattern.finditer(normalized))
                    if markers:
                        quoted = _quote(clause)
                        if quoted["truncated"]:
                            # A long preamble must not hide the actual preference. Keep
                            # its surrounding words (including nearby negation) verbatim.
                            start = max(0, markers[-1].start() - 20)
                            quoted = _quote("…" + clause[start:])
                            quoted["truncated"] = True
                        self.preferences[name] = quoted
                        matches.append(pattern)
            remainder = normalized
            if matches:
                for pattern in matches:
                    remainder = pattern.sub("", remainder)
                remainder = FILLERS.sub("", DIRECTIVE.sub("", remainder))
            if re.search(r"\w", remainder) and not FOLLOWUP.search(normalized):
                quoted = _quote(clause)
                if not self.topics or self.topics[-1] != quoted:
                    self.topics.append(quoted)
        self.current = {**_quote(text), "outcome": "pending"}
        self.requests.append(self.current)
        self.received_response = self.cancelled = False

    def finish(self, status: str) -> None:
        if self.current is None:
            return
        if status == "completed":
            status = (
                "interrupted"
                if self.cancelled
                else "response_received"
                if self.received_response
                else "no_response"
            )
        self.current["outcome"] = status
        if status != "response_received":
            self.unanswered.append(self.current)
        self.current = None

    def render(self) -> str:
        # Fixed item/quote limits keep storage bounded. The wire cap is an additional
        # safeguard if metadata changes later; always emit valid JSON, never slice it.
        payload = {
            "topic_hints": list(self.topics),
            "explicit_preferences": self.preferences.copy(),
            "recent_requests": list(self.requests),
            "unanswered_requests": list(self.unanswered),
        }
        while True:
            text = SUMMARY_LABEL + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(text.encode("utf-8")) <= MAX_SUMMARY_BYTES:
                return text
            # Prefer current preferences and uncompleted requests over old delivered turns.
            for key in ("recent_requests", "topic_hints", "unanswered_requests"):
                if payload[key]:
                    payload[key].pop(0)
                    break
            else:
                payload["explicit_preferences"].pop(next(iter(payload["explicit_preferences"])))

    def clear(self) -> None:
        self.topics.clear()
        self.preferences.clear()
        self.requests.clear()
        self.unanswered.clear()
        self.current = None
        self.received_response = self.cancelled = False
