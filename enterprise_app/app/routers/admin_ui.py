from fastapi import APIRouter
from fastapi.responses import HTMLResponse


router = APIRouter(tags=["admin-ui"])


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Enterprise Admin Console</title>
  <style>
    body { margin:0; font-family: "Avenir Next", "Segoe UI", sans-serif; background:#f3f6fa; color:#0f172a; }
    .wrap { width:min(1200px,95vw); margin:24px auto; display:grid; gap:14px; }
    .card { background:#fff; border:1px solid #dbe3ee; border-radius:14px; padding:14px; box-shadow:0 4px 18px rgba(15,23,42,.06); }
    h1,h2,h3 { margin:0 0 10px 0; }
    label { display:grid; gap:5px; margin-bottom:8px; font-size:13px; color:#334155; }
    input,textarea,button { font:inherit; }
    input,textarea { border:1px solid #cbd5e1; border-radius:10px; padding:8px; }
    button { border:0; border-radius:10px; padding:10px 12px; background:#0f766e; color:#fff; cursor:pointer; margin-right:8px; }
    pre { background:#0b1220; color:#d2d8e4; border-radius:10px; padding:10px; overflow:auto; max-height:360px; }
    .row { display:grid; grid-template-columns: 1fr 1fr; gap:10px; }
    .actions { margin-top:8px; }
    @media (max-width: 900px) { .row { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Enterprise Admin Console</h1>
      <p>Sprint-2 controls: policy rules, workflows, async job dispatch.</p>
      <div class="row">
        <div>
          <label>Email<input id="email" value="admin@enterprise.local"/></label>
          <label>Password<input id="password" type="password" value="ChangeMe123!"/></label>
          <label>Tenant Slug<input id="tenant_slug" value="default"/></label>
          <label>Client ID<input id="client_id" value="admin-console"/></label>
          <div class="actions">
            <button onclick="login()">Login</button>
            <button onclick="refreshAll()">Refresh Data</button>
          </div>
        </div>
        <div>
          <h3>Token / Session</h3>
          <pre id="session_out"></pre>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Create Policy Rule</h2>
      <label>Name<input id="policy_name" value="Deny high-risk score over threshold"/></label>
      <label>Target Action<input id="policy_action" value="workflow.run.create"/></label>
      <label>Effect<input id="policy_effect" value="deny"/></label>
      <label>Condition JSON<textarea id="policy_condition" rows="4">{"field":"input.risk_score","op":"gt","value":8}</textarea></label>
      <button onclick="createPolicy()">Create Policy</button>
    </div>

    <div class="card">
      <h2>Create Workflow Template</h2>
      <label>Name<input id="wf_name" value="risk_triage"/></label>
      <label>Version<input id="wf_version" value="1.0.0"/></label>
      <label>Definition JSON<textarea id="wf_definition" rows="6">{"steps":[{"name":"ingest","action":"input.validate"},{"name":"score","action":"risk.score"},{"name":"recommend","action":"plan.generate"}]}</textarea></label>
      <button onclick="createWorkflowTemplate()">Create Template</button>
      <button onclick="createWorkflowRun()">Create Run (first template)</button>
      <button onclick="dispatchJob()">Dispatch One Job</button>
    </div>

    <div class="row">
      <div class="card"><h3>Policies</h3><pre id="policies_out"></pre></div>
      <div class="card"><h3>Workflows</h3><pre id="workflows_out"></pre></div>
    </div>
    <div class="row">
      <div class="card"><h3>Workflow Runs</h3><pre id="runs_out"></pre></div>
      <div class="card"><h3>Jobs</h3><pre id="jobs_out"></pre></div>
    </div>
  </div>

<script>
let token = "";
function clientId(){ return document.getElementById("client_id").value.trim() || "admin-console"; }
function authHeaders(extra={}){
  const h = {"Content-Type":"application/json","X-Client-ID":clientId(), ...extra};
  if(token) h["Authorization"] = `Bearer ${token}`;
  return h;
}
async function api(path, opts={}){
  const r = await fetch(path,{...opts, headers: authHeaders(opts.headers || {})});
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = text; }
  if(!r.ok) throw new Error(typeof data==="string"?data:JSON.stringify(data));
  return data;
}
function out(id,obj){ document.getElementById(id).textContent = JSON.stringify(obj,null,2); }

async function login(){
  try{
    const data = await api("/v1/auth/login",{method:"POST", body: JSON.stringify({
      email: document.getElementById("email").value.trim(),
      password: document.getElementById("password").value,
      tenant_slug: document.getElementById("tenant_slug").value.trim()
    })});
    token = data.access_token;
    out("session_out", data.user);
    await refreshAll();
  }catch(e){ out("session_out",{error:String(e)}); }
}
async function refreshAll(){
  try{
    out("policies_out", await api("/v1/policies"));
    out("workflows_out", await api("/v1/workflows/templates"));
    out("runs_out", await api("/v1/workflows/runs"));
    out("jobs_out", await api("/v1/jobs"));
  }catch(e){ out("jobs_out",{error:String(e)}); }
}
async function createPolicy(){
  try{
    await api("/v1/policies",{method:"POST", body: JSON.stringify({
      name: document.getElementById("policy_name").value,
      target_action: document.getElementById("policy_action").value,
      effect: document.getElementById("policy_effect").value,
      condition: JSON.parse(document.getElementById("policy_condition").value || "{}")
    })});
    await refreshAll();
  }catch(e){ out("policies_out",{error:String(e)}); }
}
async function createWorkflowTemplate(){
  try{
    await api("/v1/workflows/templates",{method:"POST", body: JSON.stringify({
      name: document.getElementById("wf_name").value,
      version: document.getElementById("wf_version").value,
      definition: JSON.parse(document.getElementById("wf_definition").value || "{}")
    })});
    await refreshAll();
  }catch(e){ out("workflows_out",{error:String(e)}); }
}
async function createWorkflowRun(){
  try{
    const templates = await api("/v1/workflows/templates");
    if(!templates.length) throw new Error("No templates found");
    await api(`/v1/workflows/templates/${templates[0].id}/runs`,{method:"POST", body: JSON.stringify({input_data:{risk_score:4,source:"admin_ui"}})});
    await refreshAll();
  }catch(e){ out("runs_out",{error:String(e)}); }
}
async function dispatchJob(){
  try{
    await api("/v1/jobs/dispatch-once",{method:"POST"});
    await refreshAll();
  }catch(e){ out("jobs_out",{error:String(e)}); }
}
</script>
</body>
</html>
"""


@router.get("/admin", response_class=HTMLResponse)
def admin_console() -> HTMLResponse:
    return HTMLResponse(content=ADMIN_HTML)

