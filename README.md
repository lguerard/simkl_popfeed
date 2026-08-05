# simkl_popfeed

Sync a [Simkl](https://simkl.com) profile's watch history to
[Popfeed](https://popfeed.social) via the AT Protocol.

This is a sibling of [jellyfin_popfeed](https://github.com/lguerard/jellyfin_popfeed),
which syncs watch state from a Jellyfin server, and
[trakt_popfeed](https://github.com/lguerard/trakt_popfeed) (Trakt-based;
kept around but superseded for this use case once creating a Trakt API
application started requiring Trakt VIP). `simkl_popfeed` fills in
everything *not* watched through Jellyfin without ever duplicating or
overwriting what jellyfin_popfeed already tracked — see
[How dedup works](#how-dedup-works).

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

## One-time setup: get a Simkl access token

Simkl uses a PIN/device-code flow — no OAuth redirect server needed:

```bash
simkl-popfeed --setup
```

This prints a URL and a code; approve it in a browser, and the command
prints a long-lived (~5 year) access token to save. Copy it into `.env` as
`SIMKL_ACCESS_TOKEN`, or into a GitHub Actions secret for the daily cron.

## Configuration

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable                                 | Required | Description                                                          |
| ----------------------------------------- | -------- | ---------------------------------------------------------------------|
| `SIMKL_CLIENT_ID`                         | Yes      | Simkl API application client ID                                      |
| `SIMKL_ACCESS_TOKEN`                      | Yes      | Access token from `simkl-popfeed --setup`                            |
| `POPFEED_IDENTIFIER`                      | Yes      | Your Popfeed handle (e.g. `you.bsky.social`)                         |
| `POPFEED_PASSWORD`                        | Yes      | App password                                                         |
| `POPFEED_PDS_URL`                         | No       | PDS URL (default: `https://eurosky.social`)                          |
| `SIMKL_POPFEED_WATCHED_MOVIES_LIST_NAME`  | No       | Override the "Watched Movies" list name                              |
| `SIMKL_POPFEED_WATCHED_SHOWS_LIST_NAME`   | No       | Override the "Watched Shows" list name                               |
| `SIMKL_POPFEED_RECENT_LIST_NAME`          | No       | Override the "Recent" list name                                      |
| `DRY_RUN`                                 | No       | Set to `true` to log without writing                                 |

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

1. Fetches the full watched-movies and watched-shows history from Simkl
   (`/sync/all-items`), plus ratings.
2. Authenticates with the Popfeed PDS via AT Protocol.
3. Finds or creates the "Watched Movies" / "Watched Shows" / "Recent"
   lists — **by name**, so it reuses the exact lists jellyfin_popfeed
   already created instead of making duplicates.
4. For each watched movie/episode, writes a `social.popfeed.feed.listItem`
   record (plus a Recent-list entry, and a rating-only
   `social.popfeed.feed.review` record if Simkl has a rating for it —
   Simkl's comments/review-text API isn't available yet), unless it's
   already tracked.

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
    --shows-file path/to/shows-export.json
```

Run `simkl-popfeed --setup` first so `SIMKL_ACCESS_TOKEN` is set. Note
SeriesGuide never stores per-episode watch *dates* (only a watched flag),
so migrated items land in Simkl without original watch dates — that's a
SeriesGuide limitation, not something this script can recover.

## Limitations

- **No per-show/season progress records.** Only per-episode watched
  markers are written, not jellyfin_popfeed's aggregate show/season
  progress records.
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
