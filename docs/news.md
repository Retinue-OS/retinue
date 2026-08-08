# The news feed

Broadcast-style inbound — a channel announcement, a newsletter blurb, a feed
item — needs no reply and fits no project, so triage has nowhere to put it and it
gets archived and lost. The news feed is the home for exactly that class: a page
of **references to sources**, ranked by how much each one matters to *this user*
right now, read aloud on request, and taught by the user's own taps.

Three moving parts, each deliberately small:

| Part | What it is | Where |
|---|---|---|
| Collection | fetch declared feeds, normalize to link-sized items | `scripts/news-fetch.py`, `scripts/news-add.py` |
| Ranking | one number per item, decayed at read time | `scripts/news_store.py`, `GET /news` |
| Learning | an agent that scores items and remembers preferences | `.claude/agents/herald.md`, `scripts/news-curate.py` |

## The item

An item is a **reference, never a copy**:

```json
{
  "id": "9f2c…", "title": "Sommerfest, 30 July", "url": "https://…",
  "source": "Quartierverein", "summary": "Open-air at the Werdinsel, 18:00.",
  "published": "2026-07-23T16:00:00+00:00", "lang": "de",
  "importance": 4.5, "half_life_hours": null, "expires": "2026-07-30T23:00:00+00:00",
  "reason": "Your neighbourhood, a Saturday you are free", "tags": ["zurich", "events"]
}
```

The summary is the excerpt the source itself published, truncated. Articles are
read at the source; nothing is mirrored here, and no agent fetches article
bodies.

## Ranking: one number, sampled now

```
score = importance × 0.5 ^ (age_hours / half_life_hours)
```

with two exceptions that carry the whole "relevance changes over time" idea:

- **A dated item** (`expires` set — an event, a registration deadline) does not
  decay at all. It holds full weight until the date, then scores 0 and leaves the
  feed in one step. That is what keeps next week's concert above today's chatter
  and takes last week's concert out.
- **An opened item** is damped (×0.25), not removed, so a mis-tap loses nothing.

Nothing is stored sorted and nothing is re-scored as time passes: the feed
changes because the clock moved. `GET /news` samples the formula per request.

This replaces the keyframe-curve design originally sketched in issue #25. The
curve was more expressive and much more machinery — a vocabulary to settle, an
interpolating SPARQL query, a numeric time coordinate beside every timestamp
because SPARQL cannot subtract two `xsd:dateTime`s into a number. `importance +
optional expiry` covers the motivating cases (event fades after the date,
blurb fades over days, evergreen stays via a long half-life) in one line of
arithmetic that anyone reading the feed can predict.

## Where it lives

A plain JSON store on the persistent volume, `NEWS_DIR`
(default `/root/.retinue/news`, wired in `docker-compose.yml`):

```
items.json       every known item, newest first
preferences.md   what the Herald has learned about the user's taste
feedback.jsonl   append-only log of user signals
state.json       how much feedback the curator has folded in
```

News is high-churn, disposable data: a few hundred rows rewritten hourly, with
no value in a month. Putting it in a chamber would mean a git commit and a
QLever index rebuild per fetch, for facts nobody will query. Dashboard
conversations already set this precedent. What *is* durable — the user's
preferences — is prose in `preferences.md`, readable and editable by both the
agent and the user.

## Declaring sources

Any chamber declares its feeds in a **`.news.json`** at its root — the same
per-chamber manifest convention as `.refresh.json` and `.schedule.json`, so a
deployment adds sources by editing its own chamber, never the framework:

```json
{
  "feeds": [
    {"id": "nzz", "url": "https://www.nzz.ch/recent.rss", "label": "NZZ"},
    {"id": "club", "url": "https://club.example/feed.xml", "label": "Club",
     "half_life_hours": 168, "lang": "de"}
  ]
}
```

Optional keys: `label` (shown in the UI), `half_life_hours` (this source's
fading speed — a weekly bulletin ages slower than a wire), `lang` (BCP-47, used
by read-aloud when an item states no language of its own), `enabled: false` to
park a feed. See `examples/chambers/hitchhiker/.news.json`.

RSS 2.0, RSS 1.0/RDF and Atom all parse; the fetcher is stdlib-only
(`urllib` + `xml.etree`) and one broken feed never stops the others.

**Non-feed sources** — a Telegram channel post, a newsletter the Secretary meets
during triage, a link someone sent — go in through `scripts/news-add.py`:

```bash
python3 /workspace/scripts/news-add.py \
  --title "Sommerfest, 30 July, Zürich" --url https://t.me/quartier/1234 \
  --source "Quartierverein (Telegram)" --summary "Open-air, Werdinsel, 18:00." \
  --expires 2026-07-30T23:00
```

That keeps the "which channels are news vs. correspondence" question where it
belongs — with whichever agent already reads that channel — instead of building
a second inbound pipeline.

## The Herald: the agent that remembers

`.claude/agents/herald.md` is a core subagent (like the Archivist). It never
sorts anything; it supplies judgement:

1. **Scores** each new item (`importance`, optional `half_life_hours`,
   `expires`, a one-line `reason` shown to the user, `tags`) from title, source
   and summary — writing them back in one call to `scripts/news-score.py`.
2. **Maintains `preferences.md`** from the user's feedback: what they want more
   and less of, each line traceable to something they actually did, pruned when
   a topic goes quiet.

It runs from the scheduler through **`scripts/news-curate.py`**, a `command` job
(so the scheduler spends no credits invoking it) whose gate is a file read: no
unscored items and no new feedback means **nothing is spawned**. The feedback
cursor only advances after a clean run, so a crashed session re-reads its input
instead of losing it.

Base schedule (`.schedule.json`): `news-fetch` hourly, `news-curate` hourly.

## The user's side of the loop

On the news card and page, every item carries three actions:

- 👍 **more like this** — nudges that item now (+1 importance), logged for the Herald
- 👎 **less like this** — nudges down and marks it read
- ✕ **not interested** — hides it

Plus, on the page, a free-text note ("less crypto, more local politics") — the
strongest signal there is, because it needs no inference. Each tap has both an
immediate local effect (so the feed reacts to the tap) and a durable one (the
Herald generalizes it into the profile on the next run). The profile itself is
shown at the bottom of the news page and is **editable by hand** — a ranking you
cannot inspect is one you cannot trust, and the Herald is instructed to merge
with what it finds rather than clobber it.

## Read-aloud

The news page reads the feed aloud through the **browser's own speech
synthesis** (`webapp/components/speech.js`): ▶ Listen walks the ranked items,
⏭ skips one, ⏹ stops, and the item being read is highlighted. Each item is
spoken in the language it declares; no language is special-cased.

This is the same choice the conversation card already makes for playing Ara's
replies aloud, for the same reasons. Why not Piper, which this stack runs for
Signal voice notes: that voice belongs to a *message* — an artefact produced,
sent and stored. Reading a page aloud is a property of the page, wants to stop
mid-sentence, and should keep working when the container is unreachable and the
service worker is serving a cached shell. The browser does all of that natively,
with the voices the user has already installed. A server-side voice can be added
later without changing the store or the ranking.

## API

| Endpoint | Purpose |
|---|---|
| `GET /news?scope=feed\|read\|hidden\|all&limit=n` | ranked items, sampled now |
| `GET /news/preferences` | `{markdown, updated}` — the Herald's memory |
| `POST /news/preferences` | replace it (the user editing their own profile) |
| `POST /news/feedback` | `{id?, signal, note?}`, signal `up\|down\|read\|hide\|note` |

All four are behind the dashboard's own edge auth, like `/projects`.

## Tunables

| Variable | Default | Meaning |
|---|---|---|
| `NEWS_DIR` | `/root/.retinue/news` | where the store lives |
| `NEWS_HALF_LIFE_HOURS` | `48` | fading speed for items with no per-item value |
| `NEWS_MAX_AGE_DAYS` | `30` | pruned on every fetch |
| `NEWS_MAX_ITEMS` | `500` | hard cap, newest kept |
| `NEWS_CURATE_BATCH` | `40` | items scored per curation run |
| `NEWS_FETCH_TIMEOUT` | `20` | seconds per feed |
