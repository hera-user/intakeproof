# IntakeProof

**Supplier formats change. Every record should still be accounted for.**

IntakeProof proposes a bounded column mapping, checks it against a shipment import contract, and separates accepted records from unresolved cases. A reviewer can correct a value with a reason, revalidate, and export the import together with its evidence.

The browser workflow defaults to local rules and local Python. Optional OpenAI Responses and Agent37 execution adapters are connected to both the CLI and browser review flow, and tested with explicit test doubles. **A live sponsor run has not yet been verified.** Development assistance from Codex does not count as an in-product sponsor integration.

The [70-second walkthrough](docs/demo.mp4) uses actual app captures, synthetic data and an explicitly illustrative review decision. Its [matching evidence bundle](demo/browser-reviewed/evidence.zip) contains the exact shown result. See [the verification report](TEST-REPORT.txt) and [integration status](INTEGRATION-STATUS.json).

## Run it

Use Python 3.10 or later; development and verification used Python 3.12. No packages need installing.

From this directory:

```console
python -m intakeproof serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The server listens only on loopback. Click **Explore the example**, review the four source-column choices, then **Approve mapping & run**.

The supplied data is an original, explicitly synthetic demonstration. Its ten input records produce **4 accepted and 6 review records**. Both members of a duplicated reference are held; neither wins by appearing first. The ambiguous `03/04/2026` value stays in review.

For an illustrative review, set record `r000003` to `2026-04-03` and state that this is a synthetic demonstration decision. Revalidation produces **5 accepted and 5 review records**, with a new result ID linked to the earlier result. No real supplier confirmation is implied.

Download the evidence before stopping the server. Browser sessions are kept in memory: restarting the server clears those sessions. Previously downloaded artifacts remain independent and usable.

## Reproduce without the browser

```console
python -m intakeproof demo --out demo-output
python -m unittest discover -s tests -v
```

The first command creates a candidate import. To record your review of the mapping, use `--approve-mapping` and a new output directory. Existing nonempty output directories are refused.

For a supplied file and an already reviewed recipe:

```console
python -m intakeproof run --input supplier.csv --recipe recipe.json --out reviewed-run --approve-mapping
```

Use `--decisions decisions.json` to record date-order evidence or row corrections. Decisions are strings, never executable expressions:

```json
{
  "corrections": [
    {
      "record_id": "r000003",
      "values": {"ship_date": "2026-04-03"},
      "reason": "Synthetic demonstration decision; no actual supplier confirmation."
    }
  ]
}
```

## What the handoff contains

| Artifact | Purpose |
| --- | --- |
| `original.csv` | Byte-identical input, including its BOM and line endings if present |
| `reviewed_import.csv` | Accepted records after explicit mapping review; otherwise named `candidate_import.csv` |
| `review.json` | Every unresolved record, original values, candidate values and reasons |
| `audit.json` | Per-record lineage, physical line ranges, mapping, corrections, parent result and runtime receipt |
| `recipe.json` / `contract.json` | Editable mapping and exact target constraints |
| `report.html` | Standalone, readable evidence report |
| `SHA256SUMS.txt` | Recomputable hashes for the bundle files |

The row invariant is `input record identities = accepted identities ∪ review identities`, with disjoint output sets. Logical CSV records can span multiple physical lines. Bad quoting, duplicate headers, inconsistent widths and unsupported encoding reject the entire input explicitly; no partial success is presented.

Identifiers stay strings. A valid identifier keeps its leading zeros. Formula-like identifiers are held under the narrow contract, rather than being silently changed by prefixing an apostrophe. All original values remain available in JSON. HTML output is escaped.

## Agent boundary

The planner proposes data, never code. Each of the four targets maps to a different existing source column with one permitted operation. The validator rejects an invalid proposal and can return feedback for one repair attempt; a second invalid proposal stops the run. Mapping approval and date interpretation are separate review decisions.

`OpenAIPlanner` sends a bounded profile to the Responses API using strict JSON Schema and no tools. It captures the actual response ID, returned model, usage and response hash. Refusal, incomplete output or unavailable service is an explicit failure. The configured model must be supplied; no model is silently chosen.

`Agent37Executor` uploads the trusted worker, engine and data to a generated job directory on an **existing authorized instance**, executes a fixed command, downloads the result and compares it with a deterministic local replay. A nonzero exit, truncated output, missing completion marker or mismatched artifact is a failure. A local result is never relabelled as cloud execution.

The adapters do not sign up, provision an instance, buy credits, configure billing or read credential stores. They remain opt-in. Live access must be verified separately; test doubles have `live_model_call: false` and `cloud_call: false`.

## Optional live configuration

Use only after verifying legitimate free service credits and the absence of card charges or automatic top-ups. The flag below records the operator's verification; it cannot itself prove the account's billing state. No live run is claimed in this version.

Provide authorized credentials in the process environment using your normal secure local workflow. Do not commit or place keys in a browser form, source file, screenshot or evidence bundle.

| Environment variable | Meaning |
| --- | --- |
| `INTAKEPROOF_OPENAI_API_KEY` | Authorized OpenAI API credential |
| `INTAKEPROOF_OPENAI_MODEL` | Explicit model supporting Responses structured outputs |
| `INTAKEPROOF_AGENT37_API_KEY` | Authorized Agent37 workspace credential |
| `INTAKEPROOF_AGENT37_INSTANCE_ID` | Existing running instance permitted for this work |

The synthetic fixture can exercise both adapters through the CLI:

```console
python -m intakeproof demo --planner openai --executor agent37 --free-access-verified --out live-candidate
```

Review that candidate's `recipe.json` before using `--approve-mapping`. Sending a custom input through the CLI additionally requires `--allow-external-data`.

The same providers can run through the browser review interface:

```console
python -m intakeproof serve --planner openai --executor agent37 --free-access-verified --max-provider-jobs 3
```

Provider-enabled browser mode accepts only the byte-identical bundled synthetic example. A filename or a `synthetic` label cannot bypass this check. Exploring the example sends its headers and two bounded sample records to OpenAI. Approving the mapping or saving a correction sends the example, mapping and recorded review decisions to Agent37; keep those decisions synthetic. Credentials are supplied only through the local process environment, never the browser.

There are at most three planning jobs and three execution jobs per server process; `--max-provider-jobs` can lower either limit together to one or two. Failed attempts count, and loading another source session cannot reset the limits. Each planning job makes at most two proposals. Each Agent37 execution checks the supplied instance, uploads three files, executes once and fetches one result; there is no blind retry after a timeout. These request caps do not replace verification of free account access or cap instance storage costs.

The runtime panel distinguishes configuration, a local preview, explicit test doubles and a completed provider receipt. Inspection never starts remote execution. A failed model proposal preserves the source and offers explicit manual mapping; that decision retains the failure in its audit. An unverified failed proposal has a null live-call flag, because a timeout does not prove that no request reached the service. A failed execution does not save a replacement result or relabel a local fallback as cloud success. Any earlier download still refers to its original successful result.

Remote temporary files remain on the supplied instance. Export required evidence and clean up only resources created for this entry. Stopping or sleeping an Agent37 instance may still incur storage use; the tool does not delete pre-existing resources automatically.

## Scope and limits

- One shipment contract: `line_id`, `sku`, `quantity`, `ship_date`; 2 MiB and 20,000 records maximum; at most 100 source headers of 128 characters each.
- The browser shows the first 100 rows of each result set. Full results are retained in the downloaded bundle; CLI decisions can address any source record.
- “Reviewed” means the operator approved the mapping and recorded any corrections. It does not mean every business fact was independently confirmed. Reviewer identity is not authenticated.
- Row accounting establishes completeness, not semantic mapping correctness. Hashes support comparison with saved evidence; they are not a signed third-party attestation.
- No customer, revenue, time-saved or prize-win claim is made. Timing measures the actual operation stated in the audit.

## References

Integration adapters follow the official [OpenAI structured-output documentation](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses), [Agent37 execution API](https://www.agent37.com/docs/agents-api/exec) and [Agent37 file API](https://www.agent37.com/docs/agents-api/files). The product design and synthetic fixture are original to this entry; competitor submissions were not used.
