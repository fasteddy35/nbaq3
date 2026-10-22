"""Q3 6:00 checkpoint collector.

The first time a game's 3rd-quarter clock reaches 6:00, this records the live
score, Pinnacle's live spread and total (one Odds API pull per game), and
pace_checkpoint.py's Q3 possessions/pace from 12:00 down to 6:00.

Rows go to data/q3_checkpoints.csv. That file is also the record of which
games are done, so a game is never pulled twice, even across runs.
"""
import csv
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ── CONFIG ──────────────────────────────────────────────────────
CHECKPOINT_MINUTE = 6.0                          # Q3 clock to snapshot at (6:00 left)
ODDS_CUTOFF_MIN   = 4.0                          # first seen below 4:00 -> too late, skip odds
POLL_SECONDS      = 15
LOOKAHEAD         = timedelta(minutes=90)        # stay running if a game tips within this
MAX_RUNTIME       = timedelta(hours=5, minutes=40)  # GitHub kills jobs at 6h
PBP_WAIT          = timedelta(minutes=10)        # max wait for play-by-play to pass 6:00
DATA_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'q3_checkpoints.csv')
ESPN_API          = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
ODDS_API          = 'https://api.the-odds-api.com/v4/sports/basketball_nba'
ODDS_KEY          = os.environ.get('ODDS_API_KEY', '')
ET                = ZoneInfo('America/New_York')
SKIP_STATUSES     = {'STATUS_POSTPONED', 'STATUS_CANCELED', 'STATUS_SUSPENDED'}
# ────────────────────────────────────────────────────────────────

FIELDS = [
    'game_date', 'espn_id', 'away', 'home', 'tipoff_utc',
    'captured_utc', 'capture_period', 'capture_clock', 'away_score', 'home_score', 'margin',
    'q3_poss_to_6', 'q3_pace_to_6', 'score_at_6', 'margin_at_6',
    'odds_status', 'odds_updated_utc',
    'spread_home', 'spread_home_price', 'spread_away', 'spread_away_price',
    'total', 'over_price', 'under_price', 'odds_credits_left',
]


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


# ── ESPN ────────────────────────────────────────────────────────

def parse_event(event, game_date):
    teams  = {t['homeAway']: t for t in event['competitions'][0]['competitors']}
    status = event['status']
    return {
        'id':         event['id'],
        'game_date':  game_date,
        'away':       teams['away']['team']['abbreviation'],
        'home':       teams['home']['team']['abbreviation'],
        'away_name':  teams['away']['team']['displayName'],
        'home_name':  teams['home']['team']['displayName'],
        'away_score': int(teams['away'].get('score') or 0),
        'home_score': int(teams['home'].get('score') or 0),
        'state':      status['type']['state'],        # pre / in / post
        'status':     status['type']['name'],
        'period':     status.get('period', 0),
        'clock':      float(status.get('clock', 0.0)),  # seconds left in period
        'clock_str':  status.get('displayClock', ''),
        'tipoff':     datetime.fromisoformat(event['date'].replace('Z', '+00:00')),
    }


def get_games():
    """Yesterday's and today's games (ET), so games running past midnight stay visible."""
    today = datetime.now(ET).date()
    games = {}
    for day in (today - timedelta(days=1), today):
        resp = requests.get(f'{ESPN_API}/scoreboard', params={'dates': f'{day:%Y%m%d}'}, timeout=10)
        resp.raise_for_status()
        for event in resp.json().get('events', []):
            games[event['id']] = parse_event(event, day.isoformat())
    return games


def get_plays(game_id):
    resp = requests.get(f'{ESPN_API}/summary', params={'event': game_id}, timeout=15)
    resp.raise_for_status()
    return resp.json().get('plays', [])


def clock_to_minutes(display):
    """ESPN clock '5:32' or '42.1' -> minutes left."""
    try:
        mins, _, secs = display.rpartition(':')
        return int(mins or 0) + float(secs) / 60
    except (ValueError, AttributeError):
        return None


# ── PACE (same rules as pace_checkpoint.py, on ESPN plays) ──────

LAST_FT_PATTERNS = ['1 of 1', '2 of 2', '3 of 3', '2 of 3']


def ends_possession(play):
    ptype = play.get('type', {}).get('text', '').lower()
    desc  = play.get('text', '')
    if ptype.startswith('free throw'):
        return any(p in desc for p in LAST_FT_PATTERNS)
    if play.get('shootingPlay'):
        return bool(play.get('scoringPlay'))
    if 'rebound' in ptype:
        return 'offensive' not in ptype
    return 'turnover' in ptype or 'turnover' in desc.lower()


def pace_at_checkpoint(plays):
    """Q3 possessions from 12:00 down to the checkpoint, pace per minute, score there."""
    poss_count = 0
    score      = '--'
    for play in plays:
        if play.get('period', {}).get('number') != 3:
            continue
        mins_left = clock_to_minutes(play.get('clock', {}).get('displayValue', ''))
        if mins_left is not None and mins_left < CHECKPOINT_MINUTE:
            break
        if ends_possession(play):
            poss_count += 1
        score = f"{play.get('awayScore')}-{play.get('homeScore')}"
    return poss_count, round(poss_count / (12.0 - CHECKPOINT_MINUTE), 2), score


def pbp_past_checkpoint(plays):
    """True once the play-by-play feed has caught up beyond Q3 6:00."""
    for play in plays:
        period = play.get('period', {}).get('number', 0)
        mins   = clock_to_minutes(play.get('clock', {}).get('displayValue', ''))
        if period > 3 or (period == 3 and mins is not None and mins < CHECKPOINT_MINUTE):
            return True
    return False


# ── ODDS (Pinnacle via The Odds API) ────────────────────────────

def nickname(team_name):
    return team_name.split()[-1].lower()  # 'LA Clippers' and 'Los Angeles Clippers' -> 'clippers'


def pull_odds(game):
    """One Odds API pull: Pinnacle spread + total for this game, right now."""
    if not ODDS_KEY:
        return {'odds_status': 'no_key'}
    try:
        # The events list is free; only the odds call below uses credits (2: spreads + totals).
        events = requests.get(f'{ODDS_API}/events', params={'apiKey': ODDS_KEY}, timeout=10)
        events.raise_for_status()
        want  = (nickname(game['away_name']), nickname(game['home_name']))
        event = next((e for e in events.json()
                      if (nickname(e['away_team']), nickname(e['home_team'])) == want), None)
        if not event:
            return {'odds_status': 'no_event'}

        resp = requests.get(f"{ODDS_API}/events/{event['id']}/odds", timeout=10, params={
            'apiKey': ODDS_KEY, 'bookmakers': 'pinnacle',
            'markets': 'spreads,totals', 'oddsFormat': 'american',
        })
        resp.raise_for_status()
        row  = {'odds_credits_left': resp.headers.get('x-requests-remaining', '')}
        book = next((b for b in resp.json().get('bookmakers', []) if b['key'] == 'pinnacle'), None)
        if not book:
            return {**row, 'odds_status': 'no_pinnacle'}

        updated = []
        for market in book.get('markets', []):
            updated.append(market.get('last_update', ''))
            for o in market.get('outcomes', []):
                if market['key'] == 'spreads':
                    side = 'home' if o['name'] == event['home_team'] else 'away'
                    row[f'spread_{side}']       = o.get('point')
                    row[f'spread_{side}_price'] = o.get('price')
                elif market['key'] == 'totals':
                    row['total'] = o.get('point')
                    row['over_price' if o['name'] == 'Over' else 'under_price'] = o.get('price')
        return {**row, 'odds_status': 'ok', 'odds_updated_utc': max(updated, default='')}
    except Exception as e:
        return {'odds_status': f'error: {e}'[:120]}


# ── STORAGE ─────────────────────────────────────────────────────

def load_recorded():
    if not os.path.exists(DATA_FILE):
        return set()
    with open(DATA_FILE, newline='') as f:
        return {row['espn_id'] for row in csv.DictReader(f)}


def write_row(row):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    new_file = not os.path.exists(DATA_FILE)
    with open(DATA_FILE, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def commit(message):
    """Push each new row right away so a crashed or later run never re-pulls that game."""
    if os.environ.get('GIT_PUSH') != '1':
        return
    subprocess.run(['git', 'add', DATA_FILE], check=True)
    subprocess.run(['git', 'commit', '-q', '-m', message], check=True)
    subprocess.run(['git', 'pull', '-q', '--rebase'], check=False)
    subprocess.run(['git', 'push', '-q'], check=False)  # a failed push rides along with the next one


# ── MAIN LOOP ───────────────────────────────────────────────────

def past_checkpoint(game):
    return (game['state'] == 'post' or game['period'] > 3
            or (game['period'] == 3 and game['clock'] <= CHECKPOINT_MINUTE * 60))


def snapshot(game):
    """Capture the moment: live score now, plus the single odds pull if still on time."""
    on_time = game['state'] == 'in' and game['period'] == 3 and game['clock'] >= ODDS_CUTOFF_MIN * 60
    odds    = pull_odds(game) if on_time else {'odds_status': 'missed'}
    log(f"{game['away']} @ {game['home']}: captured Q{game['period']} {game['clock_str']} "
        f"{game['away_score']}-{game['home_score']}, odds {odds['odds_status']}")
    return {
        'game_date':      game['game_date'],
        'espn_id':        game['id'],
        'away':           game['away'],
        'home':           game['home'],
        'tipoff_utc':     game['tipoff'].strftime('%Y-%m-%dT%H:%MZ'),
        'captured_utc':   datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'capture_period': 'F' if game['state'] == 'post' else game['period'],
        'capture_clock':  '' if game['state'] == 'post' else game['clock_str'],
        'away_score':     game['away_score'],
        'home_score':     game['home_score'],
        'margin':         abs(game['home_score'] - game['away_score']),
        **odds,
    }


def finalize(snap, game, force=False):
    """Add pace once the play-by-play has caught up past 6:00 (ESPN's feed lags the clock)."""
    try:
        plays = get_plays(snap['espn_id'])
    except Exception as e:
        log(f"{snap['away']} @ {snap['home']}: play-by-play fetch failed ({e})")
        return None
    captured = datetime.strptime(snap['captured_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    waited   = datetime.now(timezone.utc) - captured
    done     = game is None or game['state'] == 'post'
    if not (force or done or pbp_past_checkpoint(plays) or waited > PBP_WAIT):
        return None
    poss, pace, score = pace_at_checkpoint(plays)
    margin = abs(int(score.split('-')[0]) - int(score.split('-')[1])) if score != '--' else ''
    return {**snap, 'q3_poss_to_6': poss, 'q3_pace_to_6': pace, 'score_at_6': score, 'margin_at_6': margin}


def watching(game, now):
    return game['state'] == 'in' or (game['state'] == 'pre' and game['tipoff'] - now <= LOOKAHEAD)


def save(row):
    write_row(row)
    log(f"{row['away']} @ {row['home']}: saved ({row['q3_poss_to_6']} poss, pace {row['q3_pace_to_6']})")
    commit(f"Q3 checkpoint: {row['away']} @ {row['home']} {row['game_date']}")


def run():
    started  = datetime.now(timezone.utc)
    recorded = load_recorded()
    pending  = {}

    while True:
        now = datetime.now(timezone.utc)
        try:
            games = get_games()
        except Exception as e:
            log(f'scoreboard fetch failed ({e})')
            games = None

        if games is not None:
            for game in games.values():
                gid = game['id']
                if gid in recorded or gid in pending or game['status'] in SKIP_STATUSES:
                    continue
                if past_checkpoint(game):
                    pending[gid] = snapshot(game)

            for gid in list(pending):
                row = finalize(pending[gid], games.get(gid))
                if row:
                    save(row)
                    recorded.add(gid)
                    del pending[gid]

            waiting = [g for g in games.values()
                       if g['id'] not in recorded and g['status'] not in SKIP_STATUSES and watching(g, now)]
            if not pending and not waiting:
                log('Nothing live or tipping off soon. Exiting.')
                break

        if now - started > MAX_RUNTIME:
            log('Max runtime reached. Saving pending games and exiting.')
            break
        time.sleep(POLL_SECONDS)

    for gid, snap in pending.items():
        row = finalize(snap, None, force=True)
        if row:
            save(row)


if __name__ == '__main__':
    run()
