FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AGENTCHAT_DB=/app/data/agentchat.db \
    AGENTCHAT_HTTP_HOST=0.0.0.0 AGENTCHAT_HTTP_PORT=8000
# ffmpeg: phone speech → 8 kHz WAV · iputils-ping: read-only ping checks
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg iputils-ping && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -r -u 10001 agentchat && mkdir -p /app/data && chown agentchat /app/data
USER agentchat
EXPOSE 8000 1162/udp 5514/udp
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import os,urllib.request as u;h=os.environ.get('AGENTCHAT_HTTP_HOST','');h='127.0.0.1' if h in ('','0.0.0.0') else h;u.urlopen(f'http://{h}:{os.environ.get(\"AGENTCHAT_HTTP_PORT\",\"8000\")}/api/health')"
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$AGENTCHAT_HTTP_HOST\" --port \"$AGENTCHAT_HTTP_PORT\" --proxy-headers"]
