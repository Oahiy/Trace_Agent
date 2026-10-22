"""Bounded fixed-context selection experiment and isolated six-case regression."""

import argparse
import asyncio
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.domains.med.dataset import DatasetRejected, canonical_digest
from app.domains.med.evaluation import EvaluationRejected
from app.domains.med.research import (
    EvidenceSelection,
    bind_selection,
    messages_for,
    numbered_sentences,
)
from app.llm.budget import CallBudget
from app.llm.contracts import ModelFailure
from app.llm.ollama import OllamaAdapter
from scripts import day3, day6, day11, day12
from scripts.day8 import load_contracts

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/day13"
MAX_DIAGNOSTIC_CALLS = 6
PARAPHRASES = (
    "Which elements constitute the PRISMA 2009 statement?",
    "What does the PRISMA 2009 statement consist of?",
)


def write_report(name, value):
    day12.write_report(name, value, cache=CACHE)


@contextmanager
def experiment_lock():
    cache = day12.report_directory(CACHE)
    descriptor = os.open(cache / "experiment.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EvaluationRejected("experiment_in_use") from None
        yield
    finally:
        os.close(descriptor)


def load_baseline():
    """Only use the saved, hash-bound Day 12 observations; never silently recapture."""
    baseline = day12.read_report(CACHE / "baseline.json")
    published = day12.read_report(ROOT / "docs/day12-results.json")
    _, suite, lock = load_contracts()
    day12.require(
        baseline["status"] == "completed" and baseline["dataset_lock"] == lock,
        "baseline_contract_changed",
    )
    cases = {case.case_id: case for case in suite.cases}
    expected = {
        row["case_id"]: row["observation_sha256"] for row in published["real_evaluation"]["rows"]
    }
    day12.require(set(baseline["observations"]) == set(expected), "baseline_cases_changed")
    for identifier, observation in baseline["observations"].items():
        day12.require(
            canonical_digest({"case": cases[identifier].model_dump(), "observation": observation})
            == expected[identifier],
            "baseline_observation_changed",
        )
    return baseline, cases["dev-01"]


class DiagnosticBudget(CallBudget):
    """Persist the shared cap BEFORE each HTTP attempt, including retries and failures."""

    def __init__(self, ledger):
        super().__init__(generation_limit=2, embedding_limit=2)
        day12.require(
            type(ledger.get("reserved_generation_calls")) is int
            and 0 <= ledger["reserved_generation_calls"] <= MAX_DIAGNOSTIC_CALLS,
            "diagnostic_ledger_invalid",
        )
        self.ledger = ledger

    def check(self, kind, output_limit):
        if kind != "generation":
            raise ModelFailure("invalid_model_input")
        if self.ledger["reserved_generation_calls"] >= MAX_DIAGNOSTIC_CALLS:
            raise ModelFailure("call_budget_exhausted")
        super().check(kind, output_limit)

    def reserve(self, kind, estimated_input, output_limit):
        reserved = super().reserve(kind, estimated_input, output_limit)
        self.ledger["reserved_generation_calls"] += 1
        write_report("experiment", self.ledger)
        return reserved


async def experiment(env):
    with experiment_lock():
        baseline, case = load_baseline()
        observation = baseline["observations"][case.case_id]
        evidence = observation["evidence"]
        # Freeze the ORIGINAL packed context. Repacking would change two variables at once.
        context_ids = observation["outcome"]["result"]["context_chunk_ids"]
        by_id = {hit["chunk_id"]: hit for hit in evidence["hits"]}
        selected = {f"E{i}": by_id[key] for i, key in enumerate(context_ids, 1)}
        wire = {key: numbered_sentences(hit["content"]) for key, hit in selected.items()}
        prompt_sha = canonical_digest(messages_for(case.question, wire)[0].content)
        path = CACHE / "experiment.json"
        ledger = (
            day12.read_report(path)
            if path.exists()
            else {
                "status": "collecting",
                "recorded_at": datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).isoformat(),
                "baseline_sha256": canonical_digest(baseline),
                "context_sha256": canonical_digest(wire),
                "reserved_generation_calls": 0,
                "experiments": [],
                "developer_review": "pending",
                "limitations": ["Fixed-context development diagnostics, not a blind benchmark"],
            }
        )
        day12.require(
            ledger["baseline_sha256"] == canonical_digest(baseline)
            and ledger["context_sha256"] == canonical_digest(wire),
            "experiment_inputs_changed",
        )
        day12.require(len(ledger["experiments"]) < 2, "candidate_limit_exhausted")
        day12.require(
            prompt_sha not in {item["prompt_sha256"] for item in ledger["experiments"]},
            "candidate_already_recorded",
        )
        group = {"prompt_sha256": prompt_sha, "status": "running", "cases": []}
        ledger["status"] = "collecting"
        ledger["experiments"].append(group)
        write_report("experiment", ledger)
        with day11.model_server(env) as runtime:
            ledger["runtime"] = runtime
            async with OllamaAdapter(Settings()) as model:
                for index, question in enumerate((case.question, *PARAPHRASES)):
                    budget = DiagnosticBudget(ledger)
                    row = {
                        "case": "original" if index == 0 else f"paraphrase-{index}",
                        "question": question,
                        "semantic_review": "pending",
                    }
                    group["cases"].append(row)
                    write_report("experiment", ledger)
                    try:
                        generated = await model.generate(
                            messages_for(question, wire),
                            EvidenceSelection,
                            budget=budget,
                            max_output_tokens=512,
                        )
                        row["selection"] = generated.data.model_dump()
                        row["result"] = bind_selection(generated.data, selected, evidence)
                        row["status"] = "completed"
                    except ModelFailure as error:
                        row.update(status="failed", error_code=error.code)
                    finally:
                        row["budget"] = budget.snapshot()
                        write_report("experiment", ledger)
        group["status"] = "completed"
        ledger["status"] = "completed"
        write_report("experiment", ledger)
        return ledger


def review():
    return day12.review(cache=CACHE)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["experiment", "evaluate", "review"])
    parser.add_argument("--require-quality", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "review":
            result = review()
        elif args.command == "experiment":
            result = asyncio.run(experiment(day6.environment()))
        else:
            result = asyncio.run(day12.evaluate(day6.environment(), cache=CACHE))
    except (EvaluationRejected, DatasetRejected, day3.ProbeFailure) as error:
        result = {"status": "failed", "error_code": str(error)}
    except Exception:
        result = {"status": "failed", "error_code": "day13_failed"}
    # Never replace a charged experiment ledger with an empty failure report.
    try:
        if args.command != "experiment":
            write_report(args.command, result)
    except Exception:
        result = {"status": "failed", "error_code": "report_write_failed"}
    print(json.dumps({k: v for k, v in result.items() if k in {"status", "summary", "error_code"}}))
    if result["status"] != "completed":
        return 1
    if args.require_quality:
        counts = result.get("summary", {}).get("quality_counts", {})
        if not counts:
            return 2
        return 2 if any(counts.get(key, 0) for key in ("failed", "pending_review")) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
