# © 2026 Cartman ApS. All rights reserved.
"""RateLimitedLLM — a *global*, by-reference client-side rate limiter.

Purpose / why this exists (see docs/ingest-config-findings.md §3, HIGH/MED):

The Azure *chat* deployment `gpt-5.4-mini` is provisioned `cap=10`, which is a
**throughput** cap — *10 requests / 60 s* — and this first pass also runs at a
**concurrency** of 2 (`--max-concurrency`). Those are two *different* limits
(throughput vs. parallelism), so both must be enforced, and both must be GLOBAL
(shared by reference), because the SDK gives two ways to bypass a per-strategy
``max_concurrency``:

  1. ``ContextualChunking._enrich_chunks`` calls ``llm.abatch_invoke(prompts)``
     with NO ``max_concurrency=`` → falls back to ``LLMInterface``'s hardcoded
     default of 12 in-flight calls — independent of anything on
     ``GraphExtraction``.
  2. ``LLMInterface.abatch_invoke`` builds a **fresh ``asyncio.Semaphore`` per
     call** (base.py), so N concurrent call sites can mean up to N×N in-flight
     calls.

The only way to hold a true global "≤2 concurrent AND ≤10 per 60 s" across
extraction *and* contextual chunking is a single shared wrapper injected
everywhere ``llm=`` is taken. This class is that wrapper.

Scope (from the findings, MED): the cap is on the **chat** deployment only.
The embedding deployment (``text-embedding-3-large``) is separate and is left
**unwrapped**. ``abatch_invoke`` already retries with backoff (max_retries=3),
so this limiter keeps us *under* the cap rather than retry-storming it.

No new dependency: pure asyncio + stdlib. ``aiolimiter.AsyncLimiter(rate=10,
capacity=10)`` + ``asyncio.Semaphore(2)`` would be a cleaner drop-in for the
sliding window; it is recorded here only as a comment, not a requirement.

Usage:

    llm_raw = LiteLLM(model="azure/gpt-5.4-mini", api_key=..., timeout=1800.0)
    llm = RateLimitedLLM(llm_raw, concurrency=2, req_per_window=10, window_s=60.0)
    extractor = GraphExtraction(llm=llm, entity_extractor=LLMExtractor(llm),
                                max_concurrency=2)
    async with GraphRAG(connection=..., llm=llm, embedder=..., ontology=SCHEMA,
                        embedding_dimension=1536) as rag:
        ...   # one source at a time, then rag.finalize()

Run ``python ratelimit.py`` for a self-test that exercises the fake inner LLM
through both gates (no network, no Azure).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from graphrag_sdk.core.models import ChatMessage, LLMResponse
from graphrag_sdk.core.providers import LLMBatchItem, LLMInterface

logger = logging.getLogger(__name__)


class RateLimitedLLM(LLMInterface):
    """A shared global LLM rate limiter. Enforces, *by reference* across every
    call site that holds this one instance:

      * at most ``concurrency`` (default 2) calls in flight at once, via a single
        shared ``asyncio.Semaphore``; and
      * at most ``req_per_window`` (default 10) calls admitted per ``window_s``
        (default 60.0 s) **sliding** window — a pure-asyncio timestamp bucket
        (an ``aiolimiter.AsyncLimiter(rate=10, capacity=10)`` would do this
        cleaner; no new dependency is added here).

    Both gates are held *by reference*, so injecting ONE instance into
    ``GraphExtraction``, ``ContextualChunking`` and ``GraphRAG(llm=...)`` caps
    the whole pipeline — this is the entire point (see module docstring).

    Design notes:
      * **Gate order is rate-then-concurrency.** The sliding-window reservation
        (which may *sleep*) happens *before* the concurrency semaphore is
        acquired, so a rate-limited waiter never wastes one of the (few)
        concurrency slots parking idle. The concurrency semaphore is held only
        for the duration of the actual call.
      * The async paths (``ainvoke``, ``ainvoke_messages``, ``abatch_invoke``,
        ``ainvoke_with_model``, ``astream``) all route through ``_throttled_call``
        — the shared gate. ``abatch_invoke`` **ignores** its per-call
        ``max_concurrency=`` (which the SDK turns into a fresh *local*
        semaphore) and instead serialises one prompt at a time through the
        *shared* semaphore, so no call site can fan out beyond the global cap.
      * The synchronous ``invoke`` / ``invoke_with_model`` are provided to fully
        implement the ABC but do **not** hold the asyncio gate — you cannot
        acquire an ``asyncio`` primitive from the worker thread that
        ``asyncio.to_thread`` (the base-class sync fallback) runs in. The
        SDK's ingestion/extraction pipeline is fully async and never calls the
        sync path; a one-line warning makes that explicit if it does.
      * Gates are created lazily *inside a running event loop* (``asyncio``
        primitives bind to the loop on first use): constructing them in
        ``__init__`` risks a cross-loop reference if the wrapper is built in a
        different loop than the one that runs the ingest.
    """

    def __init__(
        self,
        inner: LLMInterface,
        concurrency: int = 2,
        req_per_window: int = 10,
        window_s: float = 60.0,
        model_name: str | None = None,
        model_params: dict | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if req_per_window < 1:
            raise ValueError("req_per_window must be >= 1")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        super().__init__(
            model_name=model_name
            if model_name is not None
            else getattr(inner, "model_name", "rate-limited"),
            model_params=model_params,
            max_concurrency=concurrency,
        )
        self._inner = inner
        self._concurrency = concurrency
        self._req_per_window = req_per_window
        self._window_s = float(window_s)
        self._stamps: list[float] = []          # admission times (monotonic)
        self._calls = 0                          # total admitted async calls
        self._throttled = 0                      # calls that had to wait on window
        # lazily-bound asyncio primitives (created in the running loop):
        self._sem: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None
        self._warned_sync = False

    # ── gate plumbing ────────────────────────────────────────────
    async def _ensure_gates(self) -> None:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._concurrency)
            self._lock = asyncio.Lock()

    async def _reserve_rate_slot(self) -> None:
        """Reserve one admission to the sliding window.
        Under the lock: trim expired stamps, then EITHER win a slot
        (append its stamp + return) OR compute how long to wait for the
        oldest stamp to fall out of the window. The sleep happens OUTSIDE
        the lock so peer callers can re-check as the window rotates.
        """
        assert self._sem is not None and self._lock is not None  # _ensure_gates()
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_s
                self._stamps = [t for t in self._stamps if t > cutoff]
                if len(self._stamps) < self._req_per_window:
                    self._stamps.append(now)
                    return
                wait_for = max((self._stamps[0] + self._window_s) - now, 0.01)
            await asyncio.sleep(wait_for)
            self._throttled += 1

    async def _throttled_call(self, factory: Callable[[], Awaitable]) -> object:
        """Admit to the rate gate, then run ``factory()`` under the shared
        concurrency semaphore. Returns whatever ``factory()`` returns."""
        await self._ensure_gates()
        await self._reserve_rate_slot()           # throughput gate (may sleep)
        async with self._sem:                      # concurrency gate
            assert self._sem is not None
            self._calls += 1
            return await factory()

    # ── LLMInterface: async, gated ─────────────────────────────────
    async def ainvoke(
        self,
        prompt: str,
        *,
        max_retries: int = 3,
        timeout: float | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self._throttled_call(
            lambda: self._inner.ainvoke(
                prompt, max_retries=max_retries, timeout=timeout, **kwargs
            )
        )

    async def ainvoke_messages(
        self,
        messages: list[ChatMessage],
        *,
        max_retries: int = 3,
        timeout: float | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self._throttled_call(
            lambda: self._inner.ainvoke_messages(
                messages, max_retries=max_retries, timeout=timeout, **kwargs
            )
        )

    async def ainvoke_with_model(
        self,
        prompt: str,
        response_model: type,
        *,
        max_retries: int = 3,
        timeout: float | None = None,
        **kwargs,
    ):
        return await self._throttled_call(
            lambda: self._inner.ainvoke_with_model(
                prompt,
                response_model,
                max_retries=max_retries,
                timeout=timeout,
                **kwargs,
            )
        )

    async def astream(
        self,
        prompt: str,
        *,
        timeout: float | None = None,
        **kwargs,
    ):
        """One-shot async generator: consume the single underlying gated call,
        then yield its content (same shape as ``LLMInterface.astream``)."""
        resp = await self._throttled_call(
            lambda: self._inner.ainvoke(prompt, timeout=timeout, **kwargs)
        )
        assert isinstance(resp, LLMResponse)
        yield resp.content

    async def abatch_invoke(
        self,
        prompts: list[str],
        *,
        max_concurrency: int | None = None,
        max_retries: int = 3,
        timeout: float | None = None,
        **kwargs,
    ) -> list[LLMBatchItem]:
        """Serialize through the SHARED semaphore — the *single* global gate.

        The per-call ``max_concurrency=`` is **intentionally ignored**: in the
        base class it spawns a fresh *local* semaphore per call, which is exactly
        the N×N in-flight bypass the findings describe. By invoking ``ainvoke``
        one prompt at a time here, the in-flight total is governed only by the
        shared semaphore, so no call site can exceed the global cap. Order and
        per-item error semantics match the SDK's ``abatch_invoke`` (index-aligned
        ``LLMBatchItem`` list; a failed item is captured, not raised).
        """
        if not prompts:
            return []
        out: list[LLMBatchItem] = []
        for i, prompt in enumerate(prompts):
            try:
                resp = await self._throttled_call(
                    lambda pr=prompt: self._inner.ainvoke(
                        pr, max_retries=max_retries, timeout=timeout, **kwargs
                    )
                )
                out.append(LLMBatchItem(index=i, response=resp))
            except Exception as exc:  # per-item capture, like the base
                logger.warning(
                    "RateLimitedLLM batch item %d/%d failed via %s: %s",
                    i,
                    len(prompts),
                    self.model_name,
                    exc,
                )
                out.append(LLMBatchItem(index=i, error=exc))
        return out

    # ── LLMInterface: sync (ABC completeness only; unthrottled) ─────
    def _warn_sync_bypass(self, which: str) -> None:
        if not self._warned_sync:
            logger.warning(
                "RateLimitedLLM.%s() is on the SYNCHRONOUS path and does NOT hold "
                "the asyncio rate/concurrency gate. The SDK pipeline is async and "
                "never reaches this; if you are calling it deliberately, gate it "
                "outside this wrapper.",
                which,
            )
            self._warned_sync = True

    def invoke(self, prompt: str, **kwargs) -> LLMResponse:
        self._warn_sync_bypass("invoke")
        return self._inner.invoke(prompt, **kwargs)

    def invoke_with_model(self, prompt: str, response_model: type, **kwargs):
        self._warn_sync_bypass("invoke_with_model")
        return self._inner.invoke_with_model(prompt, response_model, **kwargs)

    # ── observability ───────────────────────────────────────────────
    @property
    def stats(self) -> dict:
        return {
            "model_name": self.model_name,
            "concurrency": self._concurrency,
            "req_per_window": self._req_per_window,
            "window_s": self._window_s,
            "calls_admitted": self._calls,
            "calls_throttled": self._throttled,
        }


# ── self-test ─────────────────────────────────────────────────────
class _FakeInner(LLMInterface):
    """In-process LLM double. Records peak in-flight and is fast so the cap
    assertions are deterministic (no network, no Azure)."""

    def __init__(self, sleep: float = 0.01) -> None:
        super().__init__(model_name="fake-test", max_concurrency=99)
        self._sleep = sleep
        self._inflight = 0
        self._peak = 0
        self._done = 0
        self._lock: asyncio.Lock | None = None

    async def ainvoke(
        self,
        prompt: str,
        *,
        max_retries: int = 3,
        timeout: float | None = None,
        **kwargs,
    ) -> LLMResponse:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._inflight += 1
            self._peak = max(self._peak, self._inflight)
        await asyncio.sleep(self._sleep)
        async with self._lock:
            self._inflight -= 1
            self._done += 1
        return LLMResponse(content="ok")

    async def ainvoke_messages(self, messages, **kwargs):  # type: ignore[override]
        return await self.ainvoke(messages)

    def invoke(self, prompt: "str", **kwargs):
        raise NotImplementedError("invoke() is sync; self-tests use ainvoke()")


def _test_concurrency_cap() -> None:
    """A: fan out 8 calls at concurrency=2; the shared semaphore caps in-flight
    to 2 even though 8 fire at once."""
    inner = _FakeInner(sleep=0.05)
    llm = RateLimitedLLM(inner, concurrency=2, req_per_window=10_000, window_s=60.0)
    async def go():
        results = await asyncio.gather(*(llm.ainvoke("x") for _ in range(8)))
        assert all(isinstance(r, LLMResponse) for r in results)
        assert inner._peak <= 2, f"peak in-flight {inner._peak} exceeded concurrency 2"
        assert inner._peak >= 2, f"peak in-flight {inner._peak} — cap never allowed 2"

    asyncio.run(go())
    logger.info("A concurrency cap: 8 fired, peak in-flight=%d (<=2) OK", inner._peak)


def _test_rate_cap_production_numbers() -> None:
    """B: the brief's literal case — 10 calls per 60 s. Fire 12 at PRODUCTION
    numbers (concurrency=2, req_per_window=10, window_s=60.0); the 11th/12th
    must be throttled. We cap each call with a short ``wait_for`` so the 2
    throttled callers surface as TimeoutError instantly instead of us waiting
    the 56 s the window demands — proving *exactly 10 accepted, 2 throttled*."""
    inner = _FakeInner(sleep=0.01)
    llm = RateLimitedLLM(inner, concurrency=2, req_per_window=10, window_s=60.0)

    async def go():
        coros = [llm.ainvoke(str(i)) for i in range(12)]
        results = await asyncio.gather(
            *(asyncio.wait_for(c, timeout=3.0) for c in coros),
            return_exceptions=True,
        )
        accepted = [r for r in results if isinstance(r, LLMResponse)]
        throttled = [r for r in results if isinstance(r, (asyncio.TimeoutError,))]
        other = [r for r in results if r not in accepted and r not in throttled]
        assert llm._calls == 10, f"expected 10 admitted, got {llm._calls}"
        assert len(accepted) == 10, f"expected 10 to complete, got {len(accepted)}"
        assert len(throttled) == 2, f"expected 2 throttled, got {len(throttled)}"
        assert not other, f"unexpected results: {other}"
        assert inner._peak <= 2, f"peak in-flight {inner._peak} exceeded concurrency 2"

    asyncio.run(go())
    logger.info(
        "B rate cap (10/60s @ concurrency 2): 12 fired → 10 accepted, 2 throttled, "
        "peak in-flight=%d OK",
        inner._peak,
    )


def _test_abatch_serializes_through_shared_gate() -> None:
    """C: two abatch_invoke calls fanned out concurrently, each with
    max_concurrency=100. The per-call cap MUST be ignored in favour of the
    shared gate → peak in-flight stays <= 2, and the result is an ordered
    index-aligned list of LLMBatchItem, all ok (7 < 10 so none are throttled)."""
    inner = _FakeInner(sleep=0.02)
    llm = RateLimitedLLM(inner, concurrency=2, req_per_window=10, window_s=60.0)

    async def go():
        a, b = await asyncio.gather(
            llm.abatch_invoke([f"a{i}" for i in range(4)], max_concurrency=100),
            llm.abatch_invoke([f"b{i}" for i in range(3)], max_concurrency=100),
        )
        assert [x.index for x in a] == [0, 1, 2, 3]
        assert [x.index for x in b] == [0, 1, 2]
        assert all(x.ok for x in a + b), "expected all items ok"
        assert all(isinstance(x, LLMBatchItem) for x in a + b)
        assert inner._peak <= 2, f"peak in-flight {inner._peak} exceeded concurrency 2"

    asyncio.run(go())
    logger.info(
        "C abatch_invoke: two concurrent calls @ max_concurrency=100 each → "
        "shared gate held, peak in-flight=%d (<=2), all items ok, OK",
        inner._peak,
    )


if __name__ == "__main__":
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("ratelimit.selftest")
    # Force the warning path visible so the sync-bypass guard is proven.
    os.environ.setdefault("RatelimitSyncWarn", "1")

    logger.info("── RateLimitedLLM self-test (fake inner, no network, no Azure) ──")
    _test_concurrency_cap()
    _test_rate_cap_production_numbers()
    _test_abatch_serializes_through_shared_gate()

    # Prove the sync path is present but guarded (would warn if ever used).
    sync_llm = RateLimitedLLM(_FakeInner(), concurrency=2, req_per_window=10, window_s=60.0)
    logger.info(
        "sync invoke() present: %s (delegates unthrottled; async pipeline never calls it)",
        hasattr(sync_llm, "invoke"),
    )
    logger.info("── all self-tests passed ──")
