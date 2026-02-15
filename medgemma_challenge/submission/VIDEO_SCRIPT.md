# 3-Minute Demo Script

## 0:00 - 0:20 Problem
"Discharge instructions are often too complex, and that directly impacts medication safety and readmissions. Our project uses MedGemma to convert clinician discharge notes into clear, patient-safe instructions."

## 0:20 - 0:45 Product overview
Show UI form and explain inputs:
- diagnosis
- medications
- red flags
- follow-up
- target language

## 0:45 - 1:30 Live generation
Click **Generate Discharge Plan**.
Show output sections:
- plain language summary
- medication schedule
- red flags
- follow-up
- translated summary

Highlight safety:
- "Dose and frequency are forced to remain identical to source medication orders."
- "All clinician-provided red flags are guaranteed in output."

## 1:30 - 2:05 Technical architecture
Show simple architecture slide:
1. Structured input
2. MedGemma generation
3. Safety guardrails
4. Patient-facing output

Mention fallback mode for reproducible demo (`MODEL_BACKEND=mock`).

## 2:05 - 2:35 Evaluation
Show command for evaluation script and results JSON.
Mention metrics:
- red flag recall
- medication fidelity
- readability
- latency

## 2:35 - 3:00 Impact + close
"This approach enables clearer patient communication without requiring closed cloud-only systems. It is practical for privacy-sensitive settings and can scale across discharge workflows."

