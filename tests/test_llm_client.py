"""Tests for the LLM client wrapper (#697).

The wrapper handles prompt-cached transcript Q&A and bulk callback
detection against the Claude messages API. All tests mock httpx —
no real network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helmlog.llm_client import (
    LLMClient,
    LLMConfig,
    LLMResponse,
    _parse_callback_array,
    extract_citations,
)


class TestParseCallbackArray:
    def test_plain_json(self) -> None:
        assert _parse_callback_array('[{"a":1}]') == [{"a": 1}]

    def test_strips_markdown_fences(self) -> None:
        assert _parse_callback_array('```json\n[{"a":1}]\n```') == [{"a": 1}]

    def test_extracts_from_prose(self) -> None:
        assert _parse_callback_array(
            'Here are the callbacks: [{"a":1}] and that is all.',
        ) == [{"a": 1}]

    def test_empty_array(self) -> None:
        assert _parse_callback_array("[]") == []

    def test_returns_none_for_garbage(self) -> None:
        assert _parse_callback_array("I cannot find any callbacks.") is None

    def test_recovers_from_illegal_apostrophe_escape(self) -> None:
        # Models commonly emit `\'` inside JSON strings around contractions
        # ("They\'re"). That's not a valid JSON escape — without the
        # unescape pass, every candidate fails and the whole response is
        # dropped. Race 146 on the Pi hit this on the first real run.
        body = r'[{"anchor_ts":"01:05","excerpt":"They\'re fiddling"}]'
        text = "```json\n" + body + "\n```"
        result = _parse_callback_array(text)
        assert result == [{"anchor_ts": "01:05", "excerpt": "They're fiddling"}]


def _mock_response(payload: dict[str, Any], status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=payload)
    resp.raise_for_status = MagicMock()
    return resp


def _api_payload(
    text: str, *, in_tok: int = 1000, out_tok: int = 50, cache_read: int = 0, cache_create: int = 0
) -> dict[str, Any]:
    return {
        "id": "msg_abc",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
        },
    }


class TestExtractCitations:
    def test_finds_mmss_marker(self) -> None:
        text = "We tacked at [12:05] and again at [14:30]."
        cites = extract_citations(text)
        assert [c["ts"] for c in cites] == ["12:05", "14:30"]

    def test_finds_hmmss_marker(self) -> None:
        text = "Long race — see [1:05:30]."
        cites = extract_citations(text)
        assert [c["ts"] for c in cites] == ["1:05:30"]

    def test_returns_empty_when_no_markers(self) -> None:
        assert extract_citations("plain answer") == []

    def test_dedupes_same_marker(self) -> None:
        cites = extract_citations("[12:05] then [12:05]")
        assert len(cites) == 1


class TestLLMClientAsk:
    @pytest.mark.asyncio
    async def test_returns_text_citations_tokens(self) -> None:
        cfg = LLMConfig(
            api_key="sk-test",
            model="claude-sonnet-4-6",
            endpoint="https://api.anthropic.com/v1/messages",
            input_usd_per_mtok=3.0,
            output_usd_per_mtok=15.0,
            cache_read_usd_per_mtok=0.30,
            cache_write_usd_per_mtok=3.75,
        )
        client = LLMClient(cfg)
        with patch("helmlog.llm_client.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.post = AsyncMock(
                return_value=_mock_response(
                    _api_payload(
                        "We tacked at [12:05].",
                        in_tok=200,
                        out_tok=20,
                        cache_read=1000,
                        cache_create=0,
                    )
                )
            )
            mock_cls.return_value.__aenter__.return_value = ctx

            resp = await client.ask(
                transcript_text="[12:05] helm: tack now\n[12:06:00] trim: ok",
                question="When did we tack?",
            )

        assert isinstance(resp, LLMResponse)
        assert "tacked" in resp.text
        assert resp.citations == [{"ts": "12:05"}]
        assert resp.input_tokens == 200
        assert resp.output_tokens == 20
        assert resp.cache_read_tokens == 1000
        assert resp.cache_create_tokens == 0
        # cost = (200 * 3 + 20 * 15 + 1000 * 0.30 + 0 * 3.75) / 1e6
        assert resp.cost_usd == pytest.approx((600 + 300 + 300) / 1e6)

    @pytest.mark.asyncio
    async def test_request_marks_transcript_for_caching(self) -> None:
        """The transcript block must carry cache_control so consecutive
        questions in the same race session reuse the prompt cache."""
        cfg = LLMConfig(
            api_key="sk-test",
            model="claude-sonnet-4-6",
            endpoint="https://api.anthropic.com/v1/messages",
            input_usd_per_mtok=3.0,
            output_usd_per_mtok=15.0,
            cache_read_usd_per_mtok=0.30,
            cache_write_usd_per_mtok=3.75,
        )
        client = LLMClient(cfg)
        captured: dict[str, Any] = {}

        async def fake_post(url: str, **kwargs: Any) -> MagicMock:  # noqa: ANN401
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["json"] = kwargs.get("json")
            return _mock_response(_api_payload("ok"))

        with patch("helmlog.llm_client.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.post = fake_post
            mock_cls.return_value.__aenter__.return_value = ctx
            await client.ask(transcript_text="long transcript", question="q?")

        body = captured["json"]
        # Transcript must be a content block with cache_control marker
        msg = body["messages"][0]
        transcript_block = next(
            b
            for b in msg["content"]
            if isinstance(b, dict) and "long transcript" in b.get("text", "")
        )
        assert transcript_block.get("cache_control") == {"type": "ephemeral"}
        # Auth header
        assert captured["headers"]["x-api-key"] == "sk-test"
        assert captured["headers"]["anthropic-version"]

    @pytest.mark.asyncio
    async def test_failed_response_raises_with_zero_cost(self) -> None:
        cfg = LLMConfig(
            api_key="sk-test",
            model="claude-sonnet-4-6",
            endpoint="https://api.anthropic.com/v1/messages",
            input_usd_per_mtok=3.0,
            output_usd_per_mtok=15.0,
            cache_read_usd_per_mtok=0.30,
            cache_write_usd_per_mtok=3.75,
        )
        client = LLMClient(cfg)

        import httpx

        with patch("helmlog.llm_client.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            err_resp = MagicMock()
            err_resp.status_code = 429
            err_resp.text = "rate limit"
            err_resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError(
                    "rate limit",
                    request=MagicMock(),
                    response=err_resp,
                )
            )
            ctx.post = AsyncMock(return_value=err_resp)
            mock_cls.return_value.__aenter__.return_value = ctx

            with pytest.raises(httpx.HTTPStatusError):
                await client.ask(transcript_text="x", question="q")


class TestLLMClientDetectCallbacks:
    @pytest.mark.asyncio
    async def test_parses_json_array_response(self) -> None:
        cfg = LLMConfig(
            api_key="sk-test",
            model="claude-haiku-4-5-20251001",
            endpoint="https://api.anthropic.com/v1/messages",
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
            cache_read_usd_per_mtok=0.10,
            cache_write_usd_per_mtok=1.25,
        )
        client = LLMClient(cfg)
        body = (
            '[{"anchor_ts":"12:05","speaker":"helm",'
            '"excerpt":"come back to this","rationale":"explicit revisit"}]'
        )
        with patch("helmlog.llm_client.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.post = AsyncMock(
                return_value=_mock_response(_api_payload(body, in_tok=500, out_tok=80))
            )
            mock_cls.return_value.__aenter__.return_value = ctx

            cbs, cost = await client.detect_callbacks(
                transcript_text="[12:05] helm: come back to this",
            )

        assert len(cbs) == 1
        assert cbs[0]["anchor_ts"] == "12:05"
        assert cbs[0]["speaker"] == "helm"
        assert cost == pytest.approx((500 * 1.0 + 80 * 5.0) / 1e6)

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self) -> None:
        """A non-JSON response is treated as zero callbacks rather than
        crashing the job — the cost is still recorded."""
        cfg = LLMConfig(
            api_key="sk-test",
            model="m",
            endpoint="https://api.anthropic.com/v1/messages",
            input_usd_per_mtok=1.0,
            output_usd_per_mtok=5.0,
            cache_read_usd_per_mtok=0.10,
            cache_write_usd_per_mtok=1.25,
        )
        client = LLMClient(cfg)
        with patch("helmlog.llm_client.httpx.AsyncClient") as mock_cls:
            ctx = AsyncMock()
            ctx.post = AsyncMock(
                return_value=_mock_response(
                    _api_payload("I could not find any callbacks.", in_tok=100, out_tok=10)
                )
            )
            mock_cls.return_value.__aenter__.return_value = ctx
            cbs, cost = await client.detect_callbacks(transcript_text="x")

        assert cbs == []
        assert cost > 0


class TestEstimateCost:
    def test_uses_input_pricing_for_estimate(self) -> None:
        """Pre-flight estimate (used by the cost cap pre-flight rejector
        in spec §3) must use input pricing — output is unknown until the
        call returns."""
        cfg = LLMConfig(
            api_key="sk-test",
            model="m",
            endpoint="https://api.anthropic.com/v1/messages",
            input_usd_per_mtok=3.0,
            output_usd_per_mtok=15.0,
            cache_read_usd_per_mtok=0.30,
            cache_write_usd_per_mtok=3.75,
        )
        client = LLMClient(cfg)
        # 4 chars/token rough estimate
        text = "x" * 4000  # ~1000 tokens
        est = client.estimate_input_cost(text)
        assert 0.0028 < est < 0.0032
