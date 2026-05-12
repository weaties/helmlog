"""Claude API client wrapper for transcript Q&A and callback detection (#697).

Two entry points:

* ``ask()`` answers a single question against a race transcript with prompt
  caching enabled on the transcript portion. Cumulative spend across queries
  in the same race session benefits from cache reads at ~10% of the input
  rate.
* ``detect_callbacks()`` runs once post-race over the full diarized
  transcript and returns a list of structured callback dicts.

Both return token counts and a computed USD cost so the caller can persist
per-race aggregate spend (see spec §3 cost-cap state machine).

Citations are extracted from response text via ``[HH:MM:SS]`` markers — the
prompt instructs the model to cite that way. Tool-use citations were
considered but rejected for the first pass (see spec open question 3).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger

_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
_ANTHROPIC_VERSION = "2023-06-01"

_CITATION_RE = re.compile(r"\[((?:\d+:)?\d{1,2}:\d{2})\]")

_QA_SYSTEM = (
    "You are an expert sailing coach reviewing a single race transcript. "
    "Answer the user's question using only what the transcript supports. "
    "When you reference a moment, cite it with the timestamp in square "
    "brackets exactly as it appears in the transcript (e.g. [12:34] or "
    "[1:05:30]). Be concise. If the transcript does not contain the "
    "answer, say so."
)

_CALLBACK_SYSTEM = (
    "You are scanning a sailing race transcript for verbal callbacks — "
    "moments where a crew member said they want to revisit, flag, or come "
    "back to something post-race. Return a JSON array (and nothing else) "
    "of objects with keys: anchor_ts (the timestamp exactly as it appears "
    'in the transcript, e.g. "12:34" or "1:05:30"), speaker (the '
    "diarized speaker label exactly as it appears), excerpt (the exact "
    "short phrase), rationale (one short sentence on why this is a "
    "callback). Return [] if there are no callbacks."
)


@dataclass(frozen=True)
class LLMConfig:
    """Provider configuration. Reads from env in production, injected in tests."""

    api_key: str
    model: str
    endpoint: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_read_usd_per_mtok: float
    cache_write_usd_per_mtok: float


@dataclass(frozen=True)
class LLMResponse:
    text: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_create_tokens: int = 0
    cost_usd: float = 0.0


def _parse_callback_array(text: str) -> list[dict[str, Any]] | None:
    """Tolerantly parse a JSON array out of an LLM response.

    Tries, in order:
      1. The whole string as JSON.
      2. The first ``[...]`` substring (handles ``Here are the callbacks:
         [...]`` or ``\\`\\`\\`json [...] \\`\\`\\```` markdown fences).
      3. Strip ``\\`\\`\\`json`` / ``\\`\\`\\``` fences and try again.

    Returns None if nothing parses to a list.
    """
    candidates: list[str] = [text.strip()]
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    if fenced != text.strip():
        candidates.append(fenced)
    bracket_match = re.search(r"\[.*\]", text, re.DOTALL)
    if bracket_match:
        candidates.append(bracket_match.group(0))
    # Models routinely emit `\'` inside JSON strings around contractions
    # ("They\'re", "I\'m"). That's not a valid JSON escape, so json.loads
    # rejects every otherwise-fine candidate above. Strip the bogus
    # backslash before trying each candidate again.
    candidates += [c.replace(r"\'", "'") for c in list(candidates)]
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, list):
                return parsed
        except (ValueError, json.JSONDecodeError):
            continue
    return None


def extract_citations(text: str) -> list[dict[str, Any]]:
    """Pull [HH:MM:SS] markers out of a response, deduped, in first-seen order."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in _CITATION_RE.finditer(text):
        ts = m.group(1)
        if ts in seen:
            continue
        seen.add(ts)
        out.append({"ts": ts})
    return out


def _compute_cost(cfg: LLMConfig, usage: dict[str, Any]) -> float:
    in_tok = int(usage.get("input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
    return (
        in_tok * cfg.input_usd_per_mtok
        + out_tok * cfg.output_usd_per_mtok
        + cache_read * cfg.cache_read_usd_per_mtok
        + cache_create * cfg.cache_write_usd_per_mtok
    ) / 1_000_000


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self._cfg = config

    @property
    def model(self) -> str:
        return self._cfg.model

    def estimate_input_cost(self, text: str) -> float:
        """Rough pre-flight estimate at ~4 chars per token, input-priced.

        Used by the cost-cap pre-flight rejector — output tokens aren't
        known until the call returns, so the estimate intentionally omits
        them. Worst case the actual call costs more than estimated, which
        is fine because the hard cap rechecks on the actual recorded cost.
        """
        approx_tokens = max(1, len(text) // 4)
        return approx_tokens * self._cfg.input_usd_per_mtok / 1_000_000

    async def ask(
        self,
        *,
        transcript_text: str,
        question: str,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        body = {
            "model": self._cfg.model,
            "max_tokens": max_tokens,
            "system": _QA_SYSTEM,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Race transcript:\n{transcript_text}",
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": f"Question: {question}"},
                    ],
                }
            ],
        }
        usage, text = await self._post(body)
        return LLMResponse(
            text=text,
            citations=extract_citations(text),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_create_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cost_usd=_compute_cost(self._cfg, usage),
        )

    async def detect_callbacks(
        self,
        *,
        transcript_text: str,
        max_tokens: int = 4096,
    ) -> tuple[list[dict[str, Any]], float]:
        body = {
            "model": self._cfg.model,
            "max_tokens": max_tokens,
            "system": _CALLBACK_SYSTEM,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Race transcript:\n{transcript_text}",
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": (
                                "Return the JSON array now. Output ONLY the "
                                "array, starting with [ and ending with ]. "
                                "Do not wrap in markdown fences. Do not add "
                                "preamble or explanation."
                            ),
                        },
                    ],
                },
            ],
        }
        usage, text = await self._post(body)
        cost = _compute_cost(self._cfg, usage)
        parsed = _parse_callback_array(text)
        if parsed is None:
            logger.warning(
                "callback detection returned unparseable text (first 500 chars): {!r}",
                text[:500],
            )
            return [], cost
        return parsed, cost

    async def _post(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        headers = {
            "x-api-key": self._cfg.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(self._cfg.endpoint, headers=headers, json=body)
            if resp.status_code >= 400:
                # Surface Anthropic's error body — `raise_for_status` only
                # mentions the status code, which makes 400s opaque.
                logger.warning(
                    "Anthropic API {} for model={}: {}",
                    resp.status_code,
                    self._cfg.model,
                    resp.text[:1000],
                )
                resp.raise_for_status()
            payload = resp.json()
        usage = payload.get("usage") or {}
        # First text content block holds the answer
        text = ""
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
        return usage, text
