#!/usr/bin/env python3
"""Find what is trending in YouTube clipping, score topics by views, and fetch a source video.

Uses yt-dlp's YouTube search, so no API key is needed. Topic grouping is a judgement
call made by the agent between `scan` and `score`; everything else is deterministic.

    scan      search YouTube for recent, high-view clip videos       -> trends/scan.json
    score     rank agent-written topics by total views                -> trends/ranked-topics.json
    source    find long-form source videos for the winning topic     -> trends/source-candidates.json
    download  download one source video as MP4                        -> <run-root>/source/source.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

SCHEMA_VERSION = 1

# YouTube search "sp" filters: sort by view count, type video, uploaded within the window.
WINDOW_FILTERS = {
    "today": "CAMSBAgCEAE",
    "week": "CAMSBAgDEAE",
    "month": "CAMSBAgEEAE",
    "year": "CAMSBAgFEAE",
}

DEFAULT_QUERIES = [
    "podcast clips",
    "stream highlights",
    "interview clips",
    "funny moments clips",
    "viral clips",
]

YOUTUBE_URL = re.compile(r"^https://(www\.|m\.)?(youtube\.com/(watch\?v=|shorts/|live/)|youtu\.be/)[A-Za-z0-9_-]{6,}")

Runner = Callable[[list[str]], list[dict[str, Any]]]


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def find_yt_dlp() -> str:
    found = shutil.which("yt-dlp")
    if not found:
        raise ValueError("yt-dlp not found on PATH. Install it: winget install yt-dlp.yt-dlp  |  pip install yt-dlp  |  brew install yt-dlp")
    return found


def run_yt_dlp_json(arguments: list[str]) -> list[dict[str, Any]]:
    """Run yt-dlp and parse one JSON object per output line."""
    completed = subprocess.run(
        [find_yt_dlp(), "--no-warnings", *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        raise ValueError(f"yt-dlp failed: {completed.stderr.strip()[-500:]}")
    entries = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            entries.append(json.loads(line))
    return entries


def search_url(query: str, window: str) -> str:
    if window not in WINDOW_FILTERS:
        raise ValueError(f"window must be one of {sorted(WINDOW_FILTERS)}, got {window!r}")
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={WINDOW_FILTERS[window]}"


def normalize(entry: dict[str, Any], query: str) -> dict[str, Any] | None:
    video_id = entry.get("id")
    views = entry.get("view_count")
    if not video_id or not isinstance(views, (int, float)):
        return None
    if entry.get("live_status") in {"is_live", "is_upcoming"}:
        return None
    duration = entry.get("duration")
    url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    return {
        "id": video_id,
        "title": entry.get("title") or "",
        "channel": entry.get("channel") or entry.get("uploader") or "",
        "channelId": entry.get("channel_id"),
        "views": int(views),
        "durationSeconds": duration if isinstance(duration, (int, float)) else None,
        "isShort": "/shorts/" in url or (isinstance(duration, (int, float)) and duration <= 60),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "queries": [query],
    }


def search(query: str, window: str, limit: int, runner: Runner) -> list[dict[str, Any]]:
    entries = runner(["--flat-playlist", "--dump-json", "--playlist-end", str(limit), search_url(query, window)])
    return [video for video in (normalize(entry, query) for entry in entries) if video]


def scan(args: argparse.Namespace, runner: Runner = run_yt_dlp_json) -> dict[str, Any]:
    queries = args.query or DEFAULT_QUERIES
    by_id: dict[str, dict[str, Any]] = {}
    failures = []
    for query in queries:
        try:
            results = search(query, args.window, args.per_query, runner)
        except ValueError as error:
            failures.append({"query": query, "error": str(error)})
            continue
        for video in results:
            existing = by_id.get(video["id"])
            if existing:
                if query not in existing["queries"]:
                    existing["queries"].append(query)
            else:
                by_id[video["id"]] = video
    videos = sorted(by_id.values(), key=lambda video: (-video["views"], video["id"]))
    if not videos:
        raise ValueError(f"No videos found for any query. Failures: {failures}")
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "trend-scan",
        "scannedAt": now_iso(),
        "window": args.window,
        "queries": queries,
        "failures": failures,
        "videoCount": len(videos),
        "videos": videos,
    }
    output = Path(args.output)
    write_json(output, payload)
    return {"output": str(output), "videoCount": len(videos), "window": args.window, "failures": failures,
            "top": [{"views": v["views"], "title": v["title"], "channel": v["channel"]} for v in videos[:10]]}


def validate_topics(topics_doc: Any, known_ids: set[str], count: int) -> list[dict[str, Any]]:
    topics = topics_doc.get("topics") if isinstance(topics_doc, dict) else None
    if not isinstance(topics, list) or not topics:
        raise ValueError('topics file must be {"topics": [{"name", "sourceQuery", "videoIds"}, ...]}')
    if len(topics) > count:
        raise ValueError(f"{len(topics)} topics given; at most {count} allowed")
    seen_names: set[str] = set()
    claimed: dict[str, str] = {}
    for index, topic in enumerate(topics):
        where = f"topics[{index}]"
        if not isinstance(topic, dict):
            raise ValueError(f"{where} must be an object")
        name = topic.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}.name must be a non-empty string")
        if name.strip().lower() in seen_names:
            raise ValueError(f"{where}.name {name!r} is a duplicate")
        seen_names.add(name.strip().lower())
        if not isinstance(topic.get("sourceQuery"), str) or not topic["sourceQuery"].strip():
            raise ValueError(f"{where}.sourceQuery must be a non-empty search string for the long-form source")
        ids = topic.get("videoIds")
        if not isinstance(ids, list) or not ids:
            raise ValueError(f"{where}.videoIds must be a non-empty list")
        for video_id in ids:
            if video_id not in known_ids:
                raise ValueError(f"{where}.videoIds contains {video_id!r}, which is not in the scan")
            if video_id in claimed:
                raise ValueError(f"video {video_id!r} is in both {claimed[video_id]!r} and {name!r}; each video belongs to one topic")
            claimed[video_id] = name
    return topics


def score(args: argparse.Namespace) -> dict[str, Any]:
    scan_doc = read_json(Path(args.scan))
    videos = {video["id"]: video for video in scan_doc.get("videos", [])}
    topics = validate_topics(read_json(Path(args.topics)), set(videos), args.count)
    ranked = []
    for topic in topics:
        members = sorted((videos[video_id] for video_id in topic["videoIds"]), key=lambda v: -v["views"])
        total = sum(video["views"] for video in members)
        ranked.append({
            "name": topic["name"].strip(),
            "sourceQuery": topic["sourceQuery"].strip(),
            "why": topic.get("why", ""),
            "totalViews": total,
            "videoCount": len(members),
            "channelCount": len({video["channel"] for video in members}),
            "averageViews": round(total / len(members)),
            "topVideo": {key: members[0][key] for key in ("title", "channel", "views", "url")},
            "videoIds": [video["id"] for video in members],
        })
    ranked.sort(key=lambda topic: (-topic["totalViews"], -topic["channelCount"], topic["name"].lower()))
    for rank, topic in enumerate(ranked, start=1):
        topic["rank"] = rank
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "ranked-topics",
        "scoredAt": now_iso(),
        "scoring": "totalViews desc, then channelCount desc, then name",
        "window": scan_doc.get("window"),
        "shortfall": max(0, args.count - len(ranked)),
        "winner": ranked[0]["name"],
        "topics": ranked,
    }
    output = Path(args.output)
    write_json(output, payload)
    return {"output": str(output), "winner": ranked[0]["name"], "shortfall": payload["shortfall"],
            "topics": [{"rank": t["rank"], "name": t["name"], "totalViews": t["totalViews"], "videoCount": t["videoCount"]} for t in ranked]}


def source(args: argparse.Namespace, runner: Runner = run_yt_dlp_json) -> dict[str, Any]:
    query = args.query
    if not query and args.ranked:
        ranked = read_json(Path(args.ranked))
        topic = next((t for t in ranked["topics"] if t["rank"] == args.rank), None)
        if topic is None:
            raise ValueError(f"no topic with rank {args.rank} in {args.ranked}")
        query = topic["sourceQuery"]
    if not query:
        raise ValueError("pass --query or --ranked")
    minimum = args.min_minutes * 60
    maximum = args.max_minutes * 60
    found = search(query, args.window, args.limit, runner)
    candidates = [
        video for video in found
        if not video["isShort"] and video["durationSeconds"] is not None and minimum <= video["durationSeconds"] <= maximum
    ]
    if args.channel:
        wanted = args.channel.strip().lower()
        candidates = [video for video in candidates if wanted in video["channel"].lower()]
    candidates.sort(key=lambda video: (-video["views"], video["id"]))
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "source-candidates",
        "searchedAt": now_iso(),
        "query": query,
        "window": args.window,
        "durationRangeMinutes": [args.min_minutes, args.max_minutes],
        "channel": args.channel,
        "pick": candidates[0] if candidates else None,
        "candidates": candidates,
    }
    output = Path(args.output)
    write_json(output, payload)
    if not candidates:
        return {"output": str(output), "query": query, "pick": None, "ready": False,
                "note": f"No long-form video between {args.min_minutes} and {args.max_minutes} minutes found. Try --window year or a broader --query."}
    return {"output": str(output), "query": query, "candidateCount": len(candidates),
            "pick": {key: candidates[0][key] for key in ("title", "channel", "views", "durationSeconds", "url")}}


def download(args: argparse.Namespace) -> dict[str, Any]:
    url = args.url
    if not url and args.candidates:
        pick = read_json(Path(args.candidates)).get("pick")
        if not pick:
            raise ValueError(f"{args.candidates} has no pick")
        url = pick["url"]
    if not url:
        raise ValueError("pass --url or --candidates")
    if not YOUTUBE_URL.match(url):
        raise ValueError(f"not a YouTube video URL: {url!r}")
    run_root = Path(args.run_root)
    target_dir = run_root / "source"
    target = target_dir / "source.mp4"
    if target.exists() and not args.replace:
        raise ValueError(f"{target} already exists; pass --replace to download again")
    target_dir.mkdir(parents=True, exist_ok=True)
    max_seconds = args.max_minutes * 60
    command = [
        find_yt_dlp(), "--no-warnings", "--no-playlist",
        "--match-filter", f"duration <= {max_seconds} & !is_live",
        "-f", f"bv*[height<={args.max_height}][ext=mp4]+ba[ext=m4a]/bv*[height<={args.max_height}]+ba/b[height<={args.max_height}]/b",
        "--merge-output-format", "mp4",
        "--write-info-json", "--force-overwrites",
        "-o", str(target_dir / "source.%(ext)s"),
        url,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise ValueError(f"yt-dlp download failed: {completed.stderr.strip()[-800:]}")
    if not target.is_file():
        raise ValueError(f"download finished but {target} is missing; the video may be longer than {args.max_minutes} minutes or live")
    info_path = target_dir / "source.info.json"
    info = read_json(info_path) if info_path.is_file() else {}
    record = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "source-download",
        "downloadedAt": now_iso(),
        "url": url,
        "path": str(target),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "views": info.get("view_count"),
        "durationSeconds": info.get("duration"),
        "uploadDate": info.get("upload_date"),
        "license": info.get("license"),
        "bytes": target.stat().st_size,
    }
    write_json(target_dir / "download.json", record)
    return {key: record[key] for key in ("path", "title", "channel", "views", "durationSeconds", "bytes")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trend scan, topic scoring, and source download for the Go workflow.")
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("scan", help="search YouTube for recent high-view clip videos")
    command.add_argument("--query", action="append", help="search phrase; repeatable. Defaults to a broad clipping set.")
    command.add_argument("--window", choices=sorted(WINDOW_FILTERS), default="week")
    command.add_argument("--per-query", type=int, default=30)
    command.add_argument("--output", required=True)

    command = sub.add_parser("score", help="rank agent-written topics by total views")
    command.add_argument("--scan", required=True)
    command.add_argument("--topics", required=True)
    command.add_argument("--count", type=int, default=5)
    command.add_argument("--output", required=True)

    command = sub.add_parser("source", help="find long-form source videos for a topic")
    command.add_argument("--query")
    command.add_argument("--ranked", help="ranked-topics.json; uses the sourceQuery of --rank")
    command.add_argument("--rank", type=int, default=1)
    command.add_argument("--window", choices=sorted(WINDOW_FILTERS), default="month")
    command.add_argument("--limit", type=int, default=30)
    command.add_argument("--min-minutes", type=float, default=15)
    command.add_argument("--max-minutes", type=float, default=240)
    command.add_argument("--channel", help="keep only videos whose channel name contains this text, e.g. the original creator")
    command.add_argument("--output", required=True)

    command = sub.add_parser("download", help="download one YouTube video as source.mp4")
    command.add_argument("--url")
    command.add_argument("--candidates", help="source-candidates.json; downloads its pick")
    command.add_argument("--run-root", required=True)
    command.add_argument("--max-height", type=int, default=1080)
    command.add_argument("--max-minutes", type=float, default=240)
    command.add_argument("--replace", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = {"scan": scan, "score": score, "source": source, "download": download}[args.command](args)
        print_json(result)
        return 0 if result.get("ready", True) else 1
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print_json({"error": str(error), "command": args.command})
        return 1


if __name__ == "__main__":
    sys.exit(main())
