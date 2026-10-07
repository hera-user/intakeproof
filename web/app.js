"use strict";

const $ = (id) => document.getElementById(id);
const FIELDS = ["line_id", "sku", "quantity", "ship_date"];
const state = { inspection: null, result: null, corrections: new Map(), selectedRecord: null, busy: false };

function text(tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  return element;
}

function showError(error) {
  $("error").textContent = error.message || String(error);
  $("error").hidden = false;
  $("error").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function clearError() { $("error").hidden = true; }

function busy(value, message = "") {
  state.busy = value;
  $("load-demo").disabled = value;
  $("file-input").disabled = value;
  $("run").disabled = value;
  $("activity").hidden = !value;
  $("activity").textContent = message;
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-IntakeProof-Token": document.querySelector('meta[name="csrf-token"]').content },
    body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || `Request failed (${response.status})`);
  return data;
}

function encodeBytes(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return btoa(binary);
}

async function inspect(source) {
  clearError();
  state.inspection = null;
  state.result = null;
  state.corrections.clear();
  ["mapping", "results", "evidence", "source-details"].forEach((id) => { $(id).hidden = true; });
  $("date-order").value = "";
  $("date-reason").value = "";
  busy(true, "Reading logical records → proposing a mapping → checking the contract…");
  try {
    const result = await api("/api/inspect", { ...source, delimiter: $("delimiter").value });
    state.inspection = result;
    renderSource(result);
    renderMapping(result);
    if (result.preview) renderResult(result.preview, true);
    $("file-status").textContent = "Original loaded. Review the proposed mapping below.";
    $("mapping").hidden = false;
    if (result.planning_error) showError(new Error(result.planning_error.message));
  } catch (error) {
    $("file-status").textContent = "File rejected. No partial import was released.";
    showError(error);
  } finally { busy(false); }
}

function renderSource(data) {
  $("source-details").hidden = false;
  $("source-name").textContent = data.filename;
  $("source-count").textContent = `${data.input_records} records · ${data.source_bytes.toLocaleString()} bytes`;
  $("synthetic-badge").hidden = !data.synthetic;
  const table = $("source-table");
  const head = document.createElement("tr");
  data.headers.forEach((header) => head.append(text("th", header)));
  table.tHead.replaceChildren(head);
  const rows = data.sample.map((record) => {
    const row = document.createElement("tr");
    data.headers.forEach((header) => {
      const cell = text("td", record.original[header]);
      cell.title = record.original[header];
      row.append(cell);
    });
    return row;
  });
  table.tBodies[0].replaceChildren(...rows);
}

function renderMapping(data) {
  const items = FIELDS.map((field) => {
    const item = text("div", "", "mapping-field");
    const label = text("label", field);
    label.htmlFor = `mapping-${field}`;
    item.append(label, text("span", "↑ from source column", "mapping-arrow small"));
    const select = document.createElement("select");
    select.id = `mapping-${field}`;
    const empty = text("option", "Choose a source column…");
    empty.value = "";
    select.append(empty);
    data.headers.forEach((header) => {
      const option = text("option", header);
      option.value = header;
      select.append(option);
    });
    select.value = data.recipe?.mapping[field].source || "";
    select.addEventListener("change", () => {
      $("mapping-hint").textContent = "Mapping changed. Run again to validate and release a new result.";
      $("run").textContent = "Approve mapping & run →";
    });
    item.append(select, text("p", data.recipe?.mapping[field].evidence || "No automatic proposal. Choose the source field using the original preview.", "small muted"));
    return item;
  });
  $("mapping-fields").replaceChildren(...items);
  $("mapping-mode").textContent = data.planner ? "Local rule proposal · no model call" : "Manual mapping required";
}

function decisions() {
  return { date_order: $("date-order").value || null, date_order_reason: $("date-reason").value, corrections: [...state.corrections.values()] };
}

async function run() {
  if (!state.inspection || state.busy) return;
  clearError();
  busy(true, "Validating the reviewed recipe → preserving row lineage → reconciling every record…");
  try {
    const columns = Object.fromEntries(FIELDS.map((field) => [field, $(`mapping-${field}`).value]));
    const result = await api("/api/run", { session_id: state.inspection.session_id, columns, decisions: decisions(), mapping_approved: true });
    state.result = result;
    renderResult(result, false);
    $("mapping-hint").textContent = "Mapping reviewed. This result and its decision history are now available to export.";
    $("run").textContent = "Run reviewed mapping again →";
    $("results").scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  } catch (error) { showError(error); return false; }
  finally { busy(false); }
}

function renderResult(result, preview) {
  state.result = result;
  $("results").hidden = false;
  const summary = result.summary;
  $("input-count").textContent = summary.input;
  $("accepted-count").textContent = summary.accepted;
  $("review-count").textContent = summary.review;
  $("conservation-equation").textContent = `${summary.input} input = ${summary.accepted} accepted + ${summary.review} in review`;
  $("identifier-summary").textContent = `${summary.identifiers_unchanged}/${summary.identifier_checks} accepted identifiers unchanged${summary.reviewed_identifier_corrections ? ` · ${summary.reviewed_identifier_corrections} reviewed correction` : ""}`;
  $("approval-badge").textContent = preview ? "Preview · mapping not approved" : "Reviewed mapping · evidence saved";
  $("approval-badge").className = `badge ${preview ? "warning" : "success"}`;
  $("queue-badge").textContent = `${summary.review} unresolved`;
  const review = result.records.filter((row) => row.disposition === "review");
  const accepted = result.records.filter((row) => row.disposition === "accepted");
  $("review-empty").hidden = review.length !== 0;
  $("review-table").hidden = review.length === 0;
  $("review-limit").textContent = review.length > 100 ? `Showing the first 100 of ${review.length} review records. The full queue is retained in the evidence bundle.` : "";
  const reviewRows = review.slice(0, 100).map((record) => {
    const row = document.createElement("tr");
    const identity = text("td", record.record_id);
    identity.append(text("span", `Lines ${record.physical_lines.join("–")}`, "record-lines"));
    const ref = text("td", record.values.line_id || "(missing)");
    const evidence = document.createElement("td");
    record.issues.forEach((issue) => {
      const item = text("div", "", "issue");
      item.append(text("span", issue.field, "issue-field"), text("span", issue.message, "issue-message"));
      evidence.append(item);
    });
    const action = document.createElement("td");
    const button = text("button", "Review →", "resolve-button");
    button.type = "button";
    button.setAttribute("aria-label", `Review ${record.record_id}`);
    button.disabled = preview;
    if (preview) button.title = "Approve the mapping before recording row corrections.";
    button.addEventListener("click", () => openCorrection(record));
    action.append(button);
    row.append(identity, ref, evidence, action);
    return row;
  });
  $("review-table").tBodies[0].replaceChildren(...reviewRows);
  $("accepted-badge").textContent = `${accepted.length} ready`;
  $("accepted-table").tBodies[0].replaceChildren(...accepted.slice(0, 100).map((record) => {
    const row = document.createElement("tr");
    row.append(text("td", record.record_id), ...FIELDS.map((field) => text("td", record.values[field])));
    return row;
  }));
  $("evidence").hidden = preview;
  if (!preview) {
    const base = `/api/download/${encodeURIComponent(result.run_id)}/`;
    $("download-bundle").href = `${base}bundle.zip`;
    $("download-import").href = `${base}import.csv`;
    $("download-audit").href = `${base}audit.json`;
    $("download-scope").textContent = `${accepted.length} accepted records in reviewed_import.csv. ${review.length} unresolved records are preserved separately in review.json.`;
    $("source-hash").textContent = result.source.source_sha256;
    $("run-id").textContent = result.run_id;
    $("executor-description").textContent = `Local Python · ${result.elapsed_ms} ms for parsing, validation and transformation. No cloud call.`;
    $("history-description").textContent = result.parent_run_id ? `New result, linked to ${result.parent_run_id}. ${result.decisions.corrections.length} recorded correction(s).` : "Initial result. Original source preserved.";
    $("audit-preview").textContent = JSON.stringify({ run_id: result.run_id, source: result.source, summary: result.summary, planner: result.planner, executor: result.executor, decisions: result.decisions, claims: result.claims }, null, 2);
  }
}

function openCorrection(record) {
  if (state.busy) return;
  $("correction-reason").setCustomValidity("");
  state.selectedRecord = record;
  $("correction-title").textContent = `Review ${record.record_id}`;
  $("correction-fields").replaceChildren(...FIELDS.map((field) => {
    const label = text("label", field);
    const input = document.createElement("input");
    input.id = `correction-${field}`;
    input.type = "text";
    input.maxLength = 1000;
    input.value = record.values[field];
    const sourceColumn = state.result.recipe.mapping[field].source;
    label.append(input, text("small", `Original: ${record.original[sourceColumn] || "(empty)"}`));
    return label;
  }));
  $("correction-reason").value = state.corrections.get(record.record_id)?.reason || "";
  $("correction-dialog").showModal();
}

$("load-demo").addEventListener("click", async () => {
  if (state.busy) return;
  try { await inspect(await api("/api/demo")); }
  catch (error) { showError(error); }
});

$("file-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) return showError(new Error("Choose a file of at most 2 MiB."));
  await inspect({ filename: file.name, synthetic: false, source_base64: encodeBytes(await file.arrayBuffer()) });
});

$("run").addEventListener("click", run);
$("close-dialog").addEventListener("click", () => $("correction-dialog").close());
$("cancel-dialog").addEventListener("click", () => $("correction-dialog").close());
$("correction-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const record = state.selectedRecord;
  if (!record) return;
  const previous = state.corrections.get(record.record_id);
  const values = { ...(previous?.values || {}) };
  FIELDS.forEach((field) => {
    const value = $(`correction-${field}`).value;
    if (value !== record.values[field]) values[field] = value;
  });
  if (!Object.keys(values).length) {
    $("correction-reason").setCustomValidity("Change a value before saving a correction.");
    $("correction-reason").reportValidity();
    return;
  }
  $("correction-reason").setCustomValidity("");
  state.corrections.set(record.record_id, { record_id: record.record_id, values, reason: $("correction-reason").value });
  $("correction-dialog").close();
  if (!await run()) {
    if (previous) state.corrections.set(record.record_id, previous);
    else state.corrections.delete(record.record_id);
  }
});
$("correction-reason").addEventListener("input", () => $("correction-reason").setCustomValidity(""));
