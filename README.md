# bahar-bootcamp

## 1970 movie data (TMDB)

`fetch_1970.py` collects 1970 movies plus their directors, actors and composers
and writes JSON a frontend can use for a movie page.

It launches the TMDB MCP server (`@cyanheads/tmdb-mcp-server`) over stdio and
calls its tools with the MCP Python SDK: `tmdb_discover_titles`,
`tmdb_get_movie`, `tmdb_get_person`.

```bash
export TMDB_API_KEY=...        # same key the MCP server uses
pip install -r requirements.txt
python fetch_1970.py                         # top 100 movies of 1970 by votes
python fetch_1970.py --limit 500 --min-votes 20
python fetch_1970.py --limit 0               # all ~5.5k movies (slow)
```

Needs Node (`npx`) on PATH. On WSL it ignores Windows `/mnt/...` PATH entries;
use `--npx /path/to/npx` to point at a specific binary.

### Output (`data/`)

| File | Use |
| --- | --- |
| `index.json` | Lightweight list for a browse page (title, poster, genres, director/composer names). |
| `movies/<id>.json` | Everything for one movie page: details, trailers, keywords, `directors`, `composers`, `cast` (people embedded with photo, birthday, birthplace). |
| `people.json` | `{person_id: full profile + roles}` for actor/director/composer pages, with the movies each person worked on. |

### Composers

The MCP server's `tmdb_get_movie` only returns Director/Writer/Screenplay/Story/
Producer/Creator crew (hardcoded), so it can never return a composer. By default
the script fills that one gap with TMDB's REST `GET /movie/{id}/credits`
(same `TMDB_API_KEY`); composer profiles still come through MCP. Use
`--composer-source none` for MCP-only (composers will be empty). Some films
genuinely have no credited composer on TMDB.
