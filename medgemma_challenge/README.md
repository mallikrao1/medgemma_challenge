# MedGemma Impact Challenge Project

Human-centered discharge support app built for the Kaggle **MedGemma Impact Challenge**.

## What This Project Does

This app turns discharge notes into:

1. Patient-friendly plain-language instructions
2. Medication schedule with preserved dosage/frequency
3. Red-flag symptoms that require urgent care
4. Follow-up care checklist
5. Optional translated summary for patient communication

The pipeline is **MedGemma-first** and has a deterministic fallback mode for offline demo reliability.

## Project Layout

```text
medgemma_challenge/
  app/
    main.py
    config.py
    schemas.py
    prompting.py
    model_backend.py
    safety.py
    metrics.py
    translation.py
    service.py
  frontend/
    index.html
    app.js
    styles.css
  data/
    sample_eval_cases.jsonl
  scripts/
    run_eval.py
    package_submission.py
  submission/
    WRITEUP.md
    VIDEO_SCRIPT.md
    SUBMISSION_CHECKLIST.md
  tests/
    test_service.py
    test_safety.py
  requirements.txt
  .env.example
```

## Quick Start

From repository root:

```bash
source /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/venv/bin/activate
pip install -r /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/requirements.txt
cp /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/.env.example /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/.env
uvicorn medgemma_challenge.app.main:app --host 0.0.0.0 --port 8010 --reload
```

Open the demo UI:

- `http://localhost:8010/`

## MedGemma Usage

Default model id is set to:

- `google/medgemma-4b-it`

Configure in `.env`:

- `MODEL_BACKEND=transformers`
- `MEDGEMMA_MODEL_ID=google/medgemma-4b-it`

If local model weights are unavailable, set:

- `MODEL_BACKEND=mock`

This lets you run the full app, tests, and evaluation without internet/model downloads.

## Run Evaluation

```bash
python /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/scripts/run_eval.py \
  --dataset /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/data/sample_eval_cases.jsonl \
  --output /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/data/eval_results.json \
  --backend mock
```

## Run Tests

```bash
pytest /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/tests -q
```

## Submission Assets

Use:

```bash
python /Users/mallikarjunarao/Library/Mobile\ Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/scripts/package_submission.py
```

This builds `submission_bundle.zip` from the `submission/` folder.

## AWS Deployment (Public Live Demo Bonus)

This repo includes a full AWS App Runner deployment script:

- `/Users/mallikarjunarao/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/deploy/deploy_aws_apprunner.py`

Run:

```bash
cd "/Users/mallikarjunarao/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform"
source "/Users/mallikarjunarao/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform/venv/bin/activate"
python medgemma_challenge/deploy/deploy_aws_apprunner.py --region us-east-1 --app-name medgemma-discharge-copilot
```

If credentials are not configured, export them first:

```bash
export AWS_ACCESS_KEY_ID="<your-access-key-id>"
export AWS_SECRET_ACCESS_KEY="<your-secret-access-key>"
export AWS_DEFAULT_REGION="us-east-1"
```

Smoke-test live URL:

```bash
python medgemma_challenge/deploy/smoke_test.py --url "$(cat medgemma_challenge/deploy/live_demo_url.txt)"
```

## Browser Demo Video Recording

After deployment, create an auto-recorded browser demo video:

```bash
cd "/Users/mallikarjunarao/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform"
./medgemma_challenge/deploy/create_demo_video.sh "$(cat medgemma_challenge/deploy/live_demo_url.txt)"
```

Output:

- `/Users/mallikarjunarao/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/deploy/videos/demo_submission.mp4`

## Hugging Face Space (Public Interactive Bonus)

Use files in:

- `/Users/mallikarjunarao/Library/Mobile Documents/com~apple~CloudDocs/ai-infra-platform/medgemma_challenge/hf_space`

Create a Gradio Space and upload that folder as-is. Point the `Backend URL` field to your AWS App Runner URL.
