"use strict";
const el = id => document.getElementById(id);
const fields = ["line_id", "sku", "quantity", "ship_date"];
const fieldLabels = {line_id:"Reference",sku:"Product",quantity:"Quantity",ship_date:"Ship date"};
const issueTitles = {ambiguous_date:"Two valid dates. One decision needed.",invalid_date:"This calendar date does not exist.",duplicate_line_id:"Two records share this reference.",invalid_identifier:"This identifier fails the import contract.",required:"A required value is missing.",invalid_quantity:"The quantity cannot enter the import."};
const issueShort = {ambiguous_date:"Date order",invalid_date:"Invalid date",duplicate_line_id:"Duplicate",invalid_identifier:"Identifier",required:"Missing value",invalid_quantity:"Quantity"};
let data, mode = "baseline", filter = "all", selected = "r000003";
function node(tag, text, className) { const result = document.createElement(tag); if (text !== undefined) result.textContent = text; if (className) result.className = className; return result; }
function valueText(value) { return value === "" ? "∅" : String(value); }
function current() { return data[mode]; }
function selectRecord(id, control) {
  selected = id;
  if (filter !== "all" && current().records.find(record => record.record_id === id).disposition !== filter) filter = "all";
  render();
  const target = control === "strip" ? `#record-strip [data-record-id="${id}"]` : `#ledger-body [data-record-id="${id}"] button`;
  document.querySelector(target)?.focus({preventScroll:true});
  if (window.matchMedia("(max-width:900px)").matches) document.querySelector(".inspector").scrollIntoView({block:"start"});
}
function switchMode(nextMode) {
  mode = nextMode;
  if (filter !== "all" && current().records.find(record => record.record_id === selected).disposition !== filter) filter = "all";
  render();
}
function render() {
  const run = current(), summary = run.summary;
  el("total-count").textContent = summary.input; el("ready-count").textContent = summary.accepted; el("held-count").textContent = summary.review;
  el("filter-all").textContent = summary.input; el("filter-review").textContent = summary.review; el("filter-ready").textContent = summary.accepted;
  el("accounting-status").textContent = `${summary.input} / ${summary.input} accounted for`;
  for (const name of ["baseline", "reviewed"]) el(`mode-${name}`).setAttribute("aria-pressed", String(name === mode));
  document.querySelectorAll("[data-filter]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.filter === filter)));
  const strip = el("record-strip"); strip.replaceChildren();
  run.records.forEach((record, index) => {
    const ready = record.disposition === "accepted", block = node("button", String(index + 1).padStart(2,"0"), `record-block ${ready ? "ready" : "held"}`);
    block.type = "button"; block.dataset.recordId = record.record_id; block.setAttribute("aria-label", `Inspect row ${index + 1}: ${ready ? "ready" : "held"}`); block.setAttribute("aria-pressed", String(record.record_id === selected));
    block.addEventListener("click", () => selectRecord(record.record_id, "strip")); strip.append(block);
  });
  const body = el("ledger-body"); body.replaceChildren();
  run.records.forEach((record, index) => {
    if (filter !== "all" && record.disposition !== filter) return;
    const row = node("tr", undefined, record.record_id === selected ? "selected" : ""); row.dataset.recordId = record.record_id;
    row.append(node("td", String(index + 1).padStart(2,"0")));
    const referenceCell = node("td"), button = node("button", valueText(record.values.line_id), "row-select"); button.type = "button";
    button.setAttribute("aria-label", `Inspect row ${index + 1}, reference ${record.values.line_id}`); button.setAttribute("aria-pressed", String(record.record_id === selected));
    button.addEventListener("click", () => selectRecord(record.record_id, "ledger")); referenceCell.append(button); row.append(referenceCell);
    row.append(node("td", valueText(record.values.sku)), node("td", valueText(record.values.quantity), "numeric"));
    row.append(node("td", valueText(record.values.ship_date), record.issues.some(issue => issue.code === "ambiguous_date") ? "date-ambiguous" : ""));
    const decision = node("td"); decision.append(node("span", record.disposition === "accepted" ? "Ready" : issueShort[record.issues[0].code] || "Review", `decision ${record.disposition === "accepted" ? "ready" : "held"}`));
    row.append(decision); body.append(row);
  });
  if (!body.children.length) { const row = node("tr"), cell = node("td", "No records in this view.", "no-results"); cell.colSpan = 6; row.append(cell); body.append(row); }
  renderInspector(run.records.find(record => record.record_id === selected));
  el("bundle-link").href = `assets/${mode === "baseline" ? "baseline" : "reviewed"}-evidence.zip`;
  el("import-link").href = `assets/${mode === "baseline" ? "baseline" : "reviewed"}-import.csv`;
  el("export-heading").textContent = `${summary.accepted} records in the import. ${summary.review} retained for review.`;
  el("export-description").textContent = mode === "baseline" ? "Baseline run · reviewed mapping · original preserved" : "Reviewed example · one recorded correction · original preserved";
  el("run-identity").textContent = `Displayed run: ${run.run_id}`;
}
function renderInspector(record) {
  const ready = record.disposition === "accepted";
  el("selected-status").className = `decision ${ready ? "ready" : "held"}`; el("selected-status").textContent = ready ? "Ready" : "Held";
  el("inspector-title").textContent = record.values.line_id || record.record_id;
  const [firstLine, lastLine] = record.physical_lines;
  el("selected-location").textContent = `${record.record_id} · source ${firstLine === lastLine ? `line ${firstLine}` : `lines ${firstLine}–${lastLine}`}`;
  const summary = el("issue-summary"); summary.replaceChildren(); summary.className = `issue-summary ${ready ? "ready" : ""}`;
  if (ready) { summary.append(node("h4", record.changes.some(change => change.kind === "reviewer_correction") ? "Accepted after a recorded review." : "This record passes the contract."), node("p", "Included in the import. Original source values remain in the audit.")); }
  else record.issues.forEach(issue => { summary.append(node("h4", issueTitles[issue.code] || "Review required."), node("p", issue.message)); });
  const comparison = el("field-comparison"); comparison.replaceChildren(); const head = node("div", undefined, "field-head");
  ["Field", "Original", "", "Result"].forEach(text => head.append(node("span", text))); comparison.append(head);
  fields.forEach(field => {
    const before = record.original[current().recipe.mapping[field].source], after = record.values[field];
    const row = node("div", undefined, `field-row ${before !== after ? "changed" : ""} ${record.issues.some(issue => issue.field === field) ? "issue" : ""}`);
    row.append(node("span", fieldLabels[field], "field-name"), node("span", valueText(before), "field-value"), node("span", "→", "field-arrow"), node("span", valueText(after), "field-value target")); comparison.append(row);
  });
  const correction = record.changes.find(change => change.kind === "reviewer_correction"), decision = el("recorded-decision"); decision.hidden = !correction; decision.replaceChildren();
  if (correction) decision.append(node("strong", "Recorded reason"), node("p", correction.reason));
  el("show-correction").hidden = !(mode === "baseline" && record.record_id === "r000003");
  el("correction-context").textContent = record.record_id === "r000003" ? "The example review chooses 3 April 2026. This is an illustrative decision, not supplier confirmation." : "This page inspects saved evidence. Make new review decisions in the local app.";
  el("supplier-note").textContent = record.original["Supplier note"];
}
function renderMapping() {
  const list = el("mapping-list");
  fields.forEach(field => { const rule = data.reviewed.recipe.mapping[field], details = node("details", undefined, "mapping-item"), summary = node("summary"); summary.append(node("span", rule.source), node("span", "→"), node("code", field), node("span", "+")); details.append(summary, node("p", `${rule.evidence} Operation: ${rule.operation}.`)); list.append(details); });
}
el("mode-baseline").addEventListener("click", () => data && switchMode("baseline"));
el("mode-reviewed").addEventListener("click", () => data && switchMode("reviewed"));
el("show-correction").addEventListener("click", () => switchMode("reviewed"));
document.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => {
  if (!data) return;
  filter = button.dataset.filter;
  const visible = current().records.filter(record => filter === "all" || record.disposition === filter);
  if (visible.length && !visible.some(record => record.record_id === selected)) selected = visible[0].record_id;
  render();
}));
fetch("assets/runs.json").then(response => { if (!response.ok) throw new Error("Evidence unavailable"); return response.json(); }).then(runs => {
  if (!runs.baseline || !runs.reviewed || runs.baseline.records.length !== 10 || runs.reviewed.records.length !== 10 || runs.baseline.source.source_sha256 !== runs.reviewed.source.source_sha256) throw new Error("Unexpected demo evidence");
  data = runs; renderMapping(); render(); el("explorer-content").setAttribute("aria-busy", "false");
}).catch(() => { el("load-error").hidden = false; el("explorer-content").hidden = true; });
