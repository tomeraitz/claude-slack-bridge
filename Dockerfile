FROM python:3.12-slim

# git, gh (GitHub CLI), and Node.js 20 — all from Debian trixie's default repos
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gh nodejs npm \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Trust bind-mounted repos regardless of host ownership (git ≥ 2.35.4 safe.directory check)
RUN git config --system --add safe.directory '*'

RUN useradd -m appuser && mkdir -p /home/appuser/.claude && chown appuser:appuser /home/appuser/.claude

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source and project config
COPY src/ ./src/
COPY projects.json .
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN chown -R appuser:appuser /app

USER appuser

WORKDIR /app/src

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]
