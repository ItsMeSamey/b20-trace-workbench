# Offline protocol v1

All cases are authored inputs. No fixture contains executable commands. JSON object keys must be unique. Unknown or missing fields are rejected. Numeric quantities use canonical unsigned decimal strings (`"0"` or digits without leading zeros), never JSON numbers.

Input wrapper: `{"format":"b20-workbench-v1","origin":"authored","cases":[...]}`. Each case has a unique nonempty `id` and `kind` of `state` or `abi`. Empty case, step and raw arrays are valid. A missing/duplicate case identity is a document error, exit 2.

## State traces

```json
{"id":"example","kind":"state","initial":{"at":"0","active":"1000000000000000000","pending":null},"steps":[{"id":"q","at":"0","op":"query","raw":["0","1"]}]}
```

Pending state, when supplied, is `{"value":"...","effective_at":"..."}`. Multipliers are 1 through `2^128-1`; timestamps are 0 through `2^64-1`; raw amounts are 0 through `2^256-1`. State is semantic active/pending state, not storage getter state. Before each operation, mature a pending multiplier whose time is at or before the step time. Timestamps cannot move backwards.

Every step has a unique `id` and the fields `at` and `op`, with exactly these additional fields:

| Operation | Fields | Behavior |
| --- | --- | --- |
| `query` | `raw` array | Compute `floor(raw * active / 10^18)` |
| `schedule` | `value`, `effective_at` | Set future pending multiplier if none is live |
| `instant` | `value` | Replace active multiplier if none is pending |
| `cancel` | none | Clear a live pending multiplier |
| `legacy_reconstruct` | none | Explicitly unsupported |

A schedule time must be strictly in the future. The whole case is validated before evaluation. Successful output steps have `id`, `active`, `pending` and `conversions`; conversions contain `raw` and `ui` strings and are empty for nonquery steps.

An ok result has `id`, `kind:"state"`, `status:"ok"`, and `steps`. A terminal unsupported result additionally has `at_step` and `reason`, retaining only prior successful steps. Reasons are `pending_schedule_conflict`, `pending_instant_conflict`, `no_live_pending_cancel`, `legacy_emitter_ambiguous` or `product_overflow_unqualified`. The last means any query product exceeds uint256; no partial conversions from that step are returned. This domain limit makes no claim about actual EVM overflow handling.

Malformed cases return `{"id":"...","kind":"state","status":"invalid","reason":"invalid_case"}`. No transaction authorization or revert simulation is performed.

## ABI cases

ABI cases have exactly `id`, `kind:"abi"`, `topics` (one string) and `data` (hex bytes).

| Event | Topic | Unindexed uint256 words |
| --- | --- | --- |
| `UIMultiplierUpdated` | `0x2205df4534432b2f60654a3fdb48737ffdaf3e9edb1a498bd985bc026b15b055` | old, new, effectiveAt |
| `UIMultiplierUpdateCancelled` | `0x883856335ba5f60c18b9817c4505d3c7d3f6223dcf39516b30c508c46a5e1cad` | cancelled multiplier, cancelled effectiveAt |

Input hex accepts either letter case with a `0x` prefix. Ok results contain `id`, `kind`, `status`, `event`, `values` (decimal strings), and `encoded:{topics,data}` with lowercase canonical hex. ABI words allow the full uint256 range, independently of setter bounds.

The legacy topic `0x4dbe4840d7465bd162f67814cea0b519567a2e0e578bcde61e7f4ced361e5a3d` with one word returns `status:"unsupported",reason:"legacy_emitter_ambiguous"`. Unknown topics, wrong lengths, additional indexed topics or invalid hex return the invalid-case shape with `kind:"abi"`. These format checks do not prove emitter identity or event occurrence and never infer a state operation.

## Result comparison

Result wrapper: `{"format":"b20-workbench-results-v1","origin":"authored","results":[...]}`. `evaluate` retains input case order. `compare EXPECTED ACTUAL` validates both documents and record structures before matching unique IDs. Expected outputs are supplied separately, never computed by the comparator.

Case record order may differ. Nested step/array order, keys, types and values must match exactly. Duplicate records, missing/extra IDs and valid-but-wrong values produce comparison failures (exit 1). Schema errors produce exit 2. A matching expected unsupported/invalid result is success. An encoded ABI payload is structurally validated here; exact comparison with the expected object is what detects a well-formed but incorrect encoding or value.
