FROM python:3.12-slim

# ffmpeg is required for voice playback; nodejs gives yt-dlp a JS runtime to solve
# YouTube's n-signature challenge (without it, yt-dlp can only resolve throttled/
# missing formats). Render's native Python runtime includes neither, so a
# Docker-based service is the reliable way to get them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
