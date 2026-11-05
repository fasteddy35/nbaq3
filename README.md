# NBA Q3 6:00 checkpoint collector

For every NBA game, the first time the 3rd-quarter clock hits **6:00**, this records:

- the live score and margin
- **Polymarket's main live spread and total** (with prices), with up to 5 attempts per game
- Q3 possessions and pace from 12:00 to 6:00, using the same rules as `pace_checkpoint.py`
- once the game ends: the score at the end of Q4, the final score, and whether it went to OT

It runs on GitHub Actions and adds each game as a row in [`data/q3_checkpoints.csv`](data/q3_checkpoints.csv).

## Setup (one time)

Odds come from Polymarket's public APIs, so no API key or secret is needed.

1. Create a **public** repo on GitHub (Actions minutes are free for public repos), then push this folder:
   ```
   git remote add origin https://github.com/<you>/nba-q3-collector.git
   git push -u origin main
   ```
2. On the **Actions** tab, open *Q3 checkpoint collector* and click **Run workflow** once to confirm it works.

## How it runs

- A run starts every hour. It exits within seconds unless a game is live or tips off within 90 minutes. Otherwise it polls ESPN's scoreboard every 15 seconds.
- When a game reaches Q3 6:00, it pulls the odds. If the pull fails, it tries again 5, 10, 20 and 20 seconds later (5 attempts in all), without holding up other games. It then waits for ESPN's play-by-play to catch up past 6:00, computes pace, and commits the row straight away.
- The run stays up until each saved game is final, then fills in `score_end_q4`, `final_score` and `ot`. Rows still missing a final are filled in by the next run.
- Each run first resets to the latest commit on GitHub, and merges in any rows pushed since before every push. A run that sat queued behind another one used to start from a stale checkout, hit a CSV conflict, and lose every row it captured.
- A game already in the CSV is never pulled again. Only one run can be active at a time.
- If a game is first seen with under 4:00 left in Q3, or after it ends (for example, a delayed GitHub run), the row is still saved with pace and the score at 6:00, but `odds_status` is `missed` and no odds are pulled.

## How the Polymarket line is chosen

Polymarket lists a ladder of alternate spreads and totals for each game instead of one main line. At capture, the collector finds the game's event on the Gamma API by team nicknames and tip-off time. It reads the live best bid and ask for every spread and total from the CLOB order books, since Gamma's cached prices lag by several cents during games. The main line is the one whose midpoint is closest to 50/50, among books no wider than 10 cents when any qualify.

## Columns

| Column | Meaning |
|---|---|
| `capture_period`, `capture_clock` | Game clock when the snapshot was taken; `F` means the game was already final |
| `away_score`, `home_score`, `margin` | Live score at capture; the final score for `F` rows |
| `q3_poss_to_6`, `q3_pace_to_6` | Q3 possessions from 12:00 to 6:00, and that count ÷ 6 minutes |
| `score_at_6`, `margin_at_6` | Score at the last play before 6:00, from play-by-play |
| `spread_home`, `spread_away` (+ `_price`) | Live full-game main spread. Prices are American odds to buy each side at the best ask |
| `total`, `over_price`, `under_price` | Live full-game main total, priced the same way |
| `spread_home_mid`, `over_mid` | Bid-ask midpoint (0 to 1) for the home spread side and the over: Polymarket's implied probability |
| `odds_source` | `polymarket`, or `pinnacle` for rows captured before the switch |
| `polymarket_slug` | The Polymarket event, e.g. `nba-cle-okc-2026-11-04` |
| `odds_status` | `ok`, `partial` (only one of spread/total found), `missed`, `no_event`, `no_lines`, or `error: ...`. Older Pinnacle rows may show `no_pinnacle` or `no_key` |
| `odds_updated_utc` | Order book timestamp of the prices |
| `odds_attempts` | How many pulls it took (1 to 5); blank when `missed` |
| `odds_clock` | Game clock when the odds were secured, e.g. `5:41` left in Q3 (`Q4 ...` if past Q3) |
| `odds_credits_left` | Odds API credits left after the pull. Pinnacle rows only, blank for Polymarket |
| `score_end_q4` | Score (away-home) at the end of regulation |
| `ot` | `yes` if the game went to overtime, otherwise `no` |
| `final_score` | Final score (away-home), including OT |

GitHub pauses scheduled workflows after 60 days with no commits, so after the off-season, re-enable it on the Actions tab.
