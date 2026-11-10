# Yarmouth, MA Select Board — Transcripts & Summaries

Searchable, plain-English summaries of Town of Yarmouth, Massachusetts public meetings,
generated from the Town's own meeting video on [Government Channel 8](https://vod.yarmouth.ma.us).
Modeled on [plc/louisville-co-city-council-minutes](https://github.com/plc/louisville-co-city-council-minutes).

> Transcripts are machine-generated (Whisper) and summaries are AI-written (Claude).
> Names and figures can be wrong. The official record is the Town's approved minutes and the video itself.

## Structure

- `raw/` — full timestamped transcript, named `YYYY-MM-DD--topic.md`, with the agenda chapters from the video page
- `summary/` — paired summary with the same filename (Summary · Key Points & Votes · Takeaways); `source_url` links to the video
- `pipeline.py` — discovery → audio → transcript → summary → README table
- `config.toml` — which boards to include, backfill start date, models

## Recent Summaries

<!-- recent-summaries:start -->

| Date | Title | Summary | Video |
|---|---|---|---|
| 2026-11-03 | November Town Meeting Warrant Articles | [summary](summary/2026-11-03--fall-town-meeting-warrant-preview.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5303) |
| 2026-10-27 | Select Board 10-27-2026 | [summary](summary/2026-10-27--select-board-str-articles-wastewater-discharge.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5300) |
| 2026-10-06 | Select Board 10-6-2026 | [summary](summary/2026-10-06--select-board-fire-standards-of-cover-warrant.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5279) |
| 2026-09-29 | Select Board 9-29-2026 | [summary](summary/2026-09-29--select-board-summer-street-unpaved-oyster-reef.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5271) |
| 2026-09-15 | Select Board 9-15-2026 | [summary](summary/2026-09-15--select-board-dog-hearing-tax-rate-wastewater.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5257) |
| 2026-08-25 | Select Board 8-25-2026 | [summary](summary/2026-08-25--select-board-forest-road-counsel-goals.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5242) |
| 2026-08-11 | Select Board 8-11-2026 | [summary](summary/2026-08-11--select-board-mma-letter-wall-that-heals.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5226) |
| 2026-07-21 | Select Board 7-21-2026 | [summary](summary/2026-07-21--select-board-irish-village-license-mvp-grant.md) | [video](https://vod.yarmouth.ma.us/internetchannel/show/5198) |

<!-- recent-summaries:end -->

## Setup

1. Create a GitHub repo and push these files.
2. Add a repo secret `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions).
3. Actions → **Update Yarmouth meeting summaries** → Run workflow with mode `probe`.
   This lists the Select Board videos it found, their media URLs and chapter counts, and writes nothing.
4. If the probe looks right, run it with mode `run` (or a single show id such as `5165`).
   After that it runs on its own every Thursday and Saturday morning.

### Running locally

```bash
brew install ffmpeg            # or apt-get install ffmpeg
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python pipeline.py --probe
python pipeline.py --show 5165
```

### Adding other boards

Add title patterns to `config.toml`, e.g. `"planning board"`, `"board of health"`,
`"finance committee"`, `"zoning board"`, `"old king's highway"`.

### Cost & time

Each 3-hour meeting takes roughly 1–2 hours to transcribe on a free GitHub runner with the
`small` Whisper model, so `max_per_run` defaults to 3. Summaries cost well under a dollar per meeting.
