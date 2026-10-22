# NBA Q3 6:00 checkpoint collector

For every NBA game, the first time the 3rd-quarter clock hits **6:00**, this records:

- the live score and margin
- **Pinnacle's live spread and total** (with prices), from one Odds API pull per game
- Q3 possessions and pace from 12:00 to 6:00, using the same rules as `pace_checkpoint.py`

It runs on GitHub Actions and adds each game as a row in [`data/q3_checkpoints.csv`](data/q3_checkpoints.csv).

## Setup (one time)

1. Get an API key at [the-odds-api.com](https://the-odds-api.com). Each game uses 2 credits (spreads + totals), about 2,500 for a full season, so the free 500/month tier won't last. The $30/month 20K plan covers it.
2. Create a **public** repo on GitHub (Actions minutes are free for public repos), then push this folder:
   ```
   git remote add origin https://github.com/<you>/nba-q3-collector.git
   git push -u origin main
   ```
3. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `ODDS_API_KEY` and paste the key.
4. On the **Actions** tab, open *Q3 checkpoint collector* and click **Run workflow** once to confirm it works.

## How it runs

- A run starts every hour. It exits within seconds unless a game is live or tips off within 90 minutes. Otherwise it polls ESPN's scoreboard every 15 seconds.
- When a game reaches Q3 6:00, it pulls the odds once, waits for ESPN's play-by-play to catch up past 6:00, computes pace, and commits the row straight away.
- A game already in the CSV is never pulled again. Only one run can be active at a time.
- If a game is first seen with under 4:00 left in Q3, or after it ends (for example, a delayed GitHub run), the row is still saved with pace and the score at 6:00, but `odds_status` is `missed` and no odds are pulled.

## Columns

| Column | Meaning |
|---|---|
| `capture_period`, `capture_clock` | Game clock when the snapshot was taken; `F` means the game was already final |
| `away_score`, `home_score`, `margin` | Live score at capture; the final score for `F` rows |
| `q3_poss_to_6`, `q3_pace_to_6` | Q3 possessions from 12:00 to 6:00, and that count ÷ 6 minutes |
| `score_at_6`, `margin_at_6` | Score at the last play before 6:00, from play-by-play |
| `spread_home`, `spread_away` (+ `_price`) | Pinnacle live full-game spread, American odds |
| `total`, `over_price`, `under_price` | Pinnacle live full-game total |
| `odds_status` | `ok`, `missed`, `no_pinnacle`, `no_event`, `no_key`, or `error: ...` |
| `odds_updated_utc` | When The Odds API last saw that Pinnacle line |
| `odds_credits_left` | Odds API credits remaining after the pull |

GitHub pauses scheduled workflows after 60 days with no commits, so after the off-season, re-enable it on the Actions tab.
