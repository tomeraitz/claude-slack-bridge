FROM python:3.12-slim

# Install Node.js 20 LTS (required for Claude Code CLI) and the GitHub CLI
# (used by the /process-setup skill — see plans/full-process-plugin.md §11.2 #9).
# `gh` ships in the official cli/cli apt repo; we register the keyring + source
# inside this same RUN so the resulting image stays single-layer-clean.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

RUN useradd -m appuser && mkdir -p /home/appuser/.claude && chown appuser:appuser /home/appuser/.claude

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and project config
COPY src/ ./src/
COPY projects.json .
# In-container MCP config used by sub-Claudes spawned by the daemon.
# See plans/full-process-plugin.md §7 + §11.2 #7.
COPY mcp.in-container.json .

RUN chown -R appuser:appuser /app

USER appuser

WORKDIR /app/src

CMD ["python", "main.py"]
