# simkl_popfeed

Bidirectional sync between [Simkl](https://simkl.com) and
[Popfeed](https://popfeed.social): watching something anywhere Simkl
knows about (Cinopsys, etc.) makes it show up on Popfeed, and anything
already on Popfeed (including things watched in Jellyfin, via
jellyfin_popfeed — see below) makes it show up in Simkl too — without
ever duplicating anything jellyfin_popfeed already wrote directly. See
[How it works](#how-it-works) and [How dedup works](#how-dedup-works).

This is a sibling of [jellyfin_popfeed](https://github.com/lguerard/jellyfin_popfeed)
(the Jellyfin plugin that writes straight to Popfeed — unchanged, not
touched by this project; it runs on the Jellyfin server itself, so
Jellyfin's watch history reaches Simkl through Popfeed rather than this
sync talking to Jellyfin directly, which matters if your Jellyfin server
isn't reachable from wherever this sync runs) and
[trakt_popfeed](https://github.com/lguerard/trakt_popfeed) (Trakt-based;
kept around but superseded for this use case once creating a Trakt API
application started requiring Trakt VIP).

## Requirements

- Python 3.11+
- A free [Simkl API application](https://simkl.com/settings/developer/)
  (client ID — Simkl's API is free for personal/non-commercial use, no
  VIP tier required)
- The same Popfeed / Bluesky account and app password jellyfin_popfeed uses

## Installation

```bash
pip install -e .
```

## Configuration

**Do this before anything else** — copy `.env.example` to `.env` in
whatever directory you'll run `simkl-popfeed` from, and set at least
`SIMKL_CLIENT_ID` (from the Simkl app you registered). `--setup` below
reads `SIMKL_CLIENT_ID` from this file; without it, `--setup` fails
immediately with "SIMKL_CLIENT_ID is not set".

```bash
cp .env.example .env
```

| Variable                                 | Required | Description                                                          |
| ----------------------------------------- | -------- | ---------------------------------------------------------------------|
| `SIMKL_CLIENT_ID`                         | Yes      | Simkl API application client ID — set this first                     |
| `SIMKL_ACCESS_TOKEN`                      | Yes      | Access token from `simkl-popfeed --setup` (see below) — leave the placeholder until you've run it |
| `POPFEED_IDENTIFIER`                      | Yes      | Your Popfeed handle (e.g. `you.bsky.social`)                         |
| `POPFEED_PASSWORD`                        | Yes      | App password                                                         |
| `POPFEED_PDS_URL`                         | No       | PDS URL (default: `https://eurosky.social`)                          |
| `SIMKL_POPFEED_WATCHED_MOVIES_LIST_NAME`  | No       | Override the "Watched Movies" list name                              |
| `SIMKL_POPFEED_WATCHED_SHOWS_LIST_NAME`   | No       | Override the "Watched Shows" list name                               |
| `SIMKL_POPFEED_RECENT_LIST_NAME`          | No       | Override the "Recent" list name                                      |
| `DRY_RUN`                                 | No       | Set to `true` to log without writing                                 |
| `TMDB_API_KEY`                            | No       | Free [TMDb API key](https://www.themoviedb.org/settings/api) — enables season/series completion marking (see below) |

Only `SIMKL_CLIENT_ID` needs to be real before the next step —
`POPFEED_IDENTIFIER`/`POPFEED_PASSWORD` can stay as placeholders until
you're ready to actually run the sync.

## One-time setup: get a Simkl access token

Simkl uses a PIN/device-code flow — no OAuth redirect server needed:

```bash
simkl-popfeed --setup
```

This prints a URL and a code; approve it in a browser, and the command
prints a long-lived (~5 year) access token. Paste it into `.env` as
`SIMKL_ACCESS_TOKEN` (replacing the placeholder), and also save it as a
GitHub Actions secret for the daily cron.

## Usage

```bash
# Run the sync
simkl-popfeed

# Dry run — logs actions without writing anything
simkl-popfeed --dry-run

# Use a specific .env file
simkl-popfeed --env-file /path/to/.env
```

## How It Works

1. Authenticates with the Popfeed PDS via AT Protocol, then reads every
   item already on Popfeed's "Watched Movies"/"Watched Shows" lists (this
   includes anything jellyfin_popfeed wrote there directly, and anything
   a previous run of this sync wrote) and pushes all of it to Simkl via
   `POST /sync/history`. This call is idempotent, so it's safe to repeat
   every run for items Simkl already has. Best-effort: a failure here is
   logged and the rest of the sync still runs — this is how Jellyfin
   activity ends up in Simkl without this sync ever talking to Jellyfin
   directly (jellyfin_popfeed already put it on Popfeed; this step
   forwards it from there).
2. Fetches the full watched-movies and watched-shows history from Simkl
   (`/sync/all-items`), plus ratings — this now includes whatever step 1
   just pushed, though that doesn't matter for what happens next since
   those items are already on Popfeed (the rkey dedup below skips them).
3. Finds or creates the "Watched Movies" / "Watched Shows" / "Recent"
   lists — **by name**, so it reuses the exact lists jellyfin_popfeed
   already created instead of making duplicates.
4. For each watched movie/episode, writes a `social.popfeed.feed.listItem`
   record with `status: "#finished"` (plus a Recent-list entry, and a
   rating-only `social.popfeed.feed.review` record if Simkl has a rating
   for it — Simkl's comments/review-text API isn't available yet), unless
   it's already tracked.
5. **If `TMDB_API_KEY` is set**: for every show with at least one watched
   episode, looks up each season's total episode count from TMDb (one
   call per show — Simkl/Popfeed only ever expose what's been watched,
   never the total) and marks a season complete
   (`social.popfeed.feed.listItem`, `creativeWorkType: "tv_season"`) once
   every one of its episodes is watched, and the series complete
   (`creativeWorkType: "tv_show"`) once every season is — matching
   jellyfin_popfeed's own completion logic exactly, down to the record
   shapes and rkeys (`w.ts.{seriesId}.{season}` / `w.tv.{seriesId}`), so a
   show tracked partly via Jellyfin and partly via Simkl still completes
   correctly either way. Skipped entirely if `TMDB_API_KEY` isn't set.

## How dedup works

jellyfin_popfeed computes a deterministic Popfeed record key (`rkey`) from
each item's TMDb ID — e.g. `w.mv.603` for a movie, `w.ep.1399.1.1` for a
series/season/episode. `simkl_popfeed` computes the exact same rkey scheme
from Simkl's TMDb IDs. Before writing anything, it checks whether that
rkey already exists:

- **Exists** (written by jellyfin_popfeed, or a previous simkl_popfeed
  run) → skipped, permanently. Once Jellyfin has tracked something, this
  sync never touches it again.
- **Missing** → written, becoming a simkl_popfeed-owned record.

This is deliberately write-once, not diff-and-update: if a Simkl rating
changes after the fact, the existing review record won't be revised.

## Migrating from SeriesGuide

If you're moving from SeriesGuide (which only syncs with Trakt, not
Simkl) and want your existing watched history in Simkl without paying for
anything: `scripts/migrate_seriesguide.py` reads SeriesGuide's local JSON
export (More → Export and Import in the app) and writes watched status
directly to Simkl via its free API — no Trakt account and no Simkl
Pro/VIP needed (Simkl's website "Import from Trakt" tool is Pro/VIP-gated,
but the underlying API write endpoint isn't).

```bash
python scripts/migrate_seriesguide.py \
    --movies-file path/to/movies-export.json \
    --shows-file path/to/shows-export.json \
    --lists-file path/to/lists-export.json
```

Run `simkl-popfeed --setup` first so `SIMKL_ACCESS_TOKEN` is set. Note
SeriesGuide never stores per-episode watch *dates* (only a watched flag),
so migrated items land in Simkl without original watch dates — that's a
SeriesGuide limitation, not something this script can recover.

`--lists-file` is optional and handles SeriesGuide's custom Lists export
best-effort: **Simkl's API has no custom-list endpoint at all**, so list
items are placed on Simkl's "plan to watch" status instead via
`/sync/add-to-list` — the list name/grouping itself is lost. If you want
to preserve actual named lists, recreate them manually on Simkl's website
(which does support custom lists, just not through the API) instead of
using this flag.

## Limitations

- **Season/series completion needs `TMDB_API_KEY`.** Without it, only
  per-episode watched markers are written — the aggregate show/season
  progress records are skipped entirely.
- **No review text.** Simkl's reviews/comments API is documented but not
  yet available — review records carry a rating only.
- **Episode-level TMDb ID coverage isn't guaranteed.** Simkl resolves
  most episodes to a TMDb ID via the parent show, but community reports
  note occasional gaps; anything unresolvable is skipped and logged
  rather than guessed.
- **Custom Jellyfin list names.** If your jellyfin_popfeed setup uses
  non-default `WatchedListName`/`RecentListName` values, set the
  `SIMKL_POPFEED_*_LIST_NAME` overrides above so this sync reuses the same
  lists instead of creating new ones.

## GitHub Actions

`.github/workflows/sync.yml` runs the sync daily via cron
(`workflow_dispatch` also available for manual runs). Configure these as
repository secrets/variables:

- `secrets.SIMKL_CLIENT_ID`
- `secrets.SIMKL_ACCESS_TOKEN`
- `secrets.POPFEED_IDENTIFIER`
- `secrets.POPFEED_PASSWORD`
- `vars.POPFEED_PDS_URL` (optional)
- `secrets.TMDB_API_KEY` (optional — enables season/series completion)

No Jellyfin-reachability considerations here — this sync only ever talks
to Popfeed, Simkl, and TMDb, all reachable from GitHub's hosted runners.
