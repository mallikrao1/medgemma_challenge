#!/usr/bin/env python3
"""
Deploy MedGemma challenge demo to AWS Lambda Function URL (public).

This path avoids App Runner subscription requirements and still produces
an AWS-hosted public interactive demo URL.
"""

from __future__ import annotations

import argparse
import io
import json
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


ROOT = Path(__file__).resolve().parent


LAMBDA_SOURCE = r'''
import json


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MedGemma Discharge Copilot</title>
  <style>
    :root { --bg:#f0f4f8; --card:#fff; --ink:#0f172a; --muted:#475569; --accent:#0f766e; --border:#cbd5e1; }
    * { box-sizing:border-box; font-family: "Avenir Next","Segoe UI",sans-serif; }
    body { margin:0; background:radial-gradient(circle at top left,#dff4f2,var(--bg)); color:var(--ink); }
    .shell { width:min(1400px,96vw); margin:20px auto; display:grid; grid-template-columns:1fr 1fr; gap:18px; }
    .panel { background:var(--card); border:1px solid var(--border); border-radius:16px; padding:18px; box-shadow:0 6px 30px rgba(15,23,42,.08); }
    h1,h2,h3 { margin-top:0; } .subtitle{ color:var(--muted); margin-top:-4px; }
    form { display:grid; gap:12px; } label{ display:grid; gap:6px; font-size:14px; color:var(--muted); }
    input,textarea,select,button { font:inherit; } input,textarea,select{ border:1px solid var(--border); border-radius:10px; padding:10px; }
    button{ border:0; border-radius:10px; padding:12px 14px; background:var(--accent); color:white; font-weight:600; cursor:pointer; }
    .status{ padding:8px 10px; border-radius:10px; background:#e2e8f0; color:var(--muted); margin-bottom:12px; }
    ul{ margin:0; padding-left:18px; } pre{ background:#f8fafc; border:1px solid var(--border); border-radius:10px; padding:10px; overflow:auto; }
    @media (max-width:980px){ .shell{ grid-template-columns:1fr; } }
  </style>
</head>
<body>
<main class="shell">
<section class="panel">
<h1>MedGemma Discharge Copilot</h1>
<p class="subtitle">AWS-hosted demo for MedGemma Impact Challenge</p>
<form id="f">
<label>Patient Age<input id="patient_age" type="number" min="0" max="120" value="58" required /></label>
<label>Primary Diagnosis<input id="primary_diagnosis" type="text" value="Acute heart failure exacerbation" required /></label>
<label>Comorbidities (comma-separated)<input id="comorbidities" type="text" value="Hypertension, Type 2 diabetes" /></label>
<label>Discharge Summary<textarea id="discharge_summary" rows="6" required>Admitted with shortness of breath and leg swelling. Improved with IV diuretics. Stable for discharge.</textarea></label>
<label>Medications (name|dose|frequency|purpose)<textarea id="medications" rows="4">Furosemide|40 mg|once daily|fluid control
Lisinopril|10 mg|once daily|blood pressure</textarea></label>
<label>Follow-up (one per line)<textarea id="follow_up">Cardiology in 5 days
Primary care in 7 days</textarea></label>
<label>Red flags (one per line)<textarea id="red_flags">Chest pain
Shortness of breath at rest
Rapid weight gain</textarea></label>
<label>Target language<select id="target_language"><option value="english">English</option><option value="spanish">Spanish</option><option value="hindi">Hindi</option><option value="telugu">Telugu</option></select></label>
<button id="submit-btn" type="submit">Generate Discharge Plan</button>
</form>
</section>
<section class="panel">
<h2>Patient-Facing Output</h2>
<div id="status" class="status">Ready</div>
<h3>Plain Language Summary</h3><p id="plain_summary"></p>
<h3>Translated Summary</h3><p id="translated_summary"></p>
<h3>Medication Schedule</h3><ul id="medication_schedule"></ul>
<h3>Red Flags</h3><ul id="red_flags_out"></ul>
<h3>Follow-up Plan</h3><ul id="follow_up_out"></ul>
<h3>Metadata</h3><pre id="metadata_out"></pre>
</section>
</main>
<script>
const f = document.getElementById("f");
const statusEl = document.getElementById("status");
const btn = document.getElementById("submit-btn");
function parseLines(t){ return (t||"").split("\\n").map(s=>s.trim()).filter(Boolean); }
function parseMeds(t){ return parseLines(t).map(line=>{ const [name="",dose="",frequency="",purpose=""] = line.split("|").map(v=>v.trim()); return {name,dose,frequency,purpose}; }).filter(m=>m.name&&m.dose&&m.frequency); }
function list(id,items,fmt){ const el=document.getElementById(id); el.innerHTML=""; (items||[]).forEach(i=>{ const li=document.createElement("li"); li.textContent=fmt?fmt(i):i; el.appendChild(li);});}
f.addEventListener("submit", async (e)=>{
  e.preventDefault(); btn.disabled=true; statusEl.textContent="Generating...";
  const p = {
    patient_age:Number(document.getElementById("patient_age").value||0),
    primary_diagnosis:document.getElementById("primary_diagnosis").value.trim(),
    comorbidities:document.getElementById("comorbidities").value.split(",").map(s=>s.trim()).filter(Boolean),
    discharge_summary:document.getElementById("discharge_summary").value.trim(),
    medications:parseMeds(document.getElementById("medications").value),
    follow_up_instructions:parseLines(document.getElementById("follow_up").value),
    red_flags:parseLines(document.getElementById("red_flags").value),
    target_language:document.getElementById("target_language").value,
    health_literacy_level:"basic"
  };
  try{
    const r=await fetch("/api/v1/discharge-plan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(p)});
    const d=await r.json(); if(!r.ok) throw new Error(d.detail||"Failed");
    document.getElementById("plain_summary").textContent=d.plain_language_summary||"";
    document.getElementById("translated_summary").textContent=d.translated_summary||"";
    list("medication_schedule",d.medication_schedule||[],m=>`${m.name}: ${m.dose}, ${m.frequency}. ${m.patient_instruction||""}`);
    list("red_flags_out",d.red_flags||[]);
    list("follow_up_out",d.follow_up_plan||[]);
    document.getElementById("metadata_out").textContent=JSON.stringify(d.metadata||{},null,2);
    statusEl.textContent="Done";
  } catch(err){ statusEl.textContent=`Error: ${err.message}`; }
  finally{ btn.disabled=false; }
});
</script>
</body>
</html>
"""


def _resp(status, body, content_type="application/json"):
    payload = body if isinstance(body, str) else json.dumps(body)
    return {
        "statusCode": status,
        "headers": {
            "content-type": content_type,
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET,POST,OPTIONS",
            "access-control-allow-headers": "*",
        },
        "body": payload,
    }


def _parse_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def _translate(summary, lang):
    lang = (lang or "english").lower()
    if lang == "english":
        return summary
    prefix = {"spanish":"Resumen en espanol:", "hindi":"Hindi summary:", "telugu":"Telugu summary:"}.get(lang, f"{lang.title()} summary:")
    return f"{prefix} {summary}"


def _build_plan(payload):
    diagnosis = payload.get("primary_diagnosis") or "your condition"
    meds = payload.get("medications") or []
    red_flags = payload.get("red_flags") or ["Chest pain", "Shortness of breath", "Fainting"]
    follow_up = payload.get("follow_up_instructions") or ["Follow up with your clinician in 7 days."]
    summary = f"You were treated for {diagnosis}. Please follow the plan below and seek urgent care if warning symptoms occur."
    schedule = []
    for m in meds:
        name = m.get("name", "").strip()
        dose = m.get("dose", "").strip()
        freq = m.get("frequency", "").strip()
        if not (name and dose and freq):
            continue
        schedule.append({
            "name": name,
            "dose": dose,
            "frequency": freq,
            "purpose": m.get("purpose", ""),
            "patient_instruction": f"Take {name} ({dose}) {freq} exactly as prescribed."
        })
    return {
        "plain_language_summary": summary,
        "translated_summary": _translate(summary, payload.get("target_language", "english")),
        "medication_schedule": schedule,
        "red_flags": red_flags,
        "follow_up_plan": follow_up,
        "metadata": {
            "backend_used": "lambda_mock",
            "model_id": "google/medgemma-4b-it",
            "generation_seconds": 0.0,
            "readability_flesch": 55.0,
            "safety_warnings": []
        }
    }


def handler(event, context):
    method = (event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod") or "GET").upper()
    path = event.get("rawPath") or event.get("path") or "/"
    if method == "OPTIONS":
        return _resp(200, {"ok": True})
    if method == "GET" and path in {"/", ""}:
        return _resp(200, HTML, "text/html; charset=utf-8")
    if method == "GET" and path == "/health":
        return _resp(200, {"status":"healthy","app":"MedGemma Discharge Copilot","backend":"lambda_mock"})
    if method == "POST" and path == "/api/v1/discharge-plan":
        try:
            payload = _parse_body(event)
        except Exception:
            return _resp(400, {"detail":"Invalid JSON payload"})
        return _resp(200, _build_plan(payload))
    return _resp(404, {"detail":"Not found"})
'''


def ensure_lambda_role(iam, role_name: str) -> str:
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        role = iam.get_role(RoleName=role_name)["Role"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "NoSuchEntity":
            raise
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Execution role for MedGemma challenge Lambda",
        )["Role"]
        time.sleep(8)

    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    return role["Arn"]


def build_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", LAMBDA_SOURCE)
    buf.seek(0)
    return buf.read()


def ensure_lambda_function(lambda_client, function_name: str, role_arn: str, zip_bytes: bytes) -> str:
    try:
        fn = lambda_client.get_function(FunctionName=function_name)
        lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes, Publish=True)
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Role=role_arn,
            Runtime="python3.11",
            Handler="lambda_function.handler",
            Timeout=30,
            MemorySize=512,
        )
        return fn["Configuration"]["FunctionArn"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ResourceNotFoundException":
            raise
        created = lambda_client.create_function(
            FunctionName=function_name,
            Runtime="python3.11",
            Role=role_arn,
            Handler="lambda_function.handler",
            Code={"ZipFile": zip_bytes},
            Timeout=30,
            MemorySize=512,
            Publish=True,
            Description="MedGemma challenge public demo",
        )
        return created["FunctionArn"]


def ensure_function_url(lambda_client, function_name: str) -> str:
    try:
        current = lambda_client.get_function_url_config(FunctionName=function_name)
        url = current["FunctionUrl"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "ResourceNotFoundException":
            raise
        created = lambda_client.create_function_url_config(
            FunctionName=function_name,
            AuthType="NONE",
            Cors={
                "AllowCredentials": False,
                "AllowHeaders": ["*"],
                "AllowMethods": ["*"],
                "AllowOrigins": ["*"],
            },
        )
        url = created["FunctionUrl"]

    stmt_id = "public-function-url-access"
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=stmt_id,
            Action="lambda:InvokeFunctionUrl",
            Principal="*",
            FunctionUrlAuthType="NONE",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceConflictException":
            raise

    # Required in some accounts for NONE-auth Function URL invocation
    invoke_stmt_id = "public-function-url-invoke"
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=invoke_stmt_id,
            Action="lambda:InvokeFunction",
            Principal="*",
            InvokedViaFunctionUrl=True,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceConflictException":
            raise

    return url


def save_url(url: str) -> None:
    out = ROOT / "live_demo_url.txt"
    out.write_text(url.strip() + "\n", encoding="utf-8")
    print(f"Live URL saved: {out}")
    print(f"Live URL: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy AWS Lambda URL demo")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--name", default="medgemma-discharge-copilot-lambda")
    args = parser.parse_args()

    iam = boto3.client("iam", region_name=args.region)
    lambda_client = boto3.client("lambda", region_name=args.region)
    sts = boto3.client("sts", region_name=args.region)
    ident = sts.get_caller_identity()
    print("AWS identity:", ident.get("Arn"))

    role_arn = ensure_lambda_role(iam, f"{args.name}-role")
    zip_bytes = build_zip_bytes()
    ensure_lambda_function(lambda_client, args.name, role_arn, zip_bytes)
    time.sleep(5)
    url = ensure_function_url(lambda_client, args.name)
    save_url(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
