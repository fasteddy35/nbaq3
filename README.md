# NBA Q3 6:00 checkpoint collector

For every NBA game, the first time the 3rd-quarter clock hits **6:00**, this records:

- the live score and margin
- **FanDuel's live spread and total** (with prices), via The Odds API, with up to 5 attempts per game
- Q3 possessions and pace from 12:00 to 6:00, using the same formula as `a.py`
- once the game ends: the score at the end of Q4, the final score, and whether it went to OT

It runs on GitHub Actions and adds each game as a row in [`data/q3_checkpoints.csv`](data/q3_checkpoints.csv).

## Setup (one time)

1. Get an API key at [the-odds-api.com](https://the-odds-api.com). Each game uses 2 credits per attempt (spreads + totals), about 2,500 for a full season when the first attempt works, so the free 500/month tier won't last. The $30/month 20K plan covers it.
2. Create a **public** repo on GitHub (Actions minutes are free for public repos), then push this folder:
   ```
   git remote add origin https://github.com/<you>/nba-q3-collector.git
   git push -u origin main
   ```
3. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `ODDS_API_KEY` and paste the key.
4. On the **Actions** tab, open *Q3 checkpoint collector* and click **Run workflow** once to confirm it works.

## How it runs

- A run starts every hour. It exits within seconds unless a game is live or tips off within 90 minutes. Otherwise it polls ESPN's scoreboard every 15 seconds.
- When a game reaches Q3 6:00, it pulls the odds. If the pull fails, it tries again 5, 10, 20 and 20 seconds later (5 attempts in all), without holding up other games. It then waits for ESPN's play-by-play to catch up past 6:00, computes pace, and commits the row straight away.
- The run stays up until each saved game is final, then fills in `score_end_q4`, `final_score` and `ot`. Rows still missing a final are filled in by the next run.
- Each run first resets to the latest commit on GitHub, and merges in any rows pushed since before every push. A run that sat queued behind another one used to start from a stale checkout, hit a CSV conflict, and lose every row it captured.
- A game already in the CSV is never pulled again. Only one run can be active at a time.
- If a game is first seen with under 4:00 left in Q3, or after it ends (for example, a delayed GitHub run), the row is still saved with pace and the score at 6:00, but `odds_status` is `missed` and no odds are pulled.

## Columns

| Column | Meaning |
|---|---|
| `capture_period`, `capture_clock` | Game clock when the snapshot was taken; `F` means the game was already final |
| `away_score`, `home_score`, `margin` | Live score at capture; the final score for `F` rows |
| `q3_poss_to_6`, `q3_pace_to_6` | Q3 possessions from 12:00 to 6:00 (both teams), and that count ÷ 6 minutes |
| `pace_formula` | How possessions were counted. `fga_trips_tov` (from 2026-11-11): FGA + 2- or 3-shot free throw trips + turnovers, no rebounds, and a shot, trip or turnover within 3 seconds after a live offensive rebound is the same possession. `rebounds` (earlier rows): made shots, last free throws, defensive rebounds and turnovers. The two are not comparable |
| `score_at_6`, `margin_at_6` | Score at the last play before 6:00, from play-by-play |
| `spread_home`, `spread_away` (+ `_price`) | FanDuel live full-game spread, American odds |
| `total`, `over_price`, `under_price` | FanDuel live full-game total |
| `polymarket_slug`, `spread_home_mid`, `over_mid` | Polymarket rows only (2026-11-04 to 11-08): event slug and bid-ask midpoints. Blank otherwise |
| `odds_source` | `fanduel` from 2026-11-09. Earlier rows are `pinnacle` (to 2026-11-04) or `polymarket` (2026-11-04 to 11-08) |
| `odds_status` | `ok`, `partial` (only one of spread/total found), `missed`, `no_fanduel`, `no_event`, `no_lines`, `no_key`, or `error: ...`. Older rows may show `no_pinnacle` |
| `odds_updated_utc` | When The Odds API last saw that FanDuel line |
| `odds_attempts` | How many pulls it took (1 to 5); blank when `missed` |
| `odds_clock` | Game clock when the odds were secured, e.g. `5:41` left in Q3 (`Q4 ...` if past Q3) |
| `odds_credits_left` | Odds API credits left after the pull. Blank for Polymarket rows |
| `score_end_q4` | Score (away-home) at the end of regulation |
| `ot` | `yes` if the game went to overtime, otherwise `no` |
| `final_score` | Final score (away-home), including OT |

GitHub pauses scheduled workflows after 60 days with no commits, so after the off-season, re-enable it on the Actions tab.
