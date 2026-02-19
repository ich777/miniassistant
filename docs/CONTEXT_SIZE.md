# Context Size (num_ctx)

**Context size** is the maximum context length in tokens (how much conversation + system prompt the model can see). It is set in the config under **Ollama** and is passed to Ollama for each request.

**Where it is configured:**

- **Global:** `providers.ollama.num_ctx` or `providers.ollama.options.num_ctx` (integer, e.g. 8192 or 32768).
- **Per model:** `providers.ollama.model_options.<model_name>.num_ctx` overrides the global value for that model.

**How to show the current context size:** Read the config file (path from the Persistence section, e.g. `miniassistant.yaml`) and report the value of `num_ctx` (under `providers.ollama` or `providers.ollama.options`) and, if present, per-model overrides under `model_options`. You can use exec to run `cat <config_path>` and then tell the user what the current num_ctx (and per-model num_ctx) is.

**How to change it:** Use **save_config** with the updated YAML. Add or change one of:

```yaml
providers:
  ollama:
    num_ctx: 32768
```

or inside options:

```yaml
providers:
  ollama:
    options:
      num_ctx: 32768
```

For a specific model only:

```yaml
providers:
  ollama:
    model_options:
      "llama3.1:8b":
        num_ctx: 16384
```

After saving, tell the user to restart the service. Note: very large values (e.g. 128000) may require enough RAM/VRAM; if the user has errors, suggest lowering num_ctx.
