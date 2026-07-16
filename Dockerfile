FROM python:3.12-slim

# ffmpeg is required for voice playback; Render's native Python runtime doesn't
# include it, so a Docker-based service is the reliable way to get it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
