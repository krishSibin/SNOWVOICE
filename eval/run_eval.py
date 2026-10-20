"""Offline evaluation: `python eval/run_eval.py build` then `python eval/run_eval.py run`.

This is the number that decides whether we beat the built-in assistant, and the
gate for prompt or model changes.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sn_agent.agent.pipeline import ResolutionPipeline
from sn_agent.config import get_settings
from sn_agent.llm import Meter, TcsGenAiLLM
from sn_agent.observability import setup
from sn_agent.servicenow.client import ServiceNowClient
from sn_agent.sources.servicenow_search import ServiceNowSearchSource

from .dataset import build_cases, load, save
from .scorers import aggregate, score_case, to_markdown

DATASET = Path("eval/dataset.json")
REPORTS = Path("eval/reports")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_eval")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="pick test cases out of ServiceNow")
    build.add_argument("--opened-after", default="2026-01-01")
    build.add_argument("--limit", type=int, default=50)

    run = sub.add_parser("run", help="run the pipeline over the test set")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--label", default=datetime.now().strftime("%Y%m%d-%H%M"))

    args = parser.parse_args(argv)
    settings = get_settings()
    setup(settings)
    client = ServiceNowClient(settings)

    if args.command == "build":
        opened_after = datetime.fromisoformat(args.opened_after).replace(tzinfo=timezone.utc)
        cases = build_cases(client, opened_after=opened_after, limit=args.limit)
        save(cases, DATASET)
        print(f"wrote {len(cases)} cases to {DATASET}")
        return 0

    llm = TcsGenAiLLM(settings)
    pipeline = ResolutionPipeline(settings, llm, ServiceNowSearchSource(client, settings, llm))
    cases = load(DATASET)[: args.limit]
    scores = []
    for index, case in enumerate(cases, start=1):
        ticket, actual = case.load(client)
        # as_of hides everything written after this ticket was raised, and open
        # tickets are skipped: today's live instance says nothing about then.
        result = pipeline.run(ticket, as_of=ticket.opened_at, include_open=False)
        meter = Meter(settings.prices, max_cost_usd=10**6, max_tokens=10**12)
        score = score_case(result, actual, llm=llm, meter=meter)
        scores.append(score)
        print(f"[{index}/{len(cases)}] {case.number}: {score.outcome} conf={score.confidence} match={score.match}")

    summary = aggregate(scores)
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = {"label": args.label, "summary": summary, "cases": [s.model_dump() for s in scores]}
    (REPORTS / f"{args.label}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (REPORTS / f"{args.label}.md").write_text(to_markdown(summary), encoding="utf-8")
    print(to_markdown(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
