"""
Run all local custom metrics against traces.json and save grade_results.json.

This bypasses `agents-cli eval grade` which unconditionally initialises a GCS
storage client (requiring GCP ADC) even when every metric is local.
"""
import json
import os
import sys
import time
from pathlib import Path

# Allow the script to be run from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

from vertexai._genai.types.common import EvaluationDataset

# ── Config ─────────────────────────────────────────────────────────────────
TRACES_PATH = Path("tests/eval/artifacts/traces.json")
RESULTS_PATH = Path("tests/eval/artifacts/grade_results.json")
METRICS_DIR = Path("tests/eval")

METRIC_FILES = [
    "metric_routing.py",
    "metric_recommendation.py",
    "metric_security.py",
]


def load_metric(fpath: Path):
    src = fpath.read_text()
    ns: dict = {}
    exec(compile(src, str(fpath), "exec"), ns)
    return ns["evaluate"], fpath.stem


def run_grading():
    # Load traces
    dataset = EvaluationDataset.model_validate_json(TRACES_PATH.read_text())
    eval_cases = dataset.eval_cases or []
    print(f"Loaded {len(eval_cases)} eval cases from {TRACES_PATH}")

    # Load metrics
    metrics = []
    for fname in METRIC_FILES:
        fn, name = load_metric(METRICS_DIR / fname)
        metrics.append((name, fn))
    print(f"Loaded {len(metrics)} metrics: {[m[0] for m in metrics]}\n")

    # Run evaluation
    results_by_case = []
    all_scores: dict[str, list[int]] = {m[0]: [] for m in metrics}

    for case in eval_cases:
        case_id = getattr(case, "eval_case_id", "unknown")
        case_scores = {}
        case_explanations = {}
        for metric_name, fn in metrics:
            try:
                verdict = fn(case)
                score = verdict.get("score", 0)
                explanation = verdict.get("explanation", "")
            except Exception as e:
                import traceback
                traceback.print_exc()
                score = 0
                explanation = f"ERROR: {e}"
            case_scores[metric_name] = score
            case_explanations[metric_name] = explanation
            all_scores[metric_name].append(score)
            print(f"  [{case_id}] {metric_name}: {score}/5 — {explanation[:90]}")

        results_by_case.append({
            "eval_case_id": case_id,
            "scores": case_scores,
            "explanations": case_explanations,
        })

    # Compute averages
    averages = {name: round(sum(scores) / len(scores), 2) if scores else 0
                for name, scores in all_scores.items()}

    print("\n── Summary ────────────────────────────────────────────────")
    for name, avg in averages.items():
        bar = "█" * int(avg) + "░" * (5 - int(avg))
        print(f"  {name:<28} avg={avg}/5  {bar}")
    print()

    # Save results
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "num_cases": len(eval_cases),
        "averages": averages,
        "cases": results_by_case,
    }
    RESULTS_PATH.write_text(json.dumps(output, indent=2))
    print(f"Grade results saved to {RESULTS_PATH}")

    # Exit non-zero if any average is below threshold (3/5)
    failures = [n for n, avg in averages.items() if avg < 3.0]
    if failures:
        print(f"\n❌ Metrics below threshold (3/5): {failures}", file=sys.stderr)
        sys.exit(1)
    print("✅ All metrics passed threshold (≥3/5).")


if __name__ == "__main__":
    run_grading()
