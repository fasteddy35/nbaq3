"""Q3 6:00 checkpoint collector.

The first time a game's 3rd-quarter clock reaches 6:00, this records the live
score, FanDuel's live spread and total (up to 5 Odds API attempts per game),
and Q3 possessions/pace from 12:00 down to 6:00 (a.py's formula). Once the
game ends, the row also gets the score at the end of Q4, the final score and OT.

Rows go to data/q3_checkpoints.csv. That file is also the record of which
games are done, so a game is never pulled twice, even across runs.
"""
import csv
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ── CONFIG ──────────────────────────────────────────────────────
CHECKPOINT_MINUTE = 6.0                          # Q3 clock to snapshot at (6:00 left)
ODDS_CUTOFF_MIN   = 4.0                          # first seen below 4:00 -> too late, skip odds
ODDS_RETRY_DELAYS = [5, 10, 20, 20]              # seconds to wait before odds attempts 2-5
POLL_SECONDS      = 15
LOOKAHEAD         = timedelta(minutes=90)        # stay running if a game tips within this
MAX_RUNTIME       = timedelta(hours=5, minutes=40)  # GitHub kills jobs at 6h
PBP_WAIT          = timedelta(minutes=10)        # max wait for play-by-play to pass 6:00
DATA_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'q3_checkpoints.csv')
ESPN_API          = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
ODDS_API          = 'https://api.the-odds-api.com/v4/sports/basketball_nba'
ODDS_KEY          = os.environ.get('ODDS_API_KEY', '')
BOOKMAKER         = 'fanduel'                    # Odds API bookmaker key
ET                = ZoneInfo('America/New_York')
SKIP_STATUSES     = {'STATUS_POSTPONED', 'STATUS_CANCELED', 'STATUS_SUSPENDED'}
# ────────────────────────────────────────────────────────────────

FIELDS = [
    'game_date', 'espn_id', 'away', 'home', 'tipoff_utc',
    'captured_utc', 'capture_period', 'capture_clock', 'away_score', 'home_score', 'margin',
    'q3_poss_to_6', 'q3_pace_to_6', 'pace_formula', 'score_at_6', 'margin_at_6',
    'odds_status', 'odds_updated_utc',
    'spread_home', 'spread_home_price', 'spread_away', 'spread_away_price',
    'total', 'over_price', 'under_price', 'odds_credits_left',
    'odds_attempts', 'odds_clock',
    'odds_source', 'polymarket_slug', 'spread_home_mid', 'over_mid',
    'score_end_q4', 'ot', 'final_score',
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
        'away_lines': [int(q.get('value') or 0) for q in teams['away'].get('linescores', [])],
        'home_lines': [int(q.get('value') or 0) for q in teams['home'].get('linescores', [])],
        'state':      status['type']['state'],        # pre / in / post
        'status':     status['type']['name'],
        'period':     status.get('period', 0),
        'clock':      float(status.get('clock', 0.0)),  # seconds left in period
        'clock_str':  status.get('displayClock', ''),
        'tipoff':     datetime.fromisoformat(event['date'].replace('Z', '+00:00')),
    }


def get_scoreboard(day):
    """All games on one ET date, keyed by ESPN id."""
    resp = requests.get(f'{ESPN_API}/scoreboard', params={'dates': f'{day:%Y%m%d}'}, timeout=10)
    resp.raise_for_status()
    return {e['id']: parse_event(e, day.isoformat()) for e in resp.json().get('events', [])}


def get_games():
    """Yesterday's and today's games (ET), so games running past midnight stay visible."""
    today = datetime.now(ET).date()
    return {**get_scoreboard(today - timedelta(days=1)), **get_scoreboard(today)}


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


# ── PACE (same formula as a.py, on ESPN plays) ──────────────────
#
# Possessions = FGA + free throw trips (2 or 3 shots) + turnovers, both teams combined.
# - A 2- or 3-shot trip counts once, on its first free throw ('1 of 2' / '1 of 3').
# - One-shot trips ('1 of 1': and-ones, technicals) don't count.
# - Rebounds never count. After an offensive rebound off a missed shot or a missed final
#   free throw, the next FGA, trip or turnover is the same possession (a putback) only if
#   it comes within PUTBACK_SECONDS of game clock. Later ones count as a new possession.
#   ESPN also logs an 'offensive rebound' after a missed first free throw of a trip; that
#   is a dead-ball team rebound and changes nothing.
# Rows saved before 2026-11-11 used the old rebound-based rules (pace_formula = 'rebounds').

PACE_FORMULA     = 'fga_trips_tov'
PUTBACK_SECONDS  = 3
TRIP_START_FTS   = ['1 of 2', '1 of 3']
FINAL_FTS        = ['1 of 1', '2 of 2', '3 of 3']


def pace_at_checkpoint(plays):
    """Q3 possessions from 12:00 down to the checkpoint, pace per minute, score there."""
    poss_count = 0
    score      = '--'
    last_shot  = None        # (is_ft, desc, missed) of the most recent FGA or free throw
    putback_by = None        # minutes-left a putback must come at or before
    for play in plays:
        if play.get('period', {}).get('number') != 3:
            continue
        mins_left = clock_to_minutes(play.get('clock', {}).get('displayValue', ''))
        if mins_left is not None and mins_left < CHECKPOINT_MINUTE:
            break
        score = f"{play.get('awayScore')}-{play.get('homeScore')}"

        ptype  = play.get('type', {}).get('text', '').lower()
        desc   = play.get('text', '')
        is_ft  = ptype.startswith('free throw')
        is_fga = bool(play.get('shootingPlay')) and not is_ft
        missed = not play.get('scoringPlay')

        if 'rebound' in ptype:
            if 'offensive' in ptype and last_shot and mins_left is not None:
                shot_is_ft, shot_desc, shot_missed = last_shot
                if shot_missed and (not shot_is_ft or any(p in shot_desc for p in FINAL_FTS)):
                    putback_by = mins_left - PUTBACK_SECONDS / 60
            continue
        if is_fga or is_ft:
            last_shot = (is_ft, desc, missed)

        turnover = 'turnover' in ptype or 'turnover' in desc.lower()
        if is_fga or turnover or (is_ft and any(p in desc for p in TRIP_START_FTS)):
            window, putback_by = putback_by, None     # only the first play after the rebound
            if window is not None and mins_left is not None and mins_left >= window - 1e-9:
                continue                               # putback: same possession
            poss_count += 1
    return poss_count, round(poss_count / (12.0 - CHECKPOINT_MINUTE), 2), score


def pbp_past_checkpoint(plays):
    """True once the play-by-play feed has caught up beyond Q3 6:00."""
    for play in plays:
        period = play.get('period', {}).get('number', 0)
        mins   = clock_to_minutes(play.get('clock', {}).get('displayValue', ''))
        if period > 3 or (period == 3 and mins is not None and mins < CHECKPOINT_MINUTE):
            return True
    return False


# ── ODDS (FanDuel via The Odds API) ─────────────────────────────

def nickname(team_name):
    return team_name.split()[-1].lower()  # 'LA Clippers' and 'Los Angeles Clippers' -> 'clippers'


def pull_odds(game):
    """One Odds API pull: FanDuel's live spread + total for this game, right now."""
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
            'apiKey': ODDS_KEY, 'bookmakers': BOOKMAKER,
            'markets': 'spreads,totals', 'oddsFormat': 'american',
        })
        resp.raise_for_status()
        row  = {'odds_credits_left': resp.headers.get('x-requests-remaining', '')}
        book = next((b for b in resp.json().get('bookmakers', []) if b['key'] == BOOKMAKER), None)
        if not book:
            return {**row, 'odds_status': f'no_{BOOKMAKER}'}

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
        has_spread, has_total = 'spread_home' in row, 'total' in row
        if not (has_spread or has_total):
            return {**row, 'odds_status': 'no_lines'}
        return {**row, 'odds_status': 'ok' if has_spread and has_total else 'partial',
                'odds_source': BOOKMAKER, 'odds_updated_utc': max(updated, default='')}
    except Exception as e:
        return {'odds_status': f'error: {e}'[:120]}


def game_clock(game):
    """The game clock right now: '5:32' while in Q3, otherwise e.g. 'Q4 11:48'."""
    try:
        now = get_scoreboard(date.fromisoformat(game['game_date']))[game['id']]
    except Exception:
        return ''
    return now['clock_str'] if now['period'] == 3 else f"Q{now['period']} {now['clock_str']}"


def pull_odds_with_retries(game):
    """Runs in a worker thread, so retry waits don't hold up other games."""
    for attempt in range(1, len(ODDS_RETRY_DELAYS) + 2):
        if attempt > 1:
            time.sleep(ODDS_RETRY_DELAYS[attempt - 2])
        odds = pull_odds(game)
        if odds['odds_status'] in ('ok', 'no_key'):
            break
        log(f"{game['away']} @ {game['home']}: odds attempt {attempt} failed ({odds['odds_status']})")
    odds['odds_attempts'] = attempt
    if odds['odds_status'] in ('ok', 'no_key'):
        odds['odds_clock'] = game_clock(game)
    log(f"{game['away']} @ {game['home']}: odds {odds['odds_status']} on attempt {attempt}"
        f"{' at ' + odds['odds_clock'] if odds.get('odds_clock') else ''}")
    return odds


# ── STORAGE ─────────────────────────────────────────────────────

def read_rows():
    """CSV -> {espn_id: row}. Columns added after a row was written come back blank."""
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, newline='') as f:
        rows = {r['espn_id']: {k: r.get(k) or '' for k in FIELDS} for r in csv.DictReader(f)}
    for row in rows.values():  # rows from before odds_source existed were all Pinnacle
        if row['odds_status'] == 'ok' and not row['odds_source']:
            row['odds_source'] = 'pinnacle'
        if row['q3_poss_to_6'] != '' and not row['pace_formula']:  # saved before 2026-11-11
            row['pace_formula'] = 'rebounds'
    return rows


def write_rows(rows):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows.values())


def git(*args, check=True):
    return subprocess.run(['git', *args], check=check, capture_output=True, text=True)


def pull_latest():
    """Reset to the remote branch tip.

    The checkout is the commit that triggered the run, which is hours old when the run sat
    queued behind another one. Building on it made the CSV conflict on rebase, and every
    push for the rest of that run was silently dropped.
    """
    if os.environ.get('GIT_PUSH') != '1':
        return
    branch = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    git('fetch', '-q', '--depth=1', 'origin', branch)
    git('reset', '-q', '--hard', 'FETCH_HEAD')


def commit(rows, message):
    """Push each change right away so a crashed or later run never re-pulls that game."""
    write_rows(rows)
    if os.environ.get('GIT_PUSH') != '1':
        return
    for _ in range(3):
        pull_latest()
        for gid, row in read_rows().items():  # keep rows pushed since; ours win on the same game
            rows.setdefault(gid, row)
        write_rows(rows)
        git('add', DATA_FILE)
        if git('diff', '--cached', '--quiet', check=False).returncode == 0:
            return
        git('commit', '-q', '-m', message)
        if git('push', '-q', 'origin', 'HEAD', check=False).returncode == 0:
            return
    log(f'push failed 3 times, will go out with the next save ({message})')


def fill_finals(rows, games):
    """Add Q4-end score, final score and OT to rows whose game has ended. Returns the ids filled."""
    filled = []
    for gid, row in rows.items():
        game = games.get(gid)
        if row['final_score'] or not game or game['state'] != 'post' or len(game['home_lines']) < 4:
            continue
        row['score_end_q4'] = f"{sum(game['away_lines'][:4])}-{sum(game['home_lines'][:4])}"
        row['final_score']  = f"{game['away_score']}-{game['home_score']}"
        row['ot']           = 'yes' if len(game['home_lines']) > 4 else 'no'
        filled.append(gid)
    return filled


def backfill_finals(rows):
    """Finals for rows older than get_games()' two-day window."""
    games = {}
    for day in {r['game_date'] for r in rows.values() if not r['final_score']}:
        try:
            games.update(get_scoreboard(date.fromisoformat(day)))
        except Exception as e:
            log(f'scoreboard fetch for {day} failed ({e})')
    return fill_finals(rows, games)


# ── MAIN LOOP ───────────────────────────────────────────────────

def past_checkpoint(game):
    return (game['state'] == 'post' or game['period'] > 3
            or (game['period'] == 3 and game['clock'] <= CHECKPOINT_MINUTE * 60))


def snapshot(game, pool):
    """Capture the moment: live score now, and start the odds pull if still on time."""
    on_time = game['state'] == 'in' and game['period'] == 3 and game['clock'] >= ODDS_CUTOFF_MIN * 60
    odds    = pool.submit(pull_odds_with_retries, game) if on_time else None
    log(f"{game['away']} @ {game['home']}: captured Q{game['period']} {game['clock_str']} "
        f"{game['away_score']}-{game['home_score']}{'' if on_time else ', odds missed'}")
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
        'odds':           odds,  # Future, or None when too late
    }


def finalize(snap, game, force=False):
    """Add odds and pace once both are in (ESPN's play-by-play lags the clock)."""
    future = snap['odds']
    if future and not future.done() and not force:
        return None
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
    odds   = future.result() if future else {'odds_status': 'missed'}
    snap   = {k: v for k, v in snap.items() if k != 'odds'}
    return {**dict.fromkeys(FIELDS, ''), **snap, **odds,
            'q3_poss_to_6': poss, 'q3_pace_to_6': pace, 'pace_formula': PACE_FORMULA, 'score_at_6': score, 'margin_at_6': margin}


def watching(game, now):
    return game['state'] == 'in' or (game['state'] == 'pre' and game['tipoff'] - now <= LOOKAHEAD)


def save(rows, row):
    rows[row['espn_id']] = row
    log(f"{row['away']} @ {row['home']}: saved ({row['q3_poss_to_6']} poss, pace {row['q3_pace_to_6']})")
    commit(rows, f"Q3 checkpoint: {row['away']} @ {row['home']} {row['game_date']}")


def save_finals(rows, filled):
    if filled:
        names = ', '.join(f"{rows[gid]['away']} @ {rows[gid]['home']}" for gid in filled)
        log(f'finals: {names}')
        commit(rows, f'Finals: {names}')


def run():
    started = datetime.now(timezone.utc)
    pull_latest()
    rows    = read_rows()
    pending = {}
    pool    = ThreadPoolExecutor(max_workers=8)
    save_finals(rows, backfill_finals(rows))

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
                if gid in rows or gid in pending or game['status'] in SKIP_STATUSES:
                    continue
                if past_checkpoint(game):
                    pending[gid] = snapshot(game, pool)

            for gid in list(pending):
                row = finalize(pending[gid], games.get(gid))
                if row:
                    save(rows, row)
                    del pending[gid]

            save_finals(rows, fill_finals(rows, games))

            # Keep running for games yet to reach 6:00 and for saved games still waiting on a final.
            waiting = [g for g in games.values()
                       if g['status'] not in SKIP_STATUSES and watching(g, now)
                       and not rows.get(g['id'], {}).get('final_score')]
            if not pending and not waiting:
                log('Nothing live or tipping off soon. Exiting.')
                break

        if now - started > MAX_RUNTIME:
            log('Max runtime reached. Saving pending games and exiting.')
            break
        time.sleep(POLL_SECONDS)

    for snap in pending.values():
        row = finalize(snap, None, force=True)
        if row:
            save(rows, row)
    pool.shutdown(wait=False)


if __name__ == '__main__':
    run()
