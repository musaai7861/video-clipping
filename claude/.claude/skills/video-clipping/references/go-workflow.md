# Go Workflow

Triggered when the user says **"go"** (alone, or with a hint such as "go gaming" or "go 3 clips"). One word, and the run goes from "what is trending" to finished shorts without further prompts.

Everything uses `yt-dlp` search. No API key, no sign-in. Run commands from the skill directory, with `python` (or `py` where that launcher exists).

## Steps

Pick a run folder: `go-runs\<YYYY-MM-DD>-<topic-slug>` under the user's working directory. Create it after the topic is chosen. Keep the trend files in `go-runs\<YYYY-MM-DD>-scan\` until then, or just write them straight into the final folder's `trends\`.

1. **Scan.** Search for recent, high-view clip videos.

   ```powershell
   python scripts/trends.py scan --output 'go-runs\2026-11-16\trends\scan.json'
   ```

   Default window is `week`. Default queries are a broad clipping set (podcast clips, stream highlights, interview clips, funny moments, viral clips). If the user gave a hint ("go gaming"), pass your own `--query` phrases for it, repeatable. Use `--window month` if the week is thin.

2. **Group into topics.** Read `scan.json` and write `trends\topics.json` with up to five topics:

   ```json
   {
     "topics": [
       {
         "name": "Shane Gillis SNL backlash",
         "sourceQuery": "Shane Gillis podcast full episode",
         "why": "4 clip channels posted it this week",
         "videoIds": ["abc123", "def456"]
       }
     ]
   }
   ```

   A topic is a specific clippable thing — a person, a show, an episode, an event — not a genre. "Podcasts" is not a topic; "Kill Tony controversy" is. Prefer topics that several channels are clipping, since that shows demand rather than one big channel's reach.

   Skip music videos, trailers, kids content, news broadcasts, and anything not in English unless the user asked for it. Each video goes in at most one topic. Leave unrelated videos out.

   `sourceQuery` must find the **long-form original** that clips are cut from (the full podcast episode, the full stream VOD, the full interview), not more clips.

3. **Score.**

   ```powershell
   python scripts/trends.py score --scan 'go-runs\2026-11-16\trends\scan.json' --topics 'go-runs\2026-11-16\trends\topics.json' --output 'go-runs\2026-11-16\trends\ranked-topics.json'
   ```

   Ranks by total views of the topic's videos, ties broken by number of distinct channels. The result is canonical: do not reorder it or edit scores. Rank 1 is the winner.

4. **Find the source.**

   ```powershell
   python scripts/trends.py source --ranked 'go-runs\2026-11-16\trends\ranked-topics.json' --output 'go-runs\2026-11-16\trends\source-candidates.json'
   ```

   Keeps non-Short videos between 15 and 240 minutes from the last month, sorted by views; `pick` is the top one. Check that the pick really is the original for the topic (right person, right show, full length) and not a compilation or reaction video. The most-viewed long video is often a talk show or reaction segment *about* the topic rather than the original; when the topic's scanned videos point to the original channel, rerun with `--channel 'Channel Name'` to keep only that channel. Otherwise choose a better entry from `candidates` and pass its URL in step 5.

   If nothing fits, retry with `--window year` or a sharper `--query`. If rank 1 still has no usable source, move on with `--rank 2`, and say so in the report.

5. **Download.**

   ```powershell
   python scripts/trends.py download --candidates 'go-runs\2026-11-16\trends\source-candidates.json' --run-root 'go-runs\2026-11-16-shane-gillis'
   ```

   Writes `source\source.mp4` (up to 1080p) and `source\download.json`. Pass `--url` instead of `--candidates` to download a different candidate.

6. **Clip.** Run the normal workflow from SKILL.md on `source\source.mp4`, using the same run folder as `--run-root`. In selection, favour moments about the winning topic: those are what is getting views right now.

7. **Report.** One short summary: the five topics with total views, the winner, the source video (title, channel, URL), then the clips as SKILL.md's delivery step describes.

## Status updates

Do not stop to ask between steps. Post one line when the winner and source are chosen ("Top topic: X, 12.4M views across 9 videos. Source: Y, 2 h 10 min. Downloading."), then keep going.

Stop and ask only when: the scan finds nothing usable, no source can be found for the top two topics, or two people are equally plausible as the reframe subject (as in the normal workflow).

## Rights

Clipping other creators' videos is only safe when the creator allows it (many podcasts and streamers run clipping programs) or the use is otherwise permitted. The skill cannot check this. Put the source channel in the report so the user can confirm before posting.
