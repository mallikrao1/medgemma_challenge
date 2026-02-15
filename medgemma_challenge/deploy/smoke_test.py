#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test live MedGemma demo service")
    parser.add_argument("--url", required=True, help="Base service URL, e.g. https://xxx.awsapprunner.com")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_url = args.url.rstrip("/")

    health = requests.get(f"{base_url}/health", timeout=30)
    health.raise_for_status()
    print("Health:", health.json())

    payload = {
        "patient_age": 58,
        "primary_diagnosis": "Acute heart failure exacerbation",
        "comorbidities": ["Hypertension", "Type 2 diabetes"],
        "discharge_summary": "Improved with treatment and stable for discharge.",
        "medications": [
            {"name": "Furosemide", "dose": "40 mg", "frequency": "once daily", "purpose": "fluid control"}
        ],
        "follow_up_instructions": ["Cardiology follow-up in 5 days"],
        "red_flags": ["Chest pain", "Shortness of breath at rest"],
        "target_language": "english",
        "health_literacy_level": "basic",
    }
    response = requests.post(f"{base_url}/api/v1/discharge-plan", json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    print("API response keys:", sorted(data.keys()))
    print("Summary:", data.get("plain_language_summary", "")[:200])
    print("Metadata:", json.dumps(data.get("metadata", {}), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

