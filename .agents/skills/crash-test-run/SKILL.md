---
name: crash-test-run
description: Run an approved preset Executor Crash Test scenario and explain its report. Use for F0, F1, F3 or the bundled replay; do not use for arbitrary commands or other fault scenarios.
---

# Run a crash test

Work from the Executor Crash Test repository. Use only the preset F0, F1 and F3 scenarios or `fixtures/f1_demo`. Do not accept custom shell commands, fault scripts or workspace paths as scenario input. Never print keys, authorisation headers or remote URL query strings.

If the user requests a live run, use their named scenario and environment. Otherwise use the network-free recorded fixture:

```sh
uv run crashtest replay fixtures/f1_demo --speed 8
```

For a requested live run, call `uv run crashtest run --fault F0`, `--fault F1` or `--fault F3`. Add `--environment openai_hosted` only when the user chooses the OpenAI-hosted environment. Hosted F1 is an experimental in-sandbox parent signal, not a verified managed-executor kill. Do not run F2 or F4-F12.

Read `run.json` and `report.json` in the run directory printed by the CLI. For replay, use the printed report directory. Explain the verdict and its reason, the root turn and command outcomes, file evidence, disconnect and reconnect evidence, and whether this was live or replay. Treat missing or censored evidence as INCONCLUSIVE. Do not infer signal completion, file survival or recovery from an idle session or a completed turn. Identify rules-based judging as rules and never let it override a deterministic failure. Link the HTML report when available.

For the private live demo's shared Site MCP tools, when available, use `start_test` with the approved hosted preset F0 or F1, then use `get_run_status` and `get_report` to inspect the same session shown on the demo page. Explain that the page only shows stream events received after it connects; consult saved evidence for missing events. End the disposable session with `end_test` when the user is finished reviewing it. Do not call both the MCP tool and CLI for the same requested test: they create separate sessions. Treat F1 as INCONCLUSIVE unless independent evidence supports recovery and survival; a disconnect alone is not proof.
