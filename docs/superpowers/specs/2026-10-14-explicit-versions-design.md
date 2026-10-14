# Explicit versions: design

Date: 2026-10-14. Fork of [anandprtp/Antra](https://github.com/anandprtp/Antra)
at upstream commit `07aeef1`, kept in a private repository with the original as
the `upstream` remote.

## Goal

When "Prefer explicit versions" is on, Antra downloads the explicit version of
every song that has one, still downloads songs that have no explicit version,
and shows the user which of the two they got.

Success means:

- Downloading an album from the artist page gets the explicit edition when
  Spotify has one, whichever edition the user clicked.
- A song picked or linked in its clean form is replaced by its explicit twin
  when Spotify has one.
- Every song in the download list carries one of four statuses, shown as a
  badge, and every finished file carries the matching advisory tag.
- No download fails or stalls because an explicit lookup failed.

## Why Antra gets clean versions today

Found by reading the upstream source:

1. **Album songs arrive with no explicit flag.** When the user is signed in to
   Spotify (`spotify_sp_dc` set), albums are fetched through
   `SpotifyClient._fetch_album_via_partner_api` (`antra/core/spotify.py`),
   which builds `TrackMetadata` without `is_explicit` or `isrc`. Every album
   song therefore has `is_explicit = None`.
2. **The resolver only holds out for explicit when the original is explicit.**
   `_result_looks_clean` (`antra/core/resolver.py`) refuses an immediate clean
   match only when `track.is_explicit is True`, which item 1 means never
   happens for albums.
3. **Explicit ranks below quality.** `_explicit_penalty` subtracts 0.20 from
   `similarity`, which is the last element of `_lossless_sort_key`. A hi-res
   clean result outranks a 16-bit explicit one.
4. **The artist page never separates editions.** The signed-in discography
   (`SpotifyPartnerClient.fetch_artist_discography_info`,
   `antra/core/spotify_partner.py`) returns every edition with no explicit
   field. The frontend's duplicate merge (`Ya` and `Ja` in `App.svelte`)
   prefers `is_explicit === true`, finds it on neither edition, and breaks
   the tie on album id. The unsigned route
   (`SpotifyClient._dedupe_discography_albums`) already checks each
   duplicate's songs and keeps the explicit edition; the signed-in route
   lacks it.
5. **The interface is told the original's flag, not the result's.** Engine
   events carry `asdict(track)`, so the UI could only ever show what was
   asked for.

## Decisions

- A song whose explicit version exists but cannot be found downloads as the
  clean edit and is marked as such. It is not skipped.
- The explicit version is found on Spotify (a "twin swap"), not by a loose
  search in the download sources. The swapped song carries the explicit
  version's ISRC, so the existing exact matching finds it in every source.
- The twin swap (section 2), the ranking change (section 3) and the file
  tags (section 6) run only when `prefer_explicit` is on. Reading the flags
  and keeping the explicit edition on the artist page (section 1) always run,
  as the unsigned discography route already does. Badges always show: with
  the setting off, `target_explicit` is true for a song Spotify marks
  explicit and null otherwise.

## Design

### 1. Read the flag everywhere

- `_fetch_album_via_partner_api` reads each song's `contentRating.label`
  (`"EXPLICIT"` is explicit; any other label is clean; a missing field is
  unknown, so a response without it does not send every song into a twin
  search) and its
  `externalIds.isrc` when present. `spotify_library.py` already parses
  `contentRating` this way for Liked Songs; the parsing moves into one shared
  helper used by both.
- `SpotifyPartnerClient.fetch_artist_discography_info` groups releases on the
  same key as `_discography_release_key` (name, type, year, track count). For
  groups of two or more it fetches each release with `getAlbum`, scores it as
  `_discography_release_explicit_score` does (any explicit song: 2; only
  clean songs: 0; no flags: 1), and keeps the best. Every kept release gains
  `is_explicit` (true, false or absent) for the interface. Releases with no
  duplicate cost no extra request.

### 2. Twin swap

A new module, `antra/core/explicit_twin.py`, runs after a track list is
fetched and before resolving. It holds the only network calls the feature
adds, and its matching rules are pure functions.

- **Album edition.** If a Spotify album has no explicit song, look in the
  artist's discography for a release with the same normalised name, type and
  track count that does. If one exists, fetch its songs and map them onto the
  original by disc and track number, confirming each pair by normalised
  title. Mapped songs are replaced; unmapped songs keep their original.
- **Single song.** For any remaining song marked clean, search Spotify
  (`assistedCurationSearch`, then hydrate with `getTrack` if the search
  result lacks the fields) for a twin that:
  - is marked explicit;
  - has the same primary artist (case- and accent-insensitive);
  - has the same title once markers such as "Clean", "Clean Version",
    "Edited", "Radio Edit" and "Censored" are removed;
  - is within 3 seconds of the original's duration.

  The first twin meeting all four replaces the song's `spotify_id`, `isrc`,
  `is_explicit` and `duration_ms`; album, track number and artwork stay as
  they were so the file lands where the user expects.
- **Cache.** Results, including "no twin", are cached for the session by
  Spotify id.
- **Failure.** A failed or rate-limited lookup leaves the song unchanged and
  records the lookup as failed. After 5 consecutive failures, twin lookups stop
  for the rest of the batch.

Each song leaves this step with a `target_explicit` value: true when the
original or its twin is explicit, false when Spotify confirmed it has no
explicit twin, and null when it could not tell (no flag, or the lookup
failed).

### 3. Ranking

With `prefer_explicit` on and `target_explicit` true, an explicit rank (result
explicit 2, unknown 1, clean 0) becomes the first element of the resolver's
sort keys (`_lossless_sort_key` and `_candidate_key`), ahead of quality.
`_result_looks_clean` uses `target_explicit` instead of `track.is_explicit`,
so a clean result is never accepted immediately when an explicit one is
wanted. With `target_explicit` false or null, ranking is unchanged from
upstream.

### 4. Status per song

One pure function decides the status from `target_explicit` and the chosen
result's `is_explicit` (a result whose title matches the clean markers counts
as clean):

| `target_explicit` | Result explicit | Result clean | Result unknown |
|---|---|---|---|
| true | `explicit` | `clean_edit` | `unknown` |
| false | `explicit` | `not_explicit` | `not_explicit` |
| null | `explicit` | `unknown` | `unknown` |

The status rides on `TRACK_RESOLVED` and `TRACK_COMPLETED` as a new
`EngineEvent.explicit_status` field, emitted by `emit_event` in
`antra/json_cli.py` beside `source` and `quality_label`. The interface only
displays it.

### 5. Interface

All in `antra-wails/frontend/src/App.svelte`:

| Status | Badge |
|---|---|
| `explicit` | **E**, white on dark |
| `clean_edit` | **Clean edit**, amber |
| `not_explicit` | none |
| `unknown` | **?**, grey, with a tooltip saying why |

- Download list: the badge sits beside the "Accepted via …" line of each
  song.
- Artist page: album covers whose kept edition has `is_explicit` true get an
  **E**.
- Track list shown before a download (`playlist_loaded` and
  `tracks_appended`): songs Spotify marks explicit get an **E**. Each track
  entry gains an `explicit` field.
- Batch end: a summary line ("41 explicit · 12 no explicit version · 2 clean
  edits") with the clean edits listed by title and artist.

### 6. File tags

`antra/utils/tagger.py` writes the iTunes advisory value: 1 for `explicit`,
2 for `clean_edit`, nothing otherwise.

- FLAC and other Vorbis comments: `ITUNESADVISORY`.
- MP3: `TXXX:ITUNESADVISORY`.
- M4A: the `rtng` atom.

## Testing

- Pytest, offline, against Spotify responses saved as fixtures:
  - album songs gain `is_explicit` from `contentRating`;
  - the discography keeps the explicit edition of a duplicate pair and adds
    no requests for single editions;
  - the twin rules accept and reject the right candidates (artist, marker
    stripping, the 3-second window);
  - album mapping by disc and track number, with an unmappable song kept;
  - the status table, all nine cells;
  - the new sort order puts a 16-bit explicit result above a 24-bit clean
    one only when `target_explicit` is true;
  - the advisory tag for each format.
- Manual, on the built app: one album known to have clean and explicit
  editions, downloaded from the artist page by clicking the clean edition;
  one song with no explicit version; checked by badge, summary and file tag.

## Fork setup and build

- A new private repository on the owner's GitHub account, cloned at
  `D:\Claude and Codex projects\Antra`, with `upstream` pointing at
  anandprtp/Antra. Work happens on the `explicit-versions` branch.
- New code stays in `explicit_twin.py` and its tests; existing files get small
  hooks, so merging upstream releases stays cheap.
- Building needs Go and the Wails CLI. PyInstaller may not yet support Python
  3.14, so the first build step checks that and installs Python 3.12 for a
  build virtual environment if needed. `build_desktop.py` produces the exe;
  the fork's exe is kept apart from the existing `Antra.exe`, and both share
  the settings folder.
- Antra is under the Elastic License 2.0, which permits modifying it for the
  owner's own use. The license and notices stay as they are.

## Out of scope

- Rova reading the advisory tag (a separate Rova change).
- Searching the download sources for explicit versions without a Spotify twin.
- An "explicit only" setting that skips clean edits.
- Apple Music, Amazon and Deezer links: their songs already carry explicit
  flags, so ranking and status work for them, but there is no twin swap.

## Risks

- `assistedCurationSearch` is a private Spotify operation; its response shape
  is confirmed during implementation and may change. The failure rule in
  section 2 contains the damage to a `?` badge.
- A twin search costs one or two requests per clean song. The session cache
  and the album swap, which covers a whole album with one lookup, keep
  this low for album downloads.
