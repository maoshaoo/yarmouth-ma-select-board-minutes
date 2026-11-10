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

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def _media_candidates(url: str) -> list[str]:
    """The Cablecast player streams HLS; the bare .mp4 download is often blocked."""
    base = url.rsplit("/", 1)[0]
    cands = [url]
    if url.endswith(".mp4"):
        cands += [f"{url}/playlist.m3u8", f"{url}/index.m3u8",
                  f"{base}/vod.m3u8", f"{base}/playlist.m3u8", f"{base}/index.m3u8"]
    return list(dict.fromkeys(cands))


def _probe_status(url: str, page_url: str) -> int | str:
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA, "Referer": page_url,
                                       "Range": "bytes=0-0"}, timeout=30, stream=True)
        r.close()
        return r.status_code
    except Exception as e:
        return type(e).__name__


SAMPLE_RATE = 16000  # Whisper's input rate; audio is saved as raw 32-bit float samples


def _duration(src: str, headers: str | None = None) -> float | None:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0"]
    if headers:
        cmd += ["-headers", headers]
    try:
        return float(subprocess.run(cmd + [src], capture_output=True, text=True,
                                    timeout=120).stdout.strip())
    except Exception:
        return None


def _ffmpeg_audio(src: str, out: Path, headers: str | None = None) -> bool:
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if headers:
        # The Town's server drops long downloads; reconnect and resume where it left off.
        cmd += ["-headers", headers, "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "5xx",
                "-reconnect_delay_max", "30"]
    cmd += ["-i", src, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", str(out)]
    return subprocess.run(cmd).returncode == 0 and out.exists()


def extract_audio(show: Show, workdir: Path) -> Path:
    out = workdir / f"{show.id}.f32"
    headers = f"User-Agent: {BROWSER_UA}\r\nReferer: {show.page_url}\r\nOrigin: {BASE}\r\n"
    tried = []
    for src in _media_candidates(show.media_url) if show.media_url else []:
        status = _probe_status(src, show.page_url)
        tried.append(f"{src} -> {status}")
        if status not in (200, 206):
            continue
        expected = _duration(src, headers)
        for attempt in range(1, 4):
            ok = _ffmpeg_audio(src, out, headers)
            got = out.stat().st_size / 4 / SAMPLE_RATE if out.exists() else 0
            if ok and got > 60 and (not expected or got >= 0.97 * expected):
                print(f"  audio from {src} ({got / 60:.0f} min)", flush=True)
                return out
            exp = f" of {expected / 60:.0f}" if expected else ""
            print(f"  download incomplete ({got / 60:.0f}{exp} min), retry {attempt}/3", flush=True)
        tried[-1] += " (download kept getting cut off)"
    # Last resort: let yt-dlp find the stream the web player uses.
    res = subprocess.run(["yt-dlp", "-x", "--audio-format", "mp3", "--referer", show.page_url,
                          "--user-agent", BROWSER_UA,
                          "-o", str(workdir / f"{show.id}.%(ext)s"), show.page_url])
    mp3 = workdir / f"{show.id}.mp3"
    if res.returncode == 0 and mp3.exists() and _ffmpeg_audio(str(mp3), out):
        print("  audio via yt-dlp", flush=True)
        return out
    tried.append(f"yt-dlp {show.page_url} -> failed")
    raise RuntimeError("could not download audio. Tried:\n    " + "\n    ".join(tried))


def transcribe(audio: Path) -> str:
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel(CFG["transcribe"]["model"], device="cpu", compute_type="int8")
    # Pass decoded samples directly, so faster-whisper never needs its own audio decoder.
    samples = np.fromfile(audio, dtype=np.float32)
    segments, _ = model.transcribe(samples, language="en", vad_filter=True,
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
        model=CFG["summarize"]["model"], max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content": PROMPT.format(
            title=meta["title"], date=meta["date"], chapters=chapters, transcript=transcript)}])
    # The reply can start with reasoning blocks; keep only the text blocks.
    body = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    if not body:
        raise RuntimeError(f"Claude returned no summary text (stop reason: {msg.stop_reason})")
    if "SLUG:" in body and not body.startswith("SLUG:"):
        body = body[body.index("SLUG:"):]
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
            m = re.search(r"/show/(\d+)", raw.read_text())
            print(summarize(raw).relative_to(ROOT), flush=True)
            if m:  # transcript + summary exist, so never re-transcribe this meeting
                done.add(int(m.group(1)))
                STATE.write_text(json.dumps(sorted(done), indent=0))
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
        failures = 0
        for s in todo:
            try:
                process(s, done)
            except Exception as e:  # keep going; one bad video shouldn't block the rest
                failures += 1
                print(f"  FAILED show {s.id}: {e}", file=sys.stderr, flush=True)
        update_readme()
        if failures:  # make the GitHub run show red so failures aren't silent
            sys.exit(f"{failures} of {len(todo)} meetings failed")
        return
    update_readme()


if __name__ == "__main__":
    main()
