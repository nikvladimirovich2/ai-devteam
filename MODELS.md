# Which model for which role

Each role can use its own model (`<ROLE>_MODEL` in `.env`; empty = `LLM_MODEL`) and always uses its own key.
The roles load the model very differently, so a mixed setup is usually both cheaper and better than
«the biggest model everywhere».

## What each role needs

| Role | Typical load | What matters most | Tool calls |
|---|---|---|---|
| **Team Lead** | 2–3 calls per task (plan, final report) | instruction following, **strict JSON** for the plan, Russian/English text | none |
| **Architect** | 1 long run | reasoning, system design, long coherent document (`ARCHITECTURE.md`) | few (write files) |
| **Backend** | the most tokens (30–50 %) | **coding quality**, tool calling, debugging from test output | many |
| **Frontend** | many tokens | HTML/CSS/JS, API integration | many |
| **QA** | several rounds | reading code, writing tests, **honest verdict JSON**, shell | many |
| **DevOps** | short runs + CI fix rounds | Docker/CI YAML, reading CI logs, root-cause of build failures | medium |

Rules of thumb:

1. **Tool calling is mandatory** for all roles except Team Lead. A model that only *describes* tool use
   is useless here, no matter how smart it is. Check with `./scripts/check.sh` (step 4).
2. **Context ≥ 32k** for Backend/Frontend/QA (they read files and long test output). 64k+ helps on bigger projects.
3. **Coder models for Backend/Frontend/DevOps**, a stronger general/reasoning model for Architect/QA,
   anything reliable with JSON for Team Lead.
4. Reasoning («thinking») models are good for Architect and QA, but they are slow and spend many output
   tokens — avoid them for Backend/Frontend loops unless they are fast on your hardware.
5. Keep the model **identical across a whole task**: switching models mid-task causes inconsistent code style.

## Recommended setups (open-weight, self-hosted)

### A. One GPU server, one model (simplest)

```env
LLM_MODEL=hosted_vllm/Qwen/Qwen3-32B-AWQ        # or Qwen2.5-Coder-32B-Instruct
```

All roles on one model. Qwen3-32B: good tool calling via `--tool-call-parser hermes`, 32k context.
Turn off long «thinking» if the server supports it (`/no_think` or `enable_thinking=false`) — it noticeably
speeds up Backend/Frontend.

### B. Two models: coder + reasoning (best quality/cost)

```env
LLM_MODEL=hosted_vllm/Qwen/Qwen2.5-Coder-32B-Instruct     # backend, frontend, devops
ARCHITECT_MODEL=hosted_vllm/Qwen/Qwen3-32B-AWQ            # design
QA_MODEL=hosted_vllm/Qwen/Qwen3-32B-AWQ                   # tests + verdict
LEAD_MODEL=hosted_vllm/Qwen/Qwen3-14B                     # plan/report — smaller is enough
```

### C. Big server (≥ 2×80 GB)

| Role | Model |
|---|---|
| Architect, QA | Qwen3-235B-A22B / DeepSeek-V3 / Llama-3.3-70B-Instruct |
| Backend, Frontend | Qwen2.5-Coder-32B / Qwen3-Coder-30B-A3B / DeepSeek-Coder-V2 |
| DevOps | the coder model (CI/Docker YAML is code) |
| Team Lead | any 14B–32B instruct model with stable JSON |

### D. Small hardware (one 24 GB GPU)

Qwen2.5-Coder-14B-Instruct or Qwen3-14B for all roles, 32k context. Expect more QA/CI rounds;
keep tasks small (one service, a few endpoints).

## Cloud / API models

If your corporate gateway exposes them (LiteLLM names):

| Role | Strong | Cheaper |
|---|---|---|
| Architect, QA | `anthropic/claude-sonnet-*`, `openai/gpt-4.1`, `gemini/gemini-2.5-pro` | `openai/gpt-4.1-mini` |
| Backend, Frontend, DevOps | `anthropic/claude-sonnet-*`, `openai/gpt-4.1` | `openai/gpt-4.1-mini`, `deepseek/deepseek-chat` |
| Team Lead | `openai/gpt-4.1-mini`, `gemini/gemini-2.5-flash` | same |

GigaChat / YandexGPT: usable for Team Lead (Russian text, JSON); for coding roles check tool calling
with `scripts/check.sh` first — support depends on the model version.

## Not recommended

- Models without function calling, base (non-instruct) models.
- < 7B models for coding roles — they loop on tools and invent results.
- Context < 16k — agents lose track of files they already wrote.

## How to choose on your own hardware

1. Run `./scripts/check.sh` — all roles must pass steps 3–4.
2. Run the same small task (README «Example task») with 2–3 candidate setups.
3. Compare the **final reports**: QA rounds, CI rounds, whether CI ended green, total tokens and duration.
   Fewer rounds at similar token count = the better model for that role.
