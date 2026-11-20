#!/usr/bin/env python3
"""Collect TMDB data for 1970 movies, shaped for a "movie page" frontend.

For every movie it gathers: the movie itself, its director(s), its top-billed
actors and its composer(s), and writes JSON files a frontend can load directly.

Data comes from the TMDB MCP server (@cyanheads/tmdb-mcp-server), which this
script launches over stdio and talks to with the official MCP Python SDK:

    tmdb_discover_titles  -> list of 1970 movies
    tmdb_get_movie        -> movie details + cast + directors
    tmdb_get_person       -> profile for every actor / director / composer

Composers are the one exception. The MCP server's movie tool only returns
Director/Writer/Screenplay/Story/Producer/Creator crew (a hardcoded filter), so a
composer can never come back from it. By default the script fills that single
gap with TMDB's REST endpoint GET /movie/{id}/credits (same TMDB_API_KEY).
Pass --composer-source none to stay MCP-only (composers will then be empty).

Usage:
    export TMDB_API_KEY=...            # the same key the MCP server uses
    python fetch_1970.py               # top 100 movies of 1970 by vote count
    python fetch_1970.py --limit 500 --min-votes 20
    python fetch_1970.py --limit 0     # every 1970 movie (thousands of calls)

Output (default ./data):
    index.json          lightweight list for a browse/listing page
    movies/<id>.json    everything a movie page needs, people embedded
    people.json         {person_id: profile + roles}, for actor/director pages
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

YEAR = 1970
MAX_DISCOVER_PAGES = 500  # TMDB hard limit
# Composer job names on TMDB, best first. Only the best tier present is kept.
COMPOSER_JOB_TIERS = ("Original Music Composer", "Composer", "Music")
# Env vars forwarded to the MCP server subprocess (stdio_client does not pass
# the full environment by default).
FORWARDED_ENV = (
    "TMDB_API_KEY",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "NODE_USE_ENV_PROXY",
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class TmdbError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# MCP client
# ----------------------------------------------------------------------------
def find_npx(explicit: str | None) -> str:
    if explicit:
        return explicit
    # On WSL, PATH also lists Windows dirs (/mnt/c/...) whose npx shim runs under
    # CMD.EXE and never sees our env -- so look only at native PATH entries.
    native = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                             if not p.startswith("/mnt/"))
    npx = shutil.which("npx", path=native)
    if not npx:
        sys.exit("npx not found on PATH; pass --npx /path/to/npx "
                 "(needed to launch @cyanheads/tmdb-mcp-server)")
    return npx


def server_params(npx_path: str | None = None) -> StdioServerParameters:
    npx = find_npx(npx_path)
    env = get_default_environment()
    env["MCP_LOG_LEVEL"] = "warning"  # the server logs every request as JSON on stderr
    env.update({k: os.environ[k] for k in FORWARDED_ENV if k in os.environ})
    return StdioServerParameters(command=npx, args=["-y", "@cyanheads/tmdb-mcp-server"], env=env)


def parse_result(result: Any) -> dict:
    """Return the tool's JSON payload (structuredContent, else JSON text)."""
    text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    # Field names differ across mcp SDK versions (isError vs is_error, ...).
    is_error = getattr(result, "is_error", getattr(result, "isError", False))
    structured = getattr(result, "structured_content", getattr(result, "structuredContent", None))
    if is_error:
        raise TmdbError(text or "tool returned an error")
    if structured:
        return structured
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise TmdbError(f"unparseable tool output: {text[:200]!r}") from exc


class Tmdb:
    """Thin wrapper: bounded concurrency + retries around session.call_tool."""

    def __init__(self, session: ClientSession, concurrency: int, retries: int = 3):
        self.session = session
        self.sem = asyncio.Semaphore(concurrency)
        self.retries = retries

    async def call(self, tool: str, args: dict) -> dict:
        async with self.sem:
            for attempt in range(1, self.retries + 1):
                try:
                    return parse_result(await self.session.call_tool(tool, args))
                except TmdbError as exc:
                    # Missing ids won't fix themselves; everything else may.
                    if "No TMDB" in str(exc) or attempt == self.retries:
                        raise
                    await asyncio.sleep(1.5 * attempt)
        raise AssertionError("unreachable")


# ----------------------------------------------------------------------------
# Composer fallback (REST) -- only /movie/{id}/credits, only for composers
# ----------------------------------------------------------------------------
def rest_credits(movie_id: int, token: str, retries: int = 3) -> dict:
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/credits"
    headers = {"accept": "application/json"}
    if len(token) > 40:  # v4 read-access token (JWT)
        headers["Authorization"] = f"Bearer {token}"
    else:  # v3 api key
        url += "?" + urllib.parse.urlencode({"api_key": token})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or attempt == retries:
                raise TmdbError(f"REST credits {movie_id}: HTTP {exc.code}") from exc
            time.sleep(2 * attempt if exc.code == 429 else attempt)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise TmdbError(f"REST credits {movie_id}: {exc}") from exc
            time.sleep(attempt)
    raise AssertionError("unreachable")


def pick_composers(credits: dict) -> list[dict]:
    crew = credits.get("crew") or []
    for job in COMPOSER_JOB_TIERS:
        found = [c for c in crew if c.get("job") == job]
        if found:
            seen: dict[int, dict] = {}
            for c in found:
                seen.setdefault(c["id"], {"id": c["id"], "name": c.get("name", ""), "job": job})
            return list(seen.values())
    return []


# ----------------------------------------------------------------------------
# Pipeline steps
# ----------------------------------------------------------------------------
async def discover_movie_ids(tmdb: Tmdb, limit: int, min_votes: int) -> list[int]:
    ids: list[int] = []
    page, total_pages = 1, 1
    while page <= min(total_pages, MAX_DISCOVER_PAGES) and (limit == 0 or len(ids) < limit):
        data = await tmdb.call("tmdb_discover_titles", {
            "media_type": "movie",
            "year": YEAR,
            "sort_by": "vote_count.desc",
            "vote_count_gte": min_votes,
            "page": page,
        })
        total_pages = data.get("total_pages", 1)
        ids.extend(r["id"] for r in data.get("results", []))
        log(f"discover page {page}/{total_pages}: {len(ids)} ids")
        page += 1
    return ids[:limit] if limit else ids


def build_movie(raw: dict, cast_limit: int) -> dict:
    crew = raw.get("crew_key") or []
    directors, seen = [], set()
    for c in crew:
        if c.get("job") == "Director" and c["id"] not in seen:
            seen.add(c["id"])
            directors.append({"id": c["id"], "name": c["name"], "profile_url": c.get("profile_url")})
    cast = [
        {"id": c["id"], "name": c["name"], "character": c.get("character"),
         "order": c.get("order"), "profile_url": c.get("profile_url")}
        for c in (raw.get("cast") or [])[:cast_limit]
    ]
    movie = {k: raw.get(k) for k in (
        "id", "title", "original_title", "tagline", "overview", "status", "release_date",
        "runtime_minutes", "genres", "vote_average", "vote_count", "popularity", "budget",
        "revenue", "homepage", "imdb_id", "us_certification", "poster_url", "backdrop_url",
        "trailers", "keywords", "external_ids",
    )}
    movie.update(directors=directors, composers=[], cast=cast)
    return movie


def person_profile(raw: dict) -> dict:
    keep = ("id", "name", "biography", "birthday", "deathday", "place_of_birth",
            "known_for_department", "also_known_as", "gender", "popularity",
            "homepage", "imdb_id", "profile_url", "external_ids")
    return {k: raw[k] for k in keep if k in raw}  # drops the (large) filmography


async def main_async(args: argparse.Namespace) -> None:
    token = os.environ.get("TMDB_API_KEY")
    if not token:
        sys.exit("Set TMDB_API_KEY (the same key the TMDB MCP server uses).")

    out = Path(args.out)
    (out / "movies").mkdir(parents=True, exist_ok=True)

    async with stdio_client(server_params(args.npx)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tmdb = Tmdb(session, args.concurrency)

            # 1. movies of 1970 -------------------------------------------------
            ids = await discover_movie_ids(tmdb, args.limit, args.min_votes)
            log(f"{len(ids)} movies selected")

            # 2. movie details (+ cast, directors) via MCP ----------------------
            async def load_movie(mid: int) -> dict | None:
                try:
                    raw = await tmdb.call("tmdb_get_movie", {
                        "movie_id": mid,
                        "append": ["credits", "videos", "keywords", "external_ids", "release_dates"],
                    })
                    return build_movie(raw, args.cast_limit)
                except TmdbError as exc:
                    log(f"  ! movie {mid} skipped: {exc}")
                    return None

            movies = [m for m in await asyncio.gather(*(load_movie(i) for i in ids)) if m]
            log(f"movie details: {len(movies)}/{len(ids)}")

            # 3. composers (REST fallback; MCP movie tool can't return them) -----
            if args.composer_source == "rest":
                sem = asyncio.Semaphore(args.concurrency)

                async def load_composers(m: dict) -> None:
                    async with sem:
                        try:
                            credits = await asyncio.to_thread(rest_credits, m["id"], token)
                            m["composers"] = pick_composers(credits)
                        except TmdbError as exc:
                            log(f"  ! composers for {m['id']} skipped: {exc}")

                await asyncio.gather(*(load_composers(m) for m in movies))
                log(f"composers found for {sum(bool(m['composers']) for m in movies)}/{len(movies)} movies")

            # 4. person profiles via MCP (deduplicated) -------------------------
            roles: dict[int, list[dict]] = {}
            for m in movies:
                for kind, people in (("director", m["directors"]), ("composer", m["composers"]),
                                     ("actor", m["cast"])):
                    for p in people:
                        roles.setdefault(p["id"], []).append({
                            "movie_id": m["id"], "movie_title": m["title"], "role": kind,
                            **({"character": p["character"]} if kind == "actor" else {}),
                            **({"job": p["job"]} if kind == "composer" else {}),
                        })
            log(f"fetching {len(roles)} unique people")

            async def load_person(pid: int) -> tuple[int, dict | None]:
                try:
                    return pid, person_profile(await tmdb.call("tmdb_get_person", {"person_id": pid}))
                except TmdbError as exc:
                    log(f"  ! person {pid} skipped: {exc}")
                    return pid, None

            profiles = dict(await asyncio.gather(*(load_person(p) for p in roles)))

    # 5. write files --------------------------------------------------------------
    people_out = {
        str(pid): {**(profiles.get(pid) or {"id": pid}), "roles": r}
        for pid, r in roles.items()
    }
    (out / "people.json").write_text(json.dumps(people_out, ensure_ascii=False, indent=2))

    def with_profile(p: dict) -> dict:
        """Embed the light profile fields a movie page shows next to a person.

        Full biographies are left out (they live in people.json) to keep the
        per-movie payload small.
        """
        prof = profiles.get(p["id"]) or {}
        extra = {k: prof[k] for k in ("birthday", "deathday", "place_of_birth") if k in prof}
        return {**p, **extra, "profile_url": p.get("profile_url") or prof.get("profile_url")}

    index = []
    for m in movies:
        page = {**m, **{key: [with_profile(p) for p in m[key]] for key in ("directors", "composers", "cast")}}
        (out / "movies" / f"{m['id']}.json").write_text(json.dumps(page, ensure_ascii=False, indent=2))
        index.append({
            "id": m["id"], "title": m["title"], "release_date": m["release_date"],
            "genres": [g["name"] for g in m["genres"]], "vote_average": m["vote_average"],
            "poster_url": m["poster_url"],
            "directors": [d["name"] for d in m["directors"]],
            "composers": [c["name"] for c in m["composers"]],
        })
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2))
    log(f"wrote {len(index)} movies + {len(people_out)} people to {out}/")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data", help="output directory (default: data)")
    ap.add_argument("--limit", type=int, default=100,
                    help="max movies, most-voted first; 0 = all of 1970 (default: 100)")
    ap.add_argument("--min-votes", type=int, default=0, help="only movies with >= this many votes")
    ap.add_argument("--cast-limit", type=int, default=10, help="actors per movie, max 15 (default: 10)")
    ap.add_argument("--concurrency", type=int, default=8, help="parallel requests (default: 8)")
    ap.add_argument("--composer-source", choices=("rest", "none"), default="rest",
                    help="rest: fill composers via TMDB REST credits (MCP can't); none: MCP-only")
    ap.add_argument("--npx", help="path to a native npx binary (default: auto-detect on PATH)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
