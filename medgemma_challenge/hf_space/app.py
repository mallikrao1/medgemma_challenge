import gradio as gr
import requests


def call_backend(
    backend_url: str,
    patient_age: int,
    diagnosis: str,
    comorbidities: str,
    discharge_summary: str,
    meds_text: str,
    followup_text: str,
    redflags_text: str,
    language: str,
):
    meds = []
    for line in (meds_text or "").splitlines():
        token = line.strip()
        if not token:
            continue
        parts = [p.strip() for p in token.split("|")]
        while len(parts) < 4:
            parts.append("")
        name, dose, frequency, purpose = parts[:4]
        if name and dose and frequency:
            meds.append(
                {
                    "name": name,
                    "dose": dose,
                    "frequency": frequency,
                    "purpose": purpose,
                }
            )

    payload = {
        "patient_age": int(patient_age),
        "primary_diagnosis": diagnosis,
        "comorbidities": [x.strip() for x in comorbidities.split(",") if x.strip()],
        "discharge_summary": discharge_summary,
        "medications": meds,
        "follow_up_instructions": [x.strip() for x in followup_text.splitlines() if x.strip()],
        "red_flags": [x.strip() for x in redflags_text.splitlines() if x.strip()],
        "target_language": language,
        "health_literacy_level": "basic",
    }

    endpoint = backend_url.rstrip("/") + "/api/v1/discharge-plan"
    result = requests.post(endpoint, json=payload, timeout=120)
    result.raise_for_status()
    data = result.json()
    med_table = "\n".join(
        f"- {m['name']}: {m['dose']} | {m['frequency']} | {m.get('patient_instruction', '')}"
        for m in data.get("medication_schedule", [])
    )
    red_flags = "\n".join(f"- {x}" for x in data.get("red_flags", []))
    follow_up = "\n".join(f"- {x}" for x in data.get("follow_up_plan", []))
    return (
        data.get("plain_language_summary", ""),
        data.get("translated_summary", ""),
        med_table,
        red_flags,
        follow_up,
        str(data.get("metadata", {})),
    )


with gr.Blocks(title="MedGemma Discharge Copilot") as demo:
    gr.Markdown("# MedGemma Discharge Copilot")
    gr.Markdown("Public demo frontend for the deployed API service.")
    with gr.Row():
        backend_url = gr.Textbox(
            label="Backend URL",
            value="https://REPLACE_WITH_YOUR_AWS_URL",
        )
        language = gr.Dropdown(["english", "spanish", "hindi", "telugu"], value="english", label="Target language")
    patient_age = gr.Number(label="Patient age", value=58)
    diagnosis = gr.Textbox(label="Primary diagnosis", value="Acute heart failure exacerbation")
    comorbidities = gr.Textbox(label="Comorbidities (comma separated)", value="Hypertension, Type 2 diabetes")
    discharge_summary = gr.Textbox(label="Discharge summary", lines=5)
    meds_text = gr.Textbox(
        label="Medications (name|dose|frequency|purpose per line)",
        lines=4,
        value="Furosemide|40 mg|once daily|fluid control",
    )
    followup_text = gr.Textbox(label="Follow up (one per line)", lines=3, value="Cardiology follow-up in 5 days")
    redflags_text = gr.Textbox(label="Red flags (one per line)", lines=3, value="Chest pain\nShortness of breath")
    run_btn = gr.Button("Generate")

    plain = gr.Textbox(label="Plain language summary", lines=5)
    translated = gr.Textbox(label="Translated summary", lines=5)
    med_table = gr.Textbox(label="Medication schedule", lines=6)
    red_flags = gr.Textbox(label="Red flags", lines=5)
    follow_up = gr.Textbox(label="Follow-up", lines=5)
    metadata = gr.Textbox(label="Metadata", lines=6)

    run_btn.click(
        fn=call_backend,
        inputs=[
            backend_url,
            patient_age,
            diagnosis,
            comorbidities,
            discharge_summary,
            meds_text,
            followup_text,
            redflags_text,
            language,
        ],
        outputs=[plain, translated, med_table, red_flags, follow_up, metadata],
    )

demo.launch()

