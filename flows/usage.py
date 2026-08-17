# flows/usage.py
#
# Standing instrumentation for token usage and prompt-cache effectiveness.
#
# docs/improvements/prompt-caching.md already documented how to verify caching:
# read cache_creation_input_tokens / cache_read_input_tokens off the response.
# It was written as a one-time manual check and never became code — so every
# later change (a new skill file, a changed tool set, a different model ID) could
# silently invalidate a cache prefix with nothing to notice. This module makes
# that check permanent.
#
# A cache_control breakpoint can fail three ways, none of which raise:
#
#   1. The prefix is under the model's minimum cacheable size (1024 tokens on
#      Sonnet 4.6). The API silently declines to cache and charges full price.
#   2. Something ahead of the breakpoint changed, so the prefix no longer
#      matches and the entry is written again instead of read.
#   3. The breakpoint is subsumed by a later one and is never read on its own.
#
# Claude's output is byte-identical in all three cases. The usage numbers are
# the only signal, which is why they need to be on screen rather than in a
# procedure someone runs once.
#
# Enabled by default — set LLM_LOG_USAGE=0 to silence.

import os

from llm_client import supports

LOG_USAGE = os.environ.get("LLM_LOG_USAGE", "1").lower() not in ("0", "false", "no", "")

# Per-site running totals. asyncio is single-threaded and every writer here runs
# on the same event loop — including the parallel dimension agents, which are
# gathered coroutines rather than threads — so a plain dict needs no lock.
_totals: dict[str, dict[str, int]] = {}


def _verdict(created: int, read: int, cached: bool) -> str:
    """
    Classify one call's cache behaviour.

    WRITE is expected exactly once per distinct prefix; every later call on that
    prefix should be HIT. DEAD means a cache_control breakpoint was sent and the
    API cached nothing at all — almost always cause 1 above, a prefix below the
    model minimum. Call sites that deliberately send no breakpoint pass
    cached=False and report "—", so DEAD keeps its narrow meaning: a breakpoint
    is present and misplaced.
    """
    if read:
        return "HIT"
    if created:
        return "WRITE"
    return "DEAD" if cached else "—"


def log_usage(response, where: str, cached: bool = True) -> None:
    """
    Record and print the usage numbers for one response.

    Accepts anything with a `.usage`; every field is read defensively because
    Bedrock and (later) the OpenAI-compatible local provider do not all report
    the same set. A provider that omits the cache fields reports zeros rather
    than crashing the flow it is instrumenting.

    Pass cached=False from call sites that intentionally send no cache_control
    breakpoint, so they are not counted as misplaced ones. See the judge callers
    in tools.py for the only current example.

    On a provider without explicit caching, `cached` is forced to False no
    matter what the call site asked for. The flows send cache_control markers
    unconditionally — that is by design, not a bug — but on vLLM
    those markers are dropped in translation, so counting them as DEAD would
    flag every single call as a breakpoint bug. Real prefix-cache hits still
    show up: openai_compat_client maps prompt_tokens_details.cached_tokens onto
    cache_read_input_tokens, so `read` is populated and the verdict is HIT.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return

    cached = cached and supports("prompt_caching")

    inp     = getattr(usage, "input_tokens", 0) or 0
    created = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read    = getattr(usage, "cache_read_input_tokens", 0) or 0
    out     = getattr(usage, "output_tokens", 0) or 0

    site = _totals.setdefault(
        where, {"calls": 0, "input": 0, "created": 0, "read": 0, "output": 0, "dead": 0}
    )
    site["calls"]   += 1
    site["input"]   += inp
    site["created"] += created
    site["read"]    += read
    site["output"]  += out

    verdict = _verdict(created, read, cached)
    if verdict == "DEAD":
        site["dead"] += 1

    if LOG_USAGE:
        print(
            f"[USAGE] {where:<34} "
            f"in={inp:<6} cache_w={created:<6} cache_r={read:<6} out={out:<6} {verdict}"
        )


def reset_usage() -> None:
    """Clear the accumulator. Call at the start of a flow to scope a summary."""
    _totals.clear()


def usage_summary() -> str:
    """
    Per-site totals plus a cache hit rate.

    Read the DEAD column first: a site with calls > 0 and dead == calls has a
    cache_control breakpoint that has never cached anything, which is a bug in
    the breakpoint placement rather than a cost to accept.
    """
    if not _totals:
        return "[USAGE] no calls recorded"

    lines = ["", "=" * 78, "TOKEN USAGE / CACHE SUMMARY", "=" * 78]
    lines.append(
        f"{'site':<34} {'calls':>5} {'input':>8} {'cache_w':>8} {'cache_r':>8} {'out':>7} {'dead':>5}"
    )

    grand = {"calls": 0, "input": 0, "created": 0, "read": 0, "output": 0, "dead": 0}
    for where, s in _totals.items():
        lines.append(
            f"{where:<34} {s['calls']:>5} {s['input']:>8} {s['created']:>8} "
            f"{s['read']:>8} {s['output']:>7} {s['dead']:>5}"
        )
        for k in grand:
            grand[k] += s[k]

    lines.append("-" * 78)
    lines.append(
        f"{'TOTAL':<34} {grand['calls']:>5} {grand['input']:>8} {grand['created']:>8} "
        f"{grand['read']:>8} {grand['output']:>7} {grand['dead']:>5}"
    )

    # Share of billable input tokens served from cache. Cache reads cost ~0.1x
    # and writes ~1.25x, so this is the number that actually moves the bill.
    billable = grand["input"] + grand["created"] + grand["read"]
    if billable:
        pct = 100.0 * grand["read"] / billable
        lines.append(f"\ncache hit rate: {pct:.1f}% of input tokens served from cache")

    if grand["dead"]:
        lines.append(
            f"\n[WARNING] {grand['dead']} call(s) sent a cache_control breakpoint and "
            f"cached nothing.\n"
            f"          Most likely the cached prefix is below the model's minimum "
            f"(1024 tokens on Sonnet 4.6)."
        )

    lines.append("=" * 78)
    return "\n".join(lines)
