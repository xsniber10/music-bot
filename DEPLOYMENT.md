# Deploying MusicBot: GitHub + Supabase + Render

## What changed in the code

- `bot.py` no longer reads/writes `playlists.json`. All persistence (library, saved
  playlists, favorites) now goes through `database.py`, which uses `asyncpg` against a
  Postgres connection string (`DATABASE_URL`).
- The in-memory `playlist_data` dict still exists and still powers every dropdown/embed
  exactly as before — it's just loaded from Postgres at startup instead of a JSON file,
  and every write now also does an async upsert/delete against the database.
- The entry point changed from `bot.run(TOKEN)` to an `asyncio.run(main())` pattern, so
  the Postgres connection pool can be created on the *same* event loop the bot uses
  (required for `asyncpg` + `discord.py` to coexist correctly).
- `SPOTIPY_CLIENT_ID`/`SPOTIPY_CLIENT_SECRET` are **not** used anywhere in the current
  code — the Spotify integration was rewritten a while back to scrape Spotify's public
  embed pages instead of using the official API, so no Spotify credentials are needed at
  all. Don't bother setting those two variables; only `SPOTIFY_PLAYLIST_URL` matters, and
  it already has a default hardcoded in `bot.py` if you don't set the env var.

## 1. Set up Supabase

1. Create a project at [supabase.com](https://supabase.com) (free tier is enough).
2. Open **SQL Editor** in the Supabase dashboard and run this once to create the tables:

   ```sql
   CREATE TABLE IF NOT EXISTS library (
       url TEXT PRIMARY KEY,
       title TEXT NOT NULL,
       duration INTEGER,
       thumbnail TEXT
   );

   CREATE TABLE IF NOT EXISTS playlists (
       name TEXT PRIMARY KEY,
       urls JSONB NOT NULL DEFAULT '[]'::jsonb
   );

   CREATE TABLE IF NOT EXISTS favorites (
       shortcut_name TEXT PRIMARY KEY,
       url TEXT NOT NULL
   );
   ```

   (The bot also runs this same statement automatically on every startup via
   `CREATE TABLE IF NOT EXISTS`, so this manual step is really just to confirm the
   credentials work — it's not required, but doing it once is a good sanity check.)

3. Go to **Project Settings → Database → Connection string → URI**. Use the **Session
   pooler** connection (port 6543) rather than the direct connection — Render's outbound
   IPs work better through the pooler, and it's the one Supabase recommends for
   long-running backend services. Copy it; it looks like:

   ```
   postgresql://postgres.xxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-xxxxx.pooler.supabase.com:6543/postgres
   ```

4. Paste it into `.env` as `DATABASE_URL=...` (replace `[YOUR-PASSWORD]` with your actual
   database password, set when you created the project).

## 2. Migrate your existing local data (optional but recommended)

You already have songs saved in `playlists.json` from local testing. To carry them over
instead of starting the Supabase library empty:

```
python migrate_to_supabase.py
```

This reads `playlists.json` and upserts everything into the tables above. Safe to run
more than once.

## 3. Test locally against Supabase

With `DATABASE_URL` set in `.env`, just run the bot the same way as before:

```
python bot.py
```

It should log in, connect to Postgres, and load your library from Supabase instead of
the JSON file. Once this works locally, you're ready to deploy.

## 4. Push to GitHub

```
git init
git add .
git commit -m "Prepare for Supabase + Render deployment"
git branch -M main
git remote add origin <your-empty-github-repo-url>
git push -u origin main
```

Double-check `.env` is **not** included in the commit (`git status` should not show it —
it's already in `.gitignore`). `playlists.json` is also gitignored now since Postgres is
the source of truth going forward.

## 5. Deploy on Render

1. In the Render dashboard: **New → Background Worker** (not "Web Service" — this bot
   doesn't listen on an HTTP port, so a Web Service will fail Render's port-binding
   health check).
2. Connect your GitHub repo.
3. **Environment**: choose **Docker**. This repo includes a `Dockerfile` that installs
   `ffmpeg` via `apt-get` — Render's native (non-Docker) Python runtime does **not**
   include `ffmpeg`, and voice playback will fail without it. Docker is the reliable way
   to get it on Render for a Background Worker.
   - Render will build and run the `Dockerfile` automatically; you don't need to set a
     separate Build/Start command when using the Docker environment (`CMD ["python",
     "bot.py"]` at the end of the Dockerfile is what runs).
   - If you'd rather not use Docker: Render's native Python environment has no apt
     access, so there's no reliable way to install `ffmpeg` there for a Background
     Worker. Docker is the recommended path for this project.
4. Set environment variables in the Render dashboard (**Environment** tab):
   - `DISCORD_TOKEN`
   - `DATABASE_URL` (the same Supabase pooler URL from step 1)
   - `SPOTIFY_PLAYLIST_URL` (optional — only if you want to override the default already
     in `bot.py`)
5. Deploy. Check the **Logs** tab for `Bot is online and ready.` — same message you've
   seen locally throughout development.

### About the `Procfile`

Render's Docker environment doesn't read `Procfile` (that's a Heroku convention); the
`Dockerfile`'s `CMD` is what actually runs. The `Procfile` is included anyway in case you
ever deploy this to a Heroku-style platform instead, where `worker: python bot.py`
tells it to run as a background worker dyno rather than expecting an HTTP-serving `web`
process.

## New/changed files in this repo

- `database.py` — all Postgres/`asyncpg` logic (schema, load, upsert, delete).
- `migrate_to_supabase.py` — one-time script to upload your existing local library.
- `Dockerfile` — Python 3.12 + `ffmpeg`, used by Render's Docker environment.
- `.dockerignore` — keeps `.env` and `.git` out of the built image.
- `Procfile` — Heroku-style worker declaration (see note above).
- `requirements.txt` — added `asyncpg`.
- `.env` / `.gitignore` — added `DATABASE_URL`, `SPOTIFY_PLAYLIST_URL`; `playlists.json`
  is now gitignored.
