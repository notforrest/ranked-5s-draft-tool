# Ranked 5s Draft Tool

A local, single-driver tool for live Tournament Draft champion-select suggestions, combining
your group's Riot data (mastery, personal win rates) with a curated tier list and pro-match
synergy data. See `/Users/forrestsun/.claude/plans/my-friends-and-i-nifty-wozniak.md` for the
full design writeup.

One person (the "driver") runs this on their own machine and manually clicks in bans/picks as
they happen in the real client, sharing their screen or calling out suggestions to the team.

## 1. One-time setup

```bash
cd ranked-5s-draft-tool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env, see step 2
python scripts/init_db.py     # only needed once -- refuses to run if the DB already exists
```

**Network note:** Data Dragon (`ddragon.leagueoflegends.com`) and Riot's API hosts
(`*.api.riotgames.com`) both need to be reachable for champion sync and any live data pull to
work. If you're on a corporate/managed network with web filtering (some gaming domains get
blocked by security gateways like Zscaler), those calls will fail -- run this from an
unrestricted network (e.g. home wifi) instead.

## 2. Get a Riot API key

1. Go to the Riot Developer Portal and register/sign in, then generate a **personal** API key.
2. Paste it into `.env` as `RIOT_API_KEY=...`.
3. **Personal keys expire roughly every 24 hours** -- you'll need to paste in a fresh one before
   each session you plan to refresh data. There's no way around this without applying for (and
   being approved for) a Production key, which isn't necessary for a friend-group tool.

## 3. Sync champion data (one-time, then occasionally per patch)

```bash
python scripts/sync_champions.py
```

## 4. Add your roster

The web UI's setup screen only lets you _select_ from your roster -- populate it from a file:
copy `data/curated/roster.yaml.template` to `data/curated/roster.yaml` (drop the `.template`
suffix), fill in one entry per friend (Riot ID, region, preferred role), then:

```bash
python scripts/import_roster.py data/curated/roster.yaml
```

Re-run that command any time you edit the file -- adding a friend, fixing a typo'd Riot ID, or
changing someone's role. It's an upsert (matched by Riot ID), so re-importing never deletes
anyone.

## 5. (Optional but recommended) Load meta/synergy data

Both of these are optional -- the tool still works without them, just with fewer signals
feeding suggestions (personal mastery/win-rate and role-fit still apply either way).

- **Global tier list**: easiest is the **"Fetch Tier List" button on the setup screen's "4. Meta
  Data" panel** -- automatically pulls current win/pick rate from OP.GG (unofficial, best-effort;
  see `src/draftassistant/staticdata/opgg_client.py` for why OP.GG specifically and why this can
  break if they change their API). Same thing from the CLI: `python scripts/fetch_tier_list.py`.
  If it's ever down/broken, the manual path still works independently: copy
  `data/curated/tier_list/EXAMPLE.yaml.template` to e.g. `data/curated/tier_list/14.13.yaml`,
  fill in numbers by hand from any site you like, then `python scripts/import_tier_list.py
data/curated/tier_list/14.13.yaml`.
- **Pro-match synergy data (Oracle's Elixir)**: download the current file from
  [Oracle's Elixir](https://drive.google.com/drive/u/1/folders/1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH) yourself (the
  exact link isn't stable enough to hardcode), then either:
    - **Upload it** via the "Upload CSV" button on the setup screen's "4. Meta Data" panel, or
    - **Configure it once** by pasting the link into `.env` as `ORACLES_ELIXIR_CSV_URL=...`, after
      which the setup screen's "Fetch from Configured URL" button downloads and imports it
      automatically -- only re-paste the link if it goes stale (roughly once per split).
    - CLI equivalent: `python scripts/import_oracles_elixir.py path/to/downloaded.csv`.

## 6. Before each session: refresh personal data

```bash
python scripts/run_pre_draft_refresh.py
```

Pulls fresh mastery + match history for your roster. Run this _before_ queuing up, not during a
draft -- it makes real network calls and can take a little while. If it fails with an
auth/expired-key error, get a fresh key (step 2) and re-run; it's incremental, so re-running is
cheap.

## 7. Run it

```bash
source .venv/bin/activate
PYTHONPATH=src uvicorn draftassistant.api.main:app --host 127.0.0.1 --port 8731
```

Open `http://127.0.0.1:8731/` in a browser. Pick your 5 for tonight, assign roles, confirm your
side, and start the draft. Click champions in the grid as bans/picks happen in your real client
-- the tool infers whose turn it is automatically. Click any already-filled slot on the board to
correct a misclick.
