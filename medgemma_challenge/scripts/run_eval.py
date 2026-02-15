#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from medgemma_challenge.app.config import Settings
from medgemma_challenge.app.schemas import DischargePlanRequest
from medgemma_challenge.app.service import DischargeInstructionService


def load_jsonl(path: Path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def run(dataset_path: Path, output_path: Path, backend: str):
    os.environ["MODEL_BACKEND"] = backend
    settings = Settings()
    service = DischargeInstructionService(settings)

    rows = load_jsonl(dataset_path)
    per_case = []
    total_red_flags = 0
    covered_red_flags = 0
    exact_meds = 0
    total_meds = 0
    readability_scores = []
    generation_seconds = []

    for idx, row in enumerate(rows, start=1):
        request = DischargePlanRequest(**row)
        response = service.generate(request)

        input_red = [x.lower() for x in request.red_flags]
        output_red = [x.lower() for x in response.red_flags]
        for rf in input_red:
            total_red_flags += 1
            if any(rf in out or out in rf for out in output_red):
                covered_red_flags += 1

        input_map = {m.name.lower(): (m.dose.lower(), m.frequency.lower()) for m in request.medications}
        out_map = {m.name.lower(): (m.dose.lower(), m.frequency.lower()) for m in response.medication_schedule}
        for med_name, med_sig in input_map.items():
            total_meds += 1
            if med_name in out_map and out_map[med_name] == med_sig:
                exact_meds += 1

        readability_scores.append(response.metadata.readability_flesch)
        generation_seconds.append(response.metadata.generation_seconds)
        per_case.append(
            {
                "case_id": idx,
                "backend": response.metadata.backend_used,
                "readability": response.metadata.readability_flesch,
                "generation_seconds": response.metadata.generation_seconds,
                "safety_warnings": response.metadata.safety_warnings,
            }
        )

    summary = {
        "cases": len(rows),
        "backend": backend,
        "red_flag_recall": round(covered_red_flags / total_red_flags, 4) if total_red_flags else 0.0,
        "medication_fidelity": round(exact_meds / total_meds, 4) if total_meds else 0.0,
        "avg_readability_flesch": round(sum(readability_scores) / len(readability_scores), 2)
        if readability_scores
        else 0.0,
        "avg_generation_seconds": round(sum(generation_seconds) / len(generation_seconds), 3)
        if generation_seconds
        else 0.0,
    }

    payload = {"summary": summary, "cases": per_case}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Run challenge evaluation")
    parser.add_argument("--dataset", required=True, help="Path to JSONL evaluation set")
    parser.add_argument("--output", required=True, help="Path for output JSON report")
    parser.add_argument(
        "--backend",
        default="mock",
        choices=["mock", "transformers", "openai_compatible"],
        help="Model backend",
    )
    args = parser.parse_args()
    run(Path(args.dataset), Path(args.output), args.backend)


if __name__ == "__main__":
    main()
