# Pull in the official PO Token provider image just to lift its already-built app out of
# it (see server/Dockerfile in Brainicism/bgutil-ytdlp-pot-provider) — this avoids
# reproducing its npm/tsc build ourselves.
FROM brainicism/bgutil-ytdlp-pot-provider:latest AS potprovider

FROM python:3.12-slim

# ffmpeg is required for voice playback. The pot-provider's own Node.js binary (copied
# below) doubles as yt-dlp's JS runtime for solving YouTube's n-signature challenge, so
# a separate nodejs apt package isn't needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Bring in the officially built PO Token provider app, plus the exact Node.js runtime it
# was built against (mixing a different Node major version risks native-module ABI
# mismatches, since node_modules here isn't rebuilt for this container).
COPY --from=potprovider /usr/local/bin/node /usr/local/bin/node
COPY --from=potprovider /app /opt/bgutil-pot-provider

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x start.sh

# Starts the PO Token provider (localhost:4416, not exposed outside the container) in
# the background, then runs the bot in the foreground.
CMD ["./start.sh"]
