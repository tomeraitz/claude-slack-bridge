FROM python:3.12-slim

# Install Node.js 20 LTS (required for Claude Code CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

RUN useradd -m appuser && mkdir -p /home/appuser/.claude && chown appuser:appuser /home/appuser/.claude

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

RUN chown -R appuser:appuser /app

USER appuser

WORKDIR /app/src

CMD ["python", "main.py"]
