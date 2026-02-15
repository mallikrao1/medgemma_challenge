### Project name
Discharge Copilot with MedGemma: Safer, Simpler, Multilingual Discharge Instructions

### Your team
- Mallikarjun Rao: Product and engineering lead, architecture, implementation, evaluation, and demo.

### Problem statement
Hospital discharge is a high-risk handoff point. Patients often leave with complex instructions, medication changes, and warning symptoms that are hard to understand. This causes avoidable readmissions, medication errors, and delayed escalation.

This project targets an unmet need in routine care transitions:

- Convert clinical discharge text into plain-language, action-oriented instructions.
- Preserve medication fidelity (dose/frequency) to avoid hallucination-related harm.
- Surface explicit red-flag symptoms for emergency escalation.
- Support multilingual communication for patient comprehension.

Expected impact if deployed:

- Faster discharge counseling for clinicians.
- Better patient understanding of medications and warning symptoms.
- Reduced preventable return visits driven by communication failure.

### Overall solution:
We built a **MedGemma-first discharge communication pipeline** with explicit safety controls.

Core design:

1. Clinician enters structured discharge context (diagnosis, meds, red flags, follow-up).
2. MedGemma generates a JSON output:
- plain-language summary
- medication schedule
- red flags
- follow-up plan
- translated summary
3. A safety layer enforces non-negotiable constraints:
- no medication dose/frequency drift from source
- mandatory red-flag coverage
- unsafe phrase detection
4. UI renders patient-ready output for counseling.

Why HAI-DEF model usage is effective:

- MedGemma is designed for healthcare use cases and medical communication.
- The prompt is structured for clinically constrained output rather than open-ended chat.
- Safety post-processing adds deterministic guarantees where generative models can fail.

### Technical details
### Stack
- Backend: FastAPI
- Model runtime: transformers (MedGemma), with optional OpenAI-compatible endpoint mode
- Frontend: static HTML/CSS/JS demo UI
- Evaluation: JSONL benchmark + scripted metrics
- Testing: pytest safety and service tests

### Key implementation details
1. Prompting and structure
- The model is instructed to return strict JSON only.
- Prompt includes a schema and source discharge context.

2. Safety and reliability
- Medication fidelity function overwrites generated dose/frequency with source values.
- Red-flag enforcement guarantees no clinically provided warning symptom is dropped.
- Fallback mode (`MODEL_BACKEND=mock`) guarantees local reproducibility when model weights are unavailable.

3. Evaluation
We provide a reproducible evaluation script over sample cases:
- red flag recall
- medication fidelity
- readability score (Flesch reading ease)
- response latency

Example command:

```bash
python medgemma_challenge/scripts/run_eval.py \
  --dataset medgemma_challenge/data/sample_eval_cases.jsonl \
  --output medgemma_challenge/data/eval_results.json \
  --backend mock
```

### Deployment considerations
- Local/offline mode is supported for constrained environments.
- For production, model artifacts should be preloaded and inference hosted behind authenticated APIs.
- HIPAA-ready deployment would require full PHI controls, audit logs, encryption, and institutional governance.

### Required links
- Video (<=3 min): upload `demo_submission.mp4` and add your public video URL here
- Public code repository: add your public GitHub repository URL here

### Bonus links
- Public interactive demo app: https://gwzmeycucrhdkzmnsaubck4dl40lgkjs.lambda-url.us-east-1.on.aws/
- Open-weight Hugging Face model tracing to HAI-DEF model: https://huggingface.co/google/medgemma-4b-it




