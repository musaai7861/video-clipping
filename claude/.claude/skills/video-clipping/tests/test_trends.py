from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import trends  # noqa: E402


def entry(video_id: str, views: int, duration: float | None = 300, channel: str = "Chan", title: str = "t", **extra) -> dict:
    return {"id": video_id, "view_count": views, "duration": duration, "channel": channel, "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}", **extra}


class FakeRunner:
    def __init__(self, results: dict[str, list[dict]]):
        self.results = results
        self.calls: list[list[str]] = []

    def __call__(self, arguments: list[str]) -> list[dict]:
        self.calls.append(arguments)
        url = arguments[-1]
        for query, entries in self.results.items():
            if f"search_query={trends.quote_plus(query)}&" in url:
                return entries
        return []


class TrendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def scan(self, runner: FakeRunner, queries: list[str]) -> dict:
        args = argparse.Namespace(query=queries, window="week", per_query=10, output=str(self.root / "scan.json"))
        return trends.scan(args, runner=runner)

    def test_search_url_uses_window_filter(self) -> None:
        self.assertIn("sp=CAMSBAgDEAE", trends.search_url("podcast clips", "week"))
        self.assertIn("search_query=podcast+clips", trends.search_url("podcast clips", "week"))
        with self.assertRaises(ValueError):
            trends.search_url("x", "decade")

    def test_scan_dedupes_sorts_and_drops_live(self) -> None:
        runner = FakeRunner({
            "a": [entry("v1", 100), entry("v2", 900), entry("live", 10**9, live_status="is_live")],
            "b": [entry("v1", 100), entry("v3", 500), {"id": "noviews"}],
        })
        result = self.scan(runner, ["a", "b"])
        scan = json.loads((self.root / "scan.json").read_text(encoding="utf-8"))
        self.assertEqual([v["id"] for v in scan["videos"]], ["v2", "v3", "v1"])
        self.assertEqual(scan["videos"][2]["queries"], ["a", "b"])
        self.assertEqual(result["videoCount"], 3)

    def test_scan_marks_shorts(self) -> None:
        runner = FakeRunner({"a": [entry("s1", 5, duration=45), entry("l1", 5, duration=1200)]})
        self.scan(runner, ["a"])
        videos = {v["id"]: v for v in json.loads((self.root / "scan.json").read_text(encoding="utf-8"))["videos"]}
        self.assertTrue(videos["s1"]["isShort"])
        self.assertFalse(videos["l1"]["isShort"])

    def test_scan_with_no_results_fails(self) -> None:
        with self.assertRaises(ValueError):
            self.scan(FakeRunner({}), ["a"])

    def write_scan_and_topics(self, topics: list[dict]) -> argparse.Namespace:
        self.scan(FakeRunner({"a": [entry("v1", 100, channel="A"), entry("v2", 900, channel="B"),
                                    entry("v3", 500, channel="A"), entry("v4", 450, channel="C"),
                                    entry("v5", 50, channel="D")]}), ["a"])
        (self.root / "topics.json").write_text(json.dumps({"topics": topics}), encoding="utf-8")
        return argparse.Namespace(scan=str(self.root / "scan.json"), topics=str(self.root / "topics.json"),
                                  count=5, output=str(self.root / "ranked.json"))

    def test_score_ranks_by_total_views(self) -> None:
        args = self.write_scan_and_topics([
            {"name": "Small", "sourceQuery": "small full", "videoIds": ["v5"]},
            {"name": "Big single", "sourceQuery": "big full", "videoIds": ["v2"]},
            {"name": "Many", "sourceQuery": "many full", "videoIds": ["v1", "v3", "v4"]},
        ])
        result = trends.score(args)
        self.assertEqual(result["winner"], "Many")
        self.assertEqual([t["name"] for t in result["topics"]], ["Many", "Big single", "Small"])
        self.assertEqual(result["topics"][0]["totalViews"], 1050)
        self.assertEqual(result["shortfall"], 2)
        ranked = json.loads((self.root / "ranked.json").read_text(encoding="utf-8"))
        self.assertEqual(ranked["topics"][0]["channelCount"], 2)
        self.assertEqual(ranked["topics"][0]["topVideo"]["views"], 500)

    def test_score_tie_breaks_on_channel_count(self) -> None:
        args = self.write_scan_and_topics([
            {"name": "A one channel", "sourceQuery": "q", "videoIds": ["v3"]},        # 500 views, 1 channel
            {"name": "B two channels", "sourceQuery": "q", "videoIds": ["v4", "v5"]},  # 500 views, 2 channels
        ])
        result = trends.score(args)
        self.assertEqual(result["winner"], "B two channels")

    def test_score_rejects_bad_topics(self) -> None:
        cases = [
            [{"name": "X", "sourceQuery": "q", "videoIds": ["missing"]}],
            [{"name": "X", "sourceQuery": "q", "videoIds": ["v1"]}, {"name": "Y", "sourceQuery": "q", "videoIds": ["v1"]}],
            [{"name": "X", "sourceQuery": "", "videoIds": ["v1"]}],
            [{"name": "X", "sourceQuery": "q", "videoIds": ["v1"]}, {"name": "x", "sourceQuery": "q", "videoIds": ["v2"]}],
            [{"name": f"T{i}", "sourceQuery": "q", "videoIds": [f"v{i}"]} for i in range(1, 6)] + [{"name": "T6", "sourceQuery": "q", "videoIds": ["v1"]}],
        ]
        for topics in cases:
            with self.subTest(topics=topics):
                args = self.write_scan_and_topics(topics)
                with self.assertRaises(ValueError):
                    trends.score(args)

    def test_source_filters_to_long_form_and_picks_most_viewed(self) -> None:
        runner = FakeRunner({"full episode": [
            entry("short", 10**8, duration=50),
            entry("tooshort", 10**7, duration=300),
            entry("ep1", 2000, duration=3600),
            entry("ep2", 9000, duration=5400),
            entry("unknown", 10**6, duration=None),
        ]})
        args = argparse.Namespace(query="full episode", ranked=None, rank=1, window="month", limit=30,
                                  min_minutes=15, max_minutes=240, channel=None, output=str(self.root / "src.json"))
        result = trends.source(args, runner=runner)
        self.assertEqual(result["pick"]["url"], "https://www.youtube.com/watch?v=ep2")
        saved = json.loads((self.root / "src.json").read_text(encoding="utf-8"))
        self.assertEqual([c["id"] for c in saved["candidates"]], ["ep2", "ep1"])

    def test_source_reads_query_from_ranked_topics(self) -> None:
        (self.root / "ranked.json").write_text(json.dumps({"topics": [
            {"rank": 1, "sourceQuery": "first full"}, {"rank": 2, "sourceQuery": "second full"}]}), encoding="utf-8")
        runner = FakeRunner({"second full": [entry("ep", 10, duration=2000)]})
        args = argparse.Namespace(query=None, ranked=str(self.root / "ranked.json"), rank=2, window="month", limit=30,
                                  min_minutes=15, max_minutes=240, channel=None, output=str(self.root / "src.json"))
        result = trends.source(args, runner=runner)
        self.assertEqual(result["query"], "second full")
        self.assertIn("second+full", runner.calls[0][-1])

    def test_source_channel_filter(self) -> None:
        runner = FakeRunner({"q": [entry("react", 10**7, duration=1300, channel="The Daily Show"),
                                   entry("orig", 10**6, duration=3100, channel="60 Minutes")]})
        args = argparse.Namespace(query="q", ranked=None, rank=1, window="month", limit=30,
                                  min_minutes=15, max_minutes=240, channel="60 minutes", output=str(self.root / "src.json"))
        self.assertEqual(trends.source(args, runner=runner)["pick"]["url"], "https://www.youtube.com/watch?v=orig")

    def test_source_reports_when_nothing_fits(self) -> None:
        args = argparse.Namespace(query="q", ranked=None, rank=1, window="month", limit=30,
                                  min_minutes=15, max_minutes=240, channel=None, output=str(self.root / "src.json"))
        result = trends.source(args, runner=FakeRunner({"q": [entry("s", 5, duration=30)]}))
        self.assertFalse(result["ready"])
        self.assertIsNone(result["pick"])

    def test_download_rejects_non_youtube_urls(self) -> None:
        for url in ["https://example.com/video.mp4", "file:///C:/x.mp4", "https://youtube.com.evil.test/watch?v=abcdefg"]:
            with self.subTest(url=url):
                args = argparse.Namespace(url=url, candidates=None, run_root=str(self.root / "run"),
                                          max_height=1080, max_minutes=240, replace=False)
                with self.assertRaises(ValueError):
                    trends.download(args)

    def test_youtube_url_pattern_accepts_common_forms(self) -> None:
        for url in ["https://www.youtube.com/watch?v=fSI7O92UYNs", "https://youtu.be/fSI7O92UYNs",
                    "https://m.youtube.com/watch?v=fSI7O92UYNs", "https://www.youtube.com/live/fSI7O92UYNs"]:
            self.assertTrue(trends.YOUTUBE_URL.match(url), url)


if __name__ == "__main__":
    unittest.main()
