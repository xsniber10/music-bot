import json

import asyncpg

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS verified_users (
    user_id BIGINT PRIMARY KEY
);
"""

_pool: asyncpg.Pool | None = None


async def init_pool(database_url: str) -> None:
    global _pool
    # statement_cache_size=0: Supabase's connection pooler (Supavisor, port 6543) runs in
    # transaction mode, which doesn't support asyncpg's default prepared-statement
    # caching. Without this, queries intermittently fail with errors like "prepared
    # statement ... does not exist" once the pooler recycles a connection mid-session.
    _pool = await asyncpg.create_pool(
        database_url, min_size=1, max_size=5, statement_cache_size=0
    )


async def close_pool() -> None:
    if _pool is not None:
        await _pool.close()


async def ensure_schema() -> None:
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def load_all_data() -> dict:
    async with _pool.acquire() as conn:
        library_rows = await conn.fetch("SELECT url, title, duration, thumbnail FROM library")
        playlist_rows = await conn.fetch("SELECT name, urls FROM playlists")
        favorite_rows = await conn.fetch("SELECT shortcut_name, url FROM favorites")

    library = {
        row["url"]: {
            "title": row["title"],
            "duration": row["duration"],
            "thumbnail": row["thumbnail"],
        }
        for row in library_rows
    }
    playlists = {row["name"]: json.loads(row["urls"]) for row in playlist_rows}
    favorites = {row["shortcut_name"]: row["url"] for row in favorite_rows}

    return {"library": library, "playlists": playlists, "favorites": favorites}


async def upsert_library_entry(url: str, title: str, duration: int | None, thumbnail: str | None) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO library (url, title, duration, thumbnail)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (url) DO UPDATE
            SET title = EXCLUDED.title, duration = EXCLUDED.duration, thumbnail = EXCLUDED.thumbnail
            """,
            url,
            title,
            duration,
            thumbnail,
        )


async def delete_library_entry(url: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM library WHERE url = $1", url)


async def upsert_playlist(name: str, urls: list[str]) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO playlists (name, urls)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (name) DO UPDATE SET urls = EXCLUDED.urls
            """,
            name,
            json.dumps(urls),
        )


async def delete_playlist(name: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM playlists WHERE name = $1", name)


async def upsert_favorite(shortcut_name: str, url: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO favorites (shortcut_name, url)
            VALUES ($1, $2)
            ON CONFLICT (shortcut_name) DO UPDATE SET url = EXCLUDED.url
            """,
            shortcut_name,
            url,
        )


async def delete_favorite(shortcut_name: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM favorites WHERE shortcut_name = $1", shortcut_name)


async def load_verified_users() -> set[int]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM verified_users")
    return {row["user_id"] for row in rows}


async def verify_user(user_id: int) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO verified_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            user_id,
        )


async def unverify_user(user_id: int) -> None:
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM verified_users WHERE user_id = $1", user_id)


async def wipe_music_data() -> None:
    """Clears every saved song (library, playlists, favorites). Does not touch
    verified_users — that's access control, not song data."""
    async with _pool.acquire() as conn:
        await conn.execute("TRUNCATE library, playlists, favorites")


async def get_song_counts() -> dict[str, int]:
    async with _pool.acquire() as conn:
        library = await conn.fetchval("SELECT COUNT(*) FROM library")
        playlists = await conn.fetchval("SELECT COUNT(*) FROM playlists")
        favorites = await conn.fetchval("SELECT COUNT(*) FROM favorites")
    return {"library": library, "playlists": playlists, "favorites": favorites}
