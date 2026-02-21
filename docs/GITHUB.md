# GitHub API

## Token

The GitHub token is stored as a **top-level `github_token`** key in the config and **automatically injected as `$GH_TOKEN` and `$GITHUB_TOKEN`** into every `exec` call. You do not need to extract it manually — just use `$GH_TOKEN` in curl commands.

To save the token (use `save_config`, never edit the file directly):
```yaml
github_token: github_pat_xxx...
```

**NEVER put it under `providers.github` — that key does not exist.**

## Using the API

Always use the **GitHub REST API via `curl`** — not `gh` CLI (requires separate auth setup).

```sh
# Public repo (no auth needed)
curl -s https://api.github.com/repos/OWNER/REPO/issues

# Private repo or authenticated (use $GH_TOKEN from env)
curl -s -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/repos/OWNER/REPO/issues
```

## Common endpoints

```sh
# Repo info
curl -s https://api.github.com/repos/OWNER/REPO

# Open issues
curl -s "https://api.github.com/repos/OWNER/REPO/issues?state=open&per_page=20"

# Pull requests
curl -s "https://api.github.com/repos/OWNER/REPO/pulls?state=open&per_page=20"

# Create issue
curl -s -X POST -H "Authorization: Bearer $GH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Title","body":"Body"}' \
  https://api.github.com/repos/OWNER/REPO/issues

# Comments on an issue
curl -s https://api.github.com/repos/OWNER/REPO/issues/NUMBER/comments

# File contents (decoded from base64)
curl -s https://api.github.com/repos/OWNER/REPO/contents/PATH | jq -r '.content' | base64 -d
```

## Parsing JSON

Use `jq` (install with `apt install jq` if missing):

```sh
# Extract field
curl -s https://api.github.com/repos/OWNER/REPO | jq '.description'

# List issue titles
curl -s "https://api.github.com/repos/OWNER/REPO/issues?per_page=20" | jq '.[].title'

# Grep a specific field
curl -s "https://api.github.com/repos/OWNER/REPO/issues" | jq -r '.[].title' | grep -i keyword
```

## Rules

- **Never echo `$GH_TOKEN`** — never print, log, or include it in your response.
- For **public repos**: auth optional but increases rate limit (5000 req/h vs 60/h).
- For **private repos**: always pass `-H "Authorization: Bearer $GH_TOKEN"`.
- If the token is missing or empty, tell the user: `save_config {github_token: 'ghp_...'}` then restart miniassistant.
