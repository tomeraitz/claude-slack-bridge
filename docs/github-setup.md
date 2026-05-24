# GitHub Setup — Getting Your Token

The `/process` workflow (and any skill that opens PRs or reads review comments) needs a GitHub token inside the container. Without one, `gh` and `git push` over HTTPS will fail.

If you don't plan to use the GitHub-driven parts of `/process`, you can skip this entirely — leave `GITHUB_TOKEN` unset and the container will start without configuring git auth.

---

## Step 1 — Create a Fine-Grained Personal Access Token

1. Go to https://github.com/settings/personal-access-tokens/new
2. **Token name:** something memorable (e.g. `claude-slack-bridge`)
3. **Expiration:** 90 days is a reasonable default — rotate when it expires
4. **Repository access:** select **Only select repositories** and pick the single repo this bridge will operate on (e.g. `claude-slack-two-way`)

   > Avoid "All repositories" — that gives the container write access to your entire account.

5. **Repository permissions:** grant only what's needed:

   | Permission | Access | Purpose |
   |---|---|---|
   | `Contents` | Read and write | `git push` to feature branches |
   | `Pull requests` | Read and write | `gh pr create`, read review comments |
   | `Metadata` | Read-only | Auto-granted, required |

   Skip `Issues`, `Actions`, `Workflows`, etc. unless you specifically need them.

6. Click **Generate token** and copy the value — it starts with `github_pat_...`. You won't be able to see it again.

---

## Step 2 — Add the Token to `.env`

In the repo root, open `.env` (gitignored — never commit it) and set:

```
GITHUB_TOKEN=github_pat_...
```

`.env.example` already has a placeholder line for this.

---

## Step 3 — Rebuild and Restart the Container

The token is wired into the container via `docker-compose.yml` and consumed by an entrypoint script that runs `gh auth setup-git` on startup. Apply the new env:

```
docker compose up -d --build claude-slack-bridge
```

---

## Step 4 — Verify

```
docker exec claude-slack-bridge gh auth status
docker exec claude-slack-bridge git config --global --get credential.https://github.com.helper
```

Expected output:

- `gh auth status` → "Logged in to github.com as <your-username>"
- The git config line → `!/usr/bin/gh auth git-credential`

If both look right, `gh` and `git push` inside the container will authenticate using your token.

---

## Rotating the Token

When the PAT expires (or if it's leaked):

1. Generate a new token at the URL above with the same permissions.
2. Replace the value in `.env`.
3. Restart the container — no rebuild needed:
   ```
   docker compose up -d claude-slack-bridge
   ```
4. Revoke the old token at https://github.com/settings/personal-access-tokens.

---

## Security Notes

- The token sits in the container's environment. Anyone with shell access to the container can read it. Keep the container's exposed surface (Slack channel allowlist, etc.) tight.
- Scope the token to **one repo only** so a compromise can't touch your other projects.
- `.env` is gitignored. Double-check before pushing — `git status` should never show `.env` as modified.
- The bridge writes daemon logs that may include subprocess output. Avoid logging commands that echo env vars (e.g. `env | grep TOKEN`).
