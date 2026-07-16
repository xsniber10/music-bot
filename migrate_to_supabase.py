"""One-off script: uploads the existing local playlists.json into Supabase.

Run this once after DATABASE_URL is set in .env, before deploying. It is safe
to re-run (uses upserts), but there's normally no reason to run it twice.

    python migrate_to_supabase.py
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

import database as db

load_dotenv()


async def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not found. Set it in .env first.")

    data_file = Path(__file__).resolve().parent / "playlists.json"
    if not data_file.exists():
        print("No playlists.json found locally — nothing to migrate.")
        return

    with open(data_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    await db.init_pool(database_url)
    await db.ensure_schema()

    library = data.get("library", {})
    for url, info in library.items():
        await db.upsert_library_entry(url, info.get("title", url), info.get("duration"), info.get("thumbnail"))
    print(f"Migrated {len(library)} library song(s).")

    playlists = data.get("playlists", {})
    for name, urls in playlists.items():
        await db.upsert_playlist(name, urls)
    print(f"Migrated {len(playlists)} saved playlist(s).")

    favorites = data.get("favorites", {})
    for shortcut_name, url in favorites.items():
        await db.upsert_favorite(shortcut_name, url)
    print(f"Migrated {len(favorites)} favorite(s).")

    await db.close_pool()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
