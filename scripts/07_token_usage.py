# /// script
# requires-python = ">=3.11"
# ///
"""Sum token usage from Claude Code session transcripts and price it at Anthropic API list prices.

Usage: uv run scripts/07_token_usage.py [--split ISO_TIME ...] <session.jsonl> [<subagent.jsonl> ...]

--split divides the session into phases at the given UTC timestamps (e.g. 2026-11-02T22:41:00Z);
each phase reports its own wall time (first to last API response) and cost.

Each assistant API response appears in the transcript once per content block, all carrying the same
message id and usage, so usage is de-duplicated by message id (keeping the largest output count).
"""
import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# $/MTok, Anthropic first-party API list prices (claude-api skill pricing table, cached 2026-10-06).
# Cache writes: 1.25x input (5-minute TTL), 2x input (1-hour TTL). Cache read for Opus 5.5: $0.20.
PRICES = {
    "claude-opus-5-5": dict(inp=4.00, out=20.00, w5m=5.00, w1h=8.00, read=0.20),
}
WEB_SEARCH_PER_1K = 10.00  # $ per 1,000 server-side web searches (Anthropic web search tool list price)

ap = argparse.ArgumentParser()
ap.add_argument("files", nargs="+", type=Path)
ap.add_argument("--split", action="append", default=[], help="UTC timestamp starting a new phase")
args = ap.parse_args()
ts = lambda s_: datetime.fromisoformat(s_.replace("Z", "+00:00"))
splits = sorted(map(ts, args.split))
phase_of = lambda t: sum(t >= s_ for s_ in splits)

rows = []
times = defaultdict(list)
for path in args.files:
    msgs = {}
    for line in path.open():
        d = json.loads(line)
        m = d.get("message")
        if not (isinstance(m, dict) and m.get("usage") and m.get("id") and d.get("timestamp")):
            continue
        prev = msgs.get(m["id"])
        if prev is None or m["usage"].get("output_tokens", 0) >= prev[1].get("output_tokens", 0):
            msgs[m["id"]] = (m.get("model"), m["usage"], ts(d["timestamp"]))
    agg = defaultdict(lambda: defaultdict(int))
    for model, u, t in msgs.values():
        ph = phase_of(t)
        times[ph].append(t)
        a = agg[(ph, model)]
        cc = u.get("cache_creation") or {}
        a["calls"] += 1
        a["input"] += u.get("input_tokens", 0)
        a["write_5m"] += cc.get("ephemeral_5m_input_tokens", 0)
        a["write_1h"] += cc.get("ephemeral_1h_input_tokens", 0)
        a["read"] += u.get("cache_read_input_tokens", 0)
        a["output"] += u.get("output_tokens", 0)
        a["thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
        st = u.get("server_tool_use") or {}
        a["web_search"] += st.get("web_search_requests", 0)
        a["web_fetch"] += st.get("web_fetch_requests", 0)
    label = "main session" if "subagents" not in str(path) else f"subagent {path.stem.removeprefix('agent-')[:8]}"
    for (ph, model), a in sorted(agg.items()):
        rows.append((ph, label, model, a))


def cost(model, a):
    p = PRICES[model]
    parts = dict(input=a["input"] * p["inp"], write_5m=a["write_5m"] * p["w5m"], write_1h=a["write_1h"] * p["w1h"],
                 read=a["read"] * p["read"], output=a["output"] * p["out"])
    parts = {k: v / 1e6 for k, v in parts.items()}
    parts["web_search"] = a["web_search"] * WEB_SEARCH_PER_1K / 1000
    return parts


hdr = (f"{'phase':5s} {'source':20s} {'calls':>5s} {'input':>8s} {'write5m':>10s} {'write1h':>10s} {'cache read':>12s} "
       f"{'output':>9s} {'(thinking)':>10s} {'srch':>5s} {'cost $':>8s}")
KEYS = ("calls", "input", "write_5m", "write_1h", "read", "output", "thinking", "web_search")


def line(ph, label, a, c):
    return (f"{ph:5} {label:20s} {int(a['calls']):5d} {int(a['input']):8,d} {int(a['write_5m']):10,d} "
            f"{int(a['write_1h']):10,d} {int(a['read']):12,d} {int(a['output']):9,d} {int(a['thinking']):10,d} "
            f"{int(a['web_search']):5d} {c:8.2f}")


print(hdr)
grand = defaultdict(float)
for ph in sorted(times):
    tot = defaultdict(float)
    for p_, label, model, a in rows:
        if p_ != ph:
            continue
        c = cost(model, a)
        print(line(ph + 1, label, a, sum(c.values())))
        for k in KEYS:
            tot[k] += a[k]
        for k, v in c.items():
            tot["$" + k] += v
    dollars = sum(v for k, v in tot.items() if k.startswith("$"))
    wall = max(times[ph]) - min(times[ph])
    print(line(ph + 1, "PHASE TOTAL", tot, dollars) + f"   wall {wall.total_seconds() / 60:.1f} min "
          f"({min(times[ph]):%H:%M}–{max(times[ph]):%H:%M} UTC)")
    print()
    for k, v in tot.items():
        grand[k] += v
dollars = sum(v for k, v in grand.items() if k.startswith("$"))
print(line("all", "TOTAL", grand, dollars))
print("\ncost breakdown $: " + ", ".join(f"{k[1:]} {v:.2f}" for k, v in grand.items() if k.startswith("$")))
all_in = grand["input"] + grand["write_5m"] + grand["write_1h"] + grand["read"]
print(f"total tokens processed: {all_in + grand['output']:,.0f} ({all_in:,.0f} input incl. cache, "
      f"{grand['output']:,.0f} output)")
no_cache = (all_in * 4.00 + grand["output"] * 20.00) / 1e6 + grand["$web_search"]
print(f"same tokens with no prompt caching (all input at $4/MTok): ${no_cache:.2f}")
