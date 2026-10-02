"""Measure the gaps between Claude Agent SDK messages as ClaudeCodeModel receives them.

Spends subscription quota. Each run drives a real ClaudeCodeModel through an Agno Agent with
Sendesis's reviewer settings, and stamps every message `sdk.query` yields at the point where an
idle clock would sit. One JSON line per run is appended to --out.

    .venv/bin/python tools/measure_cadence.py --reps 2 --out reports/cadence.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from collections import Counter
from pathlib import Path

import claude_agent_sdk as sdk
from agno.agent import Agent
from pydantic import BaseModel

from agno_cli_models import ClaudeCodeModel


class Finding(BaseModel):
    file: str
    line: int
    severity: str
    summary: str


class Review(BaseModel):
    findings: list[Finding]
    summary: str


APP = '''import os
from flask import Flask, request, send_file

app = Flask(__name__)
BASE = "/srv/files"


@app.get("/download")
def download():
    name = request.args.get("name", "")
    return send_file(os.path.join(BASE, name))


@app.get("/health")
def health():
    return "ok"
'''

DIFF = '''diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -6,6 +6,12 @@ app = Flask(__name__)
 BASE = "/srv/files"


+@app.get("/download")
+def download():
+    name = request.args.get("name", "")
+    return send_file(os.path.join(BASE, name))
+
+
 @app.get("/health")
 def health():
     return "ok"
'''

SHAPES = {
    "review": dict(
        prompt=("You are reviewing one code change for security vulnerabilities. Review only what the change "
                "introduces. The repository is your working directory; read app.py with the Read tool before "
                f"answering.\n\n## diff\n```diff\n{DIFF}```"),
        schema=Review, tools=("Read", "Grep", "Glob")),
    "puzzle": dict(
        prompt=("Hertzsprung's problem: count the permutations of 1..8 in which no two adjacent positions hold "
                "values that differ by exactly 1. Reason it through carefully and check your count. Put the "
                "final number alone on the last line."),
        schema=None, tools=()),
    "longthink": dict(
        prompt=("Without writing or running code, determine the number of permutations of 1..10 in which no two "
                "adjacent positions hold values differing by exactly 1. Derive a recurrence, compute every term "
                "from n=1 to n=10 by hand, and cross-check at least two terms by an independent method before "
                "answering. Put the final number alone on the last line."),
        schema=None, tools=()),
    "essay": dict(
        prompt=("Write a careful technical explanation, about 1200 words, of how TCP congestion control works: "
                "slow start, congestion avoidance, fast retransmit and recovery, and how CUBIC and BBR differ. "
                "Plain prose with short headings."),
        schema=None, tools=()),
}


def kind(msg) -> str:
    if isinstance(msg, sdk.SystemMessage):
        return f"system:{msg.subtype}"
    if isinstance(msg, sdk.StreamEvent):
        ev = msg.event
        t = ev.get("type")
        if t == "content_block_delta":
            return f"stream:delta:{ev.get('delta', {}).get('type')}"
        if t == "content_block_start":
            return f"stream:start:{ev.get('content_block', {}).get('type')}"
        return f"stream:{t}"
    if isinstance(msg, sdk.AssistantMessage):
        return "assistant:" + ",".join(type(b).__name__ for b in msg.content)
    if isinstance(msg, sdk.ResultMessage):
        return f"result:{msg.subtype}"
    return type(msg).__name__


def stamping(log: list):
    async def query(*, prompt, options):
        log.append((time.monotonic(), "query_called"))
        gen = sdk.query(prompt=prompt, options=options)
        try:
            async for msg in gen:
                log.append((time.monotonic(), kind(msg)))
                yield msg
        finally:
            await gen.aclose()

    return query


def summarize(log: list, t0: float) -> dict:
    events = [(round(t - t0, 3), k) for t, k in log]
    init = next((i for i, (_, k) in enumerate(events) if k.startswith("system:init")), None)
    out = {"n_messages": len(events) - 1, "kinds": dict(Counter(k for _, k in events))}
    if init is None:
        return {**out, "startup_s": None, "max_gap_s": None}
    out["startup_s"] = events[init][0]
    gaps = [(events[i + 1][0] - events[i][0], events[i][1], events[i + 1][1]) for i in range(init, len(events) - 1)]
    gaps.sort(reverse=True)
    out["max_gap_s"] = round(gaps[0][0], 3) if gaps else 0.0
    out["max_gap_between"] = list(gaps[0][1:]) if gaps else None
    out["top_gaps"] = [[round(g, 3), a, b] for g, a, b in gaps[:5]]
    out["events"] = events
    return out


async def one_run(shape: str, stream: bool, model_id: str, effort: str, cwd: str) -> dict:
    spec = SHAPES[shape]
    log: list = []
    model = ClaudeCodeModel(id=model_id, effort=effort, max_turns=20, builtin_tools=spec["tools"], cwd=cwd,
                            timeout_s=900, query_fn=stamping(log))
    agent = Agent(model=model, output_schema=spec["schema"])
    t0 = time.monotonic()
    record = {"shape": shape, "mode": "stream" if stream else "nonstream", "model": model_id, "effort": effort}
    try:
        if stream:
            async for _ in agent.arun(spec["prompt"], stream=True):
                pass
        else:
            await agent.arun(spec["prompt"])
        record["ok"] = True
    except Exception as exc:  # keep the data; the run is excluded from "healthy"
        record.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
    record["duration_s"] = round(time.monotonic() - t0, 3)
    record["cli_version"] = model.last_run_info.get("cli_version")
    record["result_seen"] = any(k.startswith("result:") for _, k in log)
    record.update(summarize(log, t0))
    return record


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--shapes", default=",".join(SHAPES))
    ap.add_argument("--modes", default="nonstream,stream")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as cwd:
        (Path(cwd) / "app.py").write_text(APP)
        for rep in range(a.reps):
            for shape in a.shapes.split(","):
                for mode in a.modes.split(","):
                    rec = await one_run(shape, mode == "stream", a.model, a.effort, cwd)
                    rec["rep"] = rep
                    with open(a.out, "a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print(f"{shape:7} {rec['mode']:9} rep={rep} ok={rec['ok']} dur={rec['duration_s']:7.1f}s "
                          f"startup={rec.get('startup_s')} max_gap={rec.get('max_gap_s')} {rec.get('max_gap_between')}",
                          flush=True)


if __name__ == "__main__":
    asyncio.run(main())
