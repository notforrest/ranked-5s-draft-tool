# LoL Ranked 5s Draft Assistant

A local, single-driver tool for live Tournament Draft champion-select suggestions, combining
your group's Riot data (mastery, personal win rates) with a curated tier list and pro-match
synergy data. See `/Users/forrestsun/.claude/plans/my-friends-and-i-nifty-wozniak.md` for the
full design writeup.

One person (the "driver") runs this on their own machine and manually clicks in bans/picks as
they happen in the real client, sharing their screen or calling out suggestions to the team.

## 1. One-time setup

```bash
cd lol-draft-assistant
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
python3 -c "
from draftassistant.db import connection
from draftassistant.staticdata import ddragon_client
conn = connection.get_conn()
version = ddragon_client.get_latest_version()
ddragon_client.sync_champions(conn)
conn.commit()
print('synced', version)
"
```

## 4. Add your roster

The web UI's setup screen only lets you *select* from your roster -- add players with:

```bash
python scripts/add_player.py "Forrest" "ForrestSun" "NA1" --roles MID
python scripts/add_player.py "Alex" "AlexPlays" "NA1" --roles TOP
# ... one per friend. --platform-region defaults to na1; pass --platform-region euw1 etc. if needed.
```

## 5. (Optional but recommended) Load meta/synergy data

- **Global tier list**: copy `data/curated/tier_list/EXAMPLE.yaml.template` to e.g.
  `data/curated/tier_list/14.13.yaml` (drop the `.template` suffix), fill in real numbers from a
  site like u.gg/op.gg/lolalytics for the champions/roles you care about, then:
  ```bash
  python scripts/import_tier_list.py data/curated/tier_list/14.13.yaml
  ```
  Riot's API doesn't expose this data directly, so it's only ever as fresh as your last edit --
  worth re-doing after major patches.
- **Pro-match synergy data**: download a current CSV from Oracle's Elixir
  (oracleselixir.com's data page) and run:
  ```bash
  python scripts/import_oracles_elixir.py path/to/downloaded.csv
  ```

Both are optional -- the tool still works without them, just with fewer signals feeding
suggestions (personal mastery/win-rate and role-fit still apply either way).

## 6. Before each session: refresh personal data

```bash
python scripts/run_pre_draft_refresh.py
```

Pulls fresh mastery + match history for your roster. Run this *before* queuing up, not during a
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
