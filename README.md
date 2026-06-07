Evidence-aware CodeOps Agent is a local-first agent that converts issues into small, tested patch candidates with explicit contracts, impact boundaries, verification evidence, and cost traces.

## LLM API support

LLM calls are disabled by default. To enable bounded fallback patch generation with Kimi:

```bash
export MOONSHOT_API_KEY=sk-...
uv run codeops run \
  --repo /path/to/repo \
  --issue /path/to/issue.md \
  --out .runs/kimi_demo \
  --llm-provider kimi
```

For Kimi Code:

```bash
export KIMI_API_KEY=sk-...
uv run codeops run \
  --repo /path/to/repo \
  --issue /path/to/issue.md \
  --out .runs/kimi_code_demo \
  --llm-provider kimi-code
```

Kimi Platform and Kimi Code use separate keys and endpoints. `kimi` uses
`MOONSHOT_API_KEY`, model `kimi-k2.6`, and `https://api.moonshot.cn/v1`.
`kimi-code` uses `KIMI_API_KEY`, model `kimi-for-coding`, and
the Anthropic-compatible endpoint `https://api.kimi.com/coding/`.
Kimi Code's OpenAI-compatible endpoint requires an allowlisted coding-agent
client, so CodeOps defaults to Anthropic Messages for `kimi-code`.

The LLM receives only redacted context from the AcceptanceContract, PatchPlan, ImpactEnvelope, and allowed files. Generated diffs still pass through patch policy and tests before verification.
