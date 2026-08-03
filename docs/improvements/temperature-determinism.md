# Temperature / Sampling Determinism

## What it is

`temperature` controls how much randomness goes into picking the next token when Claude generates a response. At `temperature=0`, the highest-probability token wins every time — same input, same output. At higher values, Claude samples across a wider spread of plausible tokens, so the same input can produce different output across runs.

Every `messages.create`/`messages.stream` call in this codebase previously omitted `temperature`, so all of them ran at the API default (`1.0`).

## Problem

This is a security tool. Two places where temperature-driven variance is a real cost, not just a stylistic one:

- **The main agentic loop** drives tool selection and the 0–15 risk score. Reproducible tool-call sequencing and classification is preferable to creative variety here — nobody wants `usr_005`'s risk level to shift between runs with no change in the underlying data.
- **The completeness judge and critic** (`_check_completeness`, `_critique_response` in `flows/tools.py`) are not real MCP tools — see `docs/improvements/llm-as-judge.md`. `_COMPLETENESS_TOOL`/`_CRITIQUE_TOOL` have no backing function; `tool_choice={"type": "any"}` only forces the *shape* of the reply to valid JSON. The actual `complete: true/false` verdict and the `missing`/`issues` content are still sampled — `tool_choice` cannot make that choice deterministic, only well-formed. Since these verdicts directly control loop behavior (`run_flow_until_complete`'s round count, whether `run_flow_with_reflection` triggers a revision pass), sampling variance there changes *what the flow does*, not just how it's worded.
- **`tests/test_flows.py`** asserts on exact tool-call sequences and response substrings/score ranges. High temperature is a source of test flakiness independent of any real regression.

## Solution

Two independent, env-var-backed constants in `flows/llm_client.py` — the existing provider-agnostic module that both `run_flow.py` and `tools.py` already import `MODEL_ID`/`client` from:

```python
TEMPERATURE       = float(os.environ.get("LLM_TEMPERATURE", "0"))
JUDGE_TEMPERATURE = float(os.environ.get("LLM_JUDGE_TEMPERATURE", "0"))
```

Both default to `0`. They're split rather than shared because the main loop and the judge/critic calls are conceptually distinct — e.g. a future change might give the main loop's write-up more variance while keeping judge/critic pinned to `0` for control-flow stability.

Temperature isn't provider-specific, so it lives in `llm_client.py` rather than `bedrock_client.py`/`anthropic_client.py` — same reasoning as `MODEL_ID` living in one place regardless of which provider is active.

## Where it's applied

| Constant | Used in | File |
|---|---|---|
| `TEMPERATURE` | `_run_tool_loop` (verbose + non-verbose), `run_flow_stream`, `run_flow_until_complete_stream`, `run_flow_with_reflection_stream` (phases 1 & 3), `run_dimension_agent` (non-thinking branch only — see below) | `flows/run_flow.py` |
| `JUDGE_TEMPERATURE` | `_check_completeness`, `_critique_response` | `flows/tools.py` |
| `TEMPERATURE` | `run_test_flow`'s own lightweight tool loop | `tests/test_flows.py` |

`tests/test_flows.py` gets its own mention because `run_test_flow` doesn't call into `run_flow.py` — it reimplements a lightweight tool loop with its own `messages.create` call. It's wired to the same `TEMPERATURE` constant so the eval suite gets the same flakiness reduction that motivated this change in the first place.

## The extended-thinking exception

`run_dimension_agent` (options 8/9 — parallel agents + extended thinking) is a special case. The Anthropic API requires `temperature=1` whenever `thinking` is enabled and rejects any other value with a 400. The function builds its `create_kwargs` conditionally:

```python
create_kwargs = dict(model=MODEL_ID, max_tokens=..., system=cached_system, tools=cached_tools, messages=messages)
if thinking:
    create_kwargs["thinking"]      = {"type": "adaptive", "display": "summarized"}
    create_kwargs["output_config"] = {"effort": "high"}
else:
    create_kwargs["temperature"] = TEMPERATURE
```

`temperature` is only set in the `else` branch. See `docs/improvements/extended-thinking.md` for the rest of that call.

## Overriding

```bash
export LLM_TEMPERATURE=0.3         # main loop — more varied phrasing, less reproducible tool sequencing
export LLM_JUDGE_TEMPERATURE=0     # keep judge/critic pinned regardless of the main loop's setting
```

Both are read once at import time (module-level constants), so they must be set before the process starts — not changeable mid-run.

## Limitations

**`temperature=0` is "most likely token wins," not a formal determinism guarantee.** Server-side floating-point non-associativity and batching effects can still produce different output for identical input on rare occasions, even at `temperature=0`. This change makes runs *overwhelmingly* more reproducible, not provably bit-identical every time.

**It doesn't fix ambiguous skill instructions.** If a skill's rules genuinely support two different valid tool-call orders, `temperature=0` makes Claude pick the same one consistently — it doesn't make the skill unambiguous. Real skill ambiguity should still be fixed in the `SKILL.md` files, not papered over with sampling settings.

**No consistency eval yet.** `docs/ai-concepts.md`'s TODO list already asked for "run the same request N times and check that scores and tool call sequences are stable" — that's still worth adding as a regression guard, specifically to catch someone raising `LLM_TEMPERATURE` later without realizing the flakiness tradeoff they're reintroducing.

**Single global value per call category.** `TEMPERATURE` applies uniformly across every main-loop call site (lookup, risk assessment, offboarding, all flow patterns). If one of those genuinely benefits from more varied phrasing while another needs strict reproducibility, they currently can't diverge without adding a third constant.

## Planned improvements

### Per-flow temperature

If a specific flow (e.g. free-text lookup summaries) ever wants more varied phrasing while risk-scoring flows stay at `0`, thread a `temperature` parameter through `run_flow`/`run_flow_until_complete`/etc. instead of relying on one process-wide constant. Not needed today — nothing in this codebase currently asks for output variety.

### Consistency eval

Add a test that runs the same request N times against `usr_005` and asserts the tool-call sequence and risk score are identical (or within a tight range) — turning the "temperature=0 should mean stable output" claim into something CI actually checks, and catching accidental regressions if `LLM_TEMPERATURE`/`LLM_JUDGE_TEMPERATURE` are ever raised.
