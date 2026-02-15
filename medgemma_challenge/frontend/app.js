const form = document.getElementById("discharge-form");
const statusEl = document.getElementById("status");
const submitBtn = document.getElementById("submit-btn");

function parseLines(text) {
  return (text || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseMedications(text) {
  return parseLines(text).map((line) => {
    const [name = "", dose = "", frequency = "", purpose = ""] = line.split("|").map((v) => v.trim());
    return { name, dose, frequency, purpose };
  }).filter((m) => m.name && m.dose && m.frequency);
}

function setStatus(message, error = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle("error", !!error);
}

function renderList(containerId, items) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  (items || []).forEach((item) => {
    const li = document.createElement("li");
    if (typeof item === "string") {
      li.textContent = item;
    } else {
      li.textContent = `${item.name}: ${item.dose}, ${item.frequency}. ${item.patient_instruction || ""}`;
    }
    container.appendChild(li);
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitBtn.disabled = true;
  setStatus("Generating...");

  const payload = {
    patient_age: Number(document.getElementById("patient_age").value || 0),
    primary_diagnosis: document.getElementById("primary_diagnosis").value.trim(),
    comorbidities: document.getElementById("comorbidities").value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
    discharge_summary: document.getElementById("discharge_summary").value.trim(),
    medications: parseMedications(document.getElementById("medications").value),
    follow_up_instructions: parseLines(document.getElementById("follow_up").value),
    red_flags: parseLines(document.getElementById("red_flags").value),
    target_language: document.getElementById("target_language").value,
    health_literacy_level: "basic",
  };

  try {
    const response = await fetch("/api/v1/discharge-plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Failed to generate plan");
    }

    document.getElementById("plain_summary").textContent = data.plain_language_summary || "";
    document.getElementById("translated_summary").textContent = data.translated_summary || "";
    renderList("medication_schedule", data.medication_schedule || []);
    renderList("red_flags_out", data.red_flags || []);
    renderList("follow_up_out", data.follow_up_plan || []);
    document.getElementById("metadata_out").textContent = JSON.stringify(data.metadata || {}, null, 2);

    setStatus("Done");
  } catch (error) {
    setStatus(`Error: ${error.message}`, true);
  } finally {
    submitBtn.disabled = false;
  }
});

