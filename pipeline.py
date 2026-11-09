#!/usr/bin/env python3
"""Mirror Yarmouth, MA Select Board meetings as transcripts + AI summaries.

Steps, per new meeting:
  1. discover  - list shows on the Town's Cablecast VOD server
  2. audio     - pull the audio track with ffmpeg
  3. transcribe- faster-whisper -> raw/YYYY-MM-DD--slug.md
  4. summarize - Claude -> summary/YYYY-MM-DD--slug.md
  5. README    - refresh the "Recent Summaries" table

Usage:
  python pipeline.py --probe          # list what it can see; writes nothing
  python pipeline.py                  # process new meetings (max_per_run)
  python pipeline.py --show 5165      # process one specific show id
  python pipeline.py --resummarize    # rebuild summaries from existing raw/ files
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import requests

ROOT = Path(__file__).parent
RAW, SUMMARY = ROOT / "raw", ROOT / "summary"
STATE = ROOT / "processed.json"
CFG = tomllib.loads((ROOT / "config.toml").read_text())
BASE = CFG["source"]["base_url"].rstrip("/")
UA = {"User-Agent": "yarmouth-minutes-mirror (civic archive)"}


@dataclass
class Show:
    id: int
    title: str
    date: dt.date
    page_url: str
    media_url: str | None = None
    chapters: list[str] = field(default_factory=list)

    @property
    def stem(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        slug = re.sub(r"-?\d{1,2}-\d{1,2}-\d{2,4}$", "", slug) or "meeting"
        return f"{self.date.isoformat()}--{slug}"


# ---------------------------------------------------------------- discovery

def _get(url: str, **kw):
    r = requests.get(url, headers=UA, timeout=60, **kw)
    r.raise_for_status()
    return r


def _parse_date(show: dict, title: str) -> dt.date | None:
    for key in ("eventDate", "cgExempt", "date"):
        v = show.get(key)
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
            return dt.date.fromisoformat(v[:10])
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", title)  # "Select Board 6-9-2026"
    if m:
        mo, d, y = map(int, m.groups())
        return dt.date(y + 2000 if y < 100 else y, mo, d)
    return None


def discover() -> list[Show]:
    """List matching shows via the public Cablecast API (newest first)."""
    pats = [re.compile(p, re.I) for p in CFG["source"]["title_patterns"]]
    earliest = dt.date.fromisoformat(CFG["source"]["earliest_date"])
    shows: list[Show] = []
    for offset in range(0, 2000, 100):
        data = _get(f"{BASE}/CablecastAPI/v1/shows",
                    params={"include": "vod", "page_size": 100, "offset": offset,
                            "sort": "eventDate", "sort_order": "descending"}).json()
        vods = {v["id"]: v for v in data.get("vods", [])}
        batch = data.get("shows", [])
        if not batch:
            break
        for s in batch:
            title = s.get("cgTitle") or s.get("title") or ""
            if not any(p.search(title) for p in pats):
                continue
            date = _parse_date(s, title)
            if not date or date < earliest:
                continue
            media = None
            for vid in s.get("vods") or []:
                v = vods.get(vid, {})
                media = v.get("url") or v.get("fileName") or media
            shows.append(Show(s["id"], title.strip(), date,
                              f"{BASE}/internetchannel/show/{s['id']}", media))
        newest = max((_parse_date(s, s.get("title", "")) or dt.date.min) for s in batch)
        if newest < earliest:
            break
    shows.sort(key=lambda s: s.date, reverse=True)
    return shows


def enrich_from_page(show: Show) -> Show:
    """Pull agenda chapters (and a media URL, if the API didn't give one) from the show page."""
    html = _get(show.page_url).text
    text = re.sub(r"<[^>]+>", "\n", html)
    show.chapters = [ln.strip() for ln in text.splitlines()
                     if re.match(r"\s*\(?\d{1,2}:\d{2}(:\d{2})?\)?\s+\S", ln)
                     or re.search(r"\(\d{1,2}:\d{2}(:\d{2})?\)\s*$", ln.strip())][:80]
    if not show.media_url:
        m = re.search(r"(?:https?://|/)[^\"'\s<>]+\.(?:m3u8|mp4)[^\"'\s<>]*", html)
        show.media_url = m.group(0) if m else None
    if show.media_url and show.media_url.startswith("/"):
        show.media_url = BASE + show.media_url
    return show


# ---------------------------------------------------------- audio + transcript

def extract_audio(show: Show, workdir: Path) -> Path:
    out = workdir / f"{show.id}.mp3"
    src = show.media_url
    if src:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out)], check=True)
    else:  # last resort: let yt-dlp's generic extractor find the stream on the page
        subprocess.run(["yt-dlp", "-x", "--audio-format", "mp3",
                        "-o", str(workdir / f"{show.id}.%(ext)s"), show.page_url], check=True)
    return out


def transcribe(audio: Path) -> str:
    from faster_whisper import WhisperModel
    model = WhisperModel(CFG["transcribe"]["model"], device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(audio), language="en", vad_filter=True,
                                   initial_prompt="Town of Yarmouth, Massachusetts Select Board meeting. "
                                   "South Yarmouth, West Yarmouth, Yarmouth Port, Bass River, Cape Cod.")
    lines = []
    for seg in segments:
        h, rem = divmod(int(seg.start), 3600)
        m, s = divmod(rem, 60)
        lines.append(f"[{h}:{m:02d}:{s:02d}] {seg.text.strip()}")
    return "\n".join(lines)


def write_raw(show: Show, transcript: str) -> Path:
    path = RAW / f"{show.stem}.md"
    chapters = "\n".join(f"- {c}" for c in show.chapters) or "_None listed._"
    path.write_text(f"""---
title: "{show.title}"
date: {show.date.isoformat()}
source: {show.page_url}
channel: "{CFG['source']['channel_name']}"
type: select-board
---

# {show.title}
**Date:** {show.date.isoformat()}

## Agenda chapters
{chapters}

## Transcript

{transcript}
""")
    return path


# ------------------------------------------------------------------ summary

SYSTEM = """You summarize Town of Yarmouth, Massachusetts public meetings for residents.
Write plain, neutral, accurate prose. The transcript is machine-generated, so names may be
misspelled: only name people when the role is clear (e.g. "the Select Board chair"), and never
invent votes, figures, or outcomes that the transcript doesn't support. If a vote count is unclear,
say the item "was approved" without a tally."""

PROMPT = """Summarize this meeting in Markdown with exactly these sections:

## Summary
3-4 paragraphs: what the board decided and discussed, most consequential first.

## Key Points & Votes
Group bullets under ### topic headings. Bold each vote/decision and key dollar figures.

## Takeaways
3 bullets: what this signals for Yarmouth residents (taxes, development, wastewater/sewer,
beaches, short-term rentals, town services, etc. where relevant).

Return only those sections. Also, on the very first line, output:
SLUG: <3-6 word kebab-case topic slug, e.g. select-board-sewer-bond-vote>

Meeting: {title} ({date})
Agenda chapters:
{chapters}

Transcript:
{transcript}"""


def summarize(raw_path: Path) -> Path:
    import anthropic
    text = raw_path.read_text()
    meta = dict(re.findall(r'^(\w+): "?(.*?)"?$', text.split("---")[1], re.M))
    chapters = text.split("## Agenda chapters", 1)[-1].split("## Transcript", 1)[0].strip()
    transcript = text.split("## Transcript", 1)[-1].strip()

    msg = anthropic.Anthropic().messages.create(
        model=CFG["summarize"]["model"], max_tokens=4000, system=SYSTEM,
        messages=[{"role": "user", "content": PROMPT.format(
            title=meta["title"], date=meta["date"], chapters=chapters, transcript=transcript)}])
    body = msg.content[0].text.strip()
    slug_line, _, body = body.partition("\n")
    slug = re.sub(r"[^a-z0-9-]", "", slug_line.removeprefix("SLUG:").strip().lower())

    # Rename raw + summary to the topic slug (Louisville-style filenames).
    stem = f"{meta['date']}--{slug}" if slug else raw_path.stem
    if stem != raw_path.stem:
        raw_path = raw_path.rename(RAW / f"{stem}.md")
    out = SUMMARY / f"{stem}.md"
    out.write_text(f"""---
title: "{meta['title']}"
date: {meta['date']}
type: select-board-summary
source_url: {meta['source']}
channel: "{meta['channel']}"
---

{body.strip()}
""")
    return out


# ------------------------------------------------------------------- README

def update_readme(n: int = 10) -> None:
    rows = []
    for p in sorted(SUMMARY.glob("*.md"), reverse=True)[:n]:
        t = p.read_text()
        title = re.search(r'^title: "(.*)"', t, re.M).group(1)
        url = re.search(r"^source_url: (.*)", t, re.M).group(1)
        rows.append(f"| {p.name[:10]} | {title} | [summary](summary/{p.name}) | [video]({url}) |")
    table = "| Date | Title | Summary | Video |\n|---|---|---|---|\n" + "\n".join(rows)
    readme = (ROOT / "README.md").read_text()
    readme = re.sub(r"(<!-- recent-summaries:start -->).*(<!-- recent-summaries:end -->)",
                    lambda m: f"{m.group(1)}\n\n{table}\n\n{m.group(2)}", readme, flags=re.S)
    (ROOT / "README.md").write_text(readme)


# --------------------------------------------------------------------- main

def load_state() -> set[int]:
    return set(json.loads(STATE.read_text())) if STATE.exists() else set()


def process(show: Show, done: set[int]) -> None:
    print(f"→ {show.date} {show.title} (show {show.id})", flush=True)
    enrich_from_page(show)
    with tempfile.TemporaryDirectory() as tmp:
        audio = extract_audio(show, Path(tmp))
        raw = write_raw(show, transcribe(audio))
    print(f"  wrote {summarize(raw).relative_to(ROOT)}", flush=True)
    done.add(show.id)
    STATE.write_text(json.dumps(sorted(done), indent=0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="list matching shows; write nothing")
    ap.add_argument("--show", type=int, help="process one show id")
    ap.add_argument("--resummarize", action="store_true", help="redo summaries from raw/")
    args = ap.parse_args()
    RAW.mkdir(exist_ok=True)
    SUMMARY.mkdir(exist_ok=True)
    done = load_state()

    if args.resummarize:
        for raw in sorted(RAW.glob("*.md")):
            print(summarize(raw).relative_to(ROOT))
    elif args.show:
        page = Show(args.show, "", dt.date.today(), f"{BASE}/internetchannel/show/{args.show}")
        match = next((s for s in discover() if s.id == args.show), page)
        process(match, done)
    else:
        shows = discover()
        if args.probe:
            for s in shows[:25]:
                enrich_from_page(s)
                flag = "done" if s.id in done else "new "
                print(f"{flag} {s.date} #{s.id} {s.title}\n      media: {s.media_url}\n"
                      f"      chapters: {len(s.chapters)}")
            return
        todo = [s for s in shows if s.id not in done][: CFG["source"]["max_per_run"]]
        if not todo:
            print("No new meetings.")
        for s in todo:
            try:
                process(s, done)
            except Exception as e:  # keep going; one bad video shouldn't block the rest
                print(f"  FAILED show {s.id}: {e}", file=sys.stderr)
    update_readme()


if __name__ == "__main__":
    main()

