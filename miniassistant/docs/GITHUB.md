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

## Fetching a specific issue or PR

When the user asks about a GitHub issue, PR, or repo — **always use the API via `exec: curl`**. NEVER use `web_search` for GitHub issues/PRs.

**Steps:**
1. Extract OWNER, REPO, NUMBER from the URL or question
2. Fetch: `curl -s "https://api.github.com/repos/OWNER/REPO/issues/NUMBER" | jq '{title: .title, state: .state, user: .user.login, labels: [.labels[].name], body: .body}'`
3. Summarize: title, author, labels, state, and a short summary of the body

**Examples of user requests → what to do:**
- "Gib mir Issue #4 von user/repo" → `curl -s https://api.github.com/repos/user/repo/issues/4`
- "https://github.com/user/repo/issues/4" → extract user, repo, 4 → use API
- "Was gibt's Neues in user/repo?" → fetch recent issues + PRs via API

**If the API returns an error:** try without auth header (public repo), or with auth (private repo). If both fail, tell the user the specific error — NEVER say "check it yourself".

## Repo tracking

When the user asks to **track a repo** (e.g. "track this repo", "notify me about new issues"):

### Step 1 — Create a tracking file

Write a Markdown file to the **workspace**: `WORKSPACE/github-track-OWNER-REPO.md`

```markdown
# GitHub Tracking: OWNER/REPO
- **Track:** issues
- **Last issue:** 0
- **Last checked:** 2025-01-01T00:00:00Z
```

- **Track:** what to watch — `issues`, `pulls`, or `issues, pulls`
- **Last issue:** highest issue/PR number seen so far (start with current highest or 0)
- **Last checked:** ISO timestamp of last check

**Before creating:** Fetch current open issues to set `Last issue` to the highest number. This prevents reporting all existing issues on the first run.

### Step 2 — Create a schedule

```
schedule(
  action='create',
  when='0 */2 * * *',
  prompt='Check GitHub repo OWNER/REPO for new issues. Read tracking file WORKSPACE/github-track-OWNER-REPO.md. Fetch issues with number > last issue number from the file. For each new issue: send a short summary (title, author, labels). Then update last issue and last checked in the tracking file. If no new issues: do nothing, do not send a message.'
)
```

- Adjust `when` to what the user wants (every 2h, daily, etc.)
- The prompt must include the **full path** to the tracking file
- The prompt must say **"do nothing if no new issues"** — otherwise the bot sends empty updates

### Step 3 — Confirm to user

Tell the user:
1. What repo is tracked
2. What is tracked (issues, PRs, or both)
3. How often (schedule time)
4. Where the tracking file is stored

### Tracking file rules

- **Filename convention:** `github-track-OWNER-REPO.md` (in workspace)
- **Always update** `Last issue` and `Last checked` after each check
- **Never delete** the tracking file during workspace cleanup (it is referenced by a schedule)
- To **stop tracking:** remove the schedule AND delete the tracking file

## Rules

- **Never echo `$GH_TOKEN`** — never print, log, or include it in your response.
- For **public repos**: auth optional but increases rate limit (5000 req/h vs 60/h).
- For **private repos**: always pass `-H "Authorization: Bearer $GH_TOKEN"`.
- If the token is missing or empty, tell the user: `save_config {github_token: 'ghp_...'}` then restart miniassistant.
