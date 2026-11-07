# B20 offline compatibility workbench

Check authored B20 multiplier traces and canonical event payloads, then compare your implementation's result JSON with independently authored expected outputs. The tool adds a repeatable compatibility workflow and field-level mismatch reports to the documented helper calculations.

Python 3.10+ syntax is required; Python **3.14.8** is the tested runtime. There are no third-party dependencies, network calls, wallets, credential loading or executable plugins.

```sh
python3 -m unittest -v test_workbench
python3 b20workbench.py inspect-metadata fixtures/metadata/captured-page-projection.json
python3 b20workbench.py evaluate fixtures/authored/fixtures.json --output my-results.json
python3 b20workbench.py compare fixtures/authored/expected.json my-results.json
```

Choose a new output path each time. Existing files are never overwritten. `evaluate` does not read expected outputs. To check another implementation, export its results using [PROTOCOL.md](PROTOCOL.md) and pass that file to `compare`; the workbench never executes adapters or fixture-supplied commands.

## Results and errors

- Evaluation exit **0**: the document was processed, including explicit invalid or unsupported case outcomes.
- Comparison exit **0**: every expected result matches exactly.
- Comparison exit **1**: missing, extra, duplicate or mismatched result records.
- Exit **2**: malformed document, malformed result record or file error.

Expected unsupported/invalid outcomes can pass conformance. A JSON number where a decimal string is required is malformed; a valid decimal string with the wrong computed value is a mismatch. Cases match by unique ID, while nested values and step order remain exact.

## Coverage and evidence

The independently authored pack contains **40 synthetic cases**: 30 state traces and 10 ABI cases. It covers integer precision, floor conversion, scheduled maturity without an event, cancellation, supported immediate updates, canonical payloads and explicit unsupported boundaries. The evaluator uses semantic active/pending state, not an asserted view of post-maturity raw storage.

Independent execution matched all 40 cases but found an initial comparison bug: two ID-only malformed result objects could compare as equal. The corrected comparator validates both result schemas. Its independent focused review passed **19/19** CLI checks. The evaluator was unchanged, so the prior independent 40-case run was reused. The corrected project also passed **29 unit tests**. Clean-package command results are recorded separately in `evidence/`; these are implementer reproduction, not an additional independent run.

Four authored faulty-result examples demonstrate detection of relative-update confusion, event-only activation, floating-point rounding and ignored cancellation. They are not observed SDK defects, and this project claims no speed advantage over official helpers.

The metadata fixture is a factual projection of an actual **100-record public API page**, captured on 2026-11-07. Descriptions and icon links were removed; retained values and the next-page cursor are unchanged. It is partial, not a complete or currently refreshed inventory. [PROVENANCE.json](PROVENANCE.json) pins its source and projection. Display metadata is not an exact raw-state or transition-history oracle.

Pending schedule replacement/override, ambiguous legacy-event reconstruction and products exceeding uint256 remain unsupported. No live historical state was captured: the attempted public RPC acquisition returned 403 and was not retried through another route. This work does not prove log completeness, production contract behavior outside the stated domain, market value or financial correctness.

Code, tests and authored cases are covered by [LICENSE](LICENSE). See [NOTICE.md](NOTICE.md) for the factual sample and source attribution. This package is a developer-tool artifact, not evidence of a contest entry, award or payment.
