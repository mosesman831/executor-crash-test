# Executor Crash Test

A small fault-injection harness for teams running `codex exec-server` behind an OpenAI Agents API self-hosted session. It compares the saved turn and command results with files on disk, then checks whether the agent's final answer reflects the evidence. The aim is to make executor recovery behaviour visible before relying on it in production.

Built at the OpenAI DevDay Community Hackathon London on 1 November 2026. This is an experimental tool: results describe the particular run and environment, not a general guarantee about the platform.

## What it runs

- **F0** runs a baseline task that creates three files and records the turn, command and file results.
- **F1** kills the executor process group during a command and restarts it against the same environment. It records the trigger, disconnect and reconnect evidence, command outcome and pre-fault file survival.
- **F3** closes the event stream for 20 seconds without killing the executor, then reconstructs state from the session, saved items and buffered events. Saved items cannot reconstruct every intermediate stream event.

Each run writes `report.json`, a self-contained `report.html`, and its evidence under the configured root. `INCONCLUSIVE` means essential evidence or a confirmed trigger was missing. The Decisions judge uses one request when available; on refusal or error it falls back to labelled, low-confidence rules. Its thresholds are untuned, and its answer cannot override a deterministic failure.

## Setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js and npm, an OpenAI project with access to the Agents API, and a writable directory for runs and disposable workspaces. Install the alpha executor and project dependencies:

```bash
npm install -g @openai/codex@alpha
uv sync
```

Create two separate keys under the same owner and project. Use an application key for the controller and a restricted environment key from the Agents tab for the executor. The environment key is intended only for connecting environments. Agent-generated commands can read it. Follow the [self-hosted security guide](https://developers.openai.com/api/docs/guides/agents-api/environments/security) when creating the key and use a dedicated project with a spend limit.

Create a local `.env`, which is ignored by Git:

```dotenv
OPENAI_API_KEY=your_application_key
OPENAI_EXECUTOR_API_KEY=your_restricted_environment_key
CRASHTEST_ROOT=/absolute/path/to/a/writable/crashtest-directory
```

The controller passes the environment key to the child as `CODEX_API_KEY` and does not put the application key in the child environment. The executor needs outbound access to the endpoints in the [self-hosted guide](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted). Optional settings include `CRASHTEST_AGENT_MODEL`, `CRASHTEST_CODEX_BIN` and `CRASHTEST_DECISIONS=off`.

Run from the repository:

```bash
uv run crashtest run --fault F0
uv run crashtest run --fault F1
uv run crashtest run --fault F3
```

Use `--no-judge` to use rules without a Decisions request, or `--keep-session` to retain the API session for inspection. Exit codes are 0 for PASS or WARN, 1 for FAIL, 2 for ERROR and 3 for INCONCLUSIVE. `uv run pytest -q` runs the offline tests.

The included fixture is synthetic. Replay needs no API keys, executor or network, and its report is labelled as replay:

```bash
uv run crashtest replay fixtures/f1_demo --speed 8 --output ./replay-report
```

## Isolation and evidence

Process cleanup is best effort on the current machine because it has no dedicated `crashexec` user.

Run this on disposable compute. The executor can run shell commands and write files. The child environment is restricted, but same-user processes may still read each other's environment through `/proc`; use separate users or containers to isolate the application key. The default process-group kill is best effort: sandboxed descendants can escape that group, so it is not proof that an entire machine or container stopped. A dedicated executor user or a container or cgroup can provide a stronger boundary.

Events, logs and reports are redacted, and report files omit URLs. Redaction is a precaution, not a guarantee: inspect artefacts before sharing them. Separate the application key from the executor container in CI, use scoped credentials and revoke them when no longer needed.

## What was built today

The harness now supports F0, F1 and F3, JSONL recording, SHA-256 survival metrics, a Decisions honesty check with rules fallback, offline replay, and JSON and HTML reports. A live F0 and a live F3 passed; the live F1 run recorded a failed command and a completed root turn, with reconnect time censored.

## How Codex helped

Codex helped build the Python harness in small tested steps: it implemented the event-stream parser and recovery recorder, the executor supervisor and fault triggers, offline replay, metrics, the Decisions fallback and the report card. It also used test failures and live run evidence to investigate gaps in stream reconstruction and improve the recovery code. Human review and live tests remained necessary to assess the observed results.

## Related work

This project uses OpenAI's [Codex](https://github.com/openai/codex) `exec-server` and [Agents API self-hosted environments](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted). [Agent Chaos Monkey](https://github.com/charles2ke/Agent-Chaos-Monkey) explores resilience and evidence at the agent-tool boundary. Other related projects include mcpfault, ToolChaos and Khaos SDK. They informed the surrounding landscape; this harness focuses on the self-hosted executor lifecycle and the files and saved results around it.

## OpenAI-hosted sandbox

Run `uv run crashtest run --environment openai_hosted --fault F0` or use `--fault F3` to test stream recovery. The hosted mode downloads published files from `/workspace/outputs` and checks their SHA-256 hashes. An executor key is only needed for self-hosted runs. F1 uses an experimental in-sandbox signal aimed at the command parent when it identifies itself as `codex`. It records disconnect and trace evidence, but signal completion, executor identity and file survival remain unconfirmed and the verdict is INCONCLUSIVE.

To compare the crash scenario in both environments, run `uv run crashtest run --fault F1` for the self-hosted executor and `uv run crashtest run --environment openai_hosted --fault F1 --keep-session` for the experimental hosted signal. Open the generated report for evidence. Hosted F1 runs can be replayed offline with `uv run crashtest replay RUN_DIRECTORY`.

## Judges' walkthrough

The prepared walkthrough presents a real recorded OpenAI-hosted run. It does not start a new test, and it labels the observed disconnect and inconclusive verdict. From the repository root, serve it on localhost:

```bash
python3 -m http.server 8000 --bind 127.0.0.1 --directory demo
```

Open `http://127.0.0.1:8000/` and use the buttons to advance through the five steps. The page includes redacted trace and event downloads. The repository also includes the `crash-test-run` Codex skill under `.agents/skills/` for running an approved preset and explaining its report.
