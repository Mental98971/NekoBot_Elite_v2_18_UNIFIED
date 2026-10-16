# Activity rankings (ChatFight port)

Source: ChatFight (Aadish Ranjan) - see [NOTICE.md](../NOTICE.md) for the licence situation. Code: `bot/plugins/chatfight.py`.

## Commands

| Command | Where | What |
|---|---|---|
| `/rankings` (`/ranking`) | groups | Leaderboard image of the top 10 + caption; buttons switch Overall / Today / Week |
| `/topusers` | groups | Top 10 members by total messages |
| `/userstats` | groups | A member's message count: reply to them, or pass `@username` / numeric id |
| `/mytop` | anywhere | The 5 groups where you chat most (Overall / Today / Week buttons) |
| `/sysstats` (`/system`) | anywhere | Host card: bot uptime, host uptime, RAM, CPU, disk (ChatFight's `/ping`) |
| `/fightinfo` (`/chatfight`) | anywhere | ChatFight's welcome card (ChatFight's `/start`) |
| `/update`, `/restart` | sudo (`OWNER_ID`, `ADMIN_IDS`) | `git pull --ff-only` then restart / restart |

Name changes forced by collisions: ChatFight's `/ping` -> `/sysstats` (`/ping` already probes AniList/Jikan), its `/start` -> `/fightinfo` (`/start` is the main menu).

## How counting works

* Every non-command, non-service message in a group adds 1 to an in-memory counter (bots are skipped unless `ACTIVITY_COUNT_BOTS=true`).
* A job writes the counters in one transaction every `ACTIVITY_FLUSH_INTERVAL` seconds, and `post_shutdown` writes the rest. A failed write keeps the counts.
* Day / week: each row carries the key of the period its counters belong to (`YYYY-MM-DD`, ISO `YYYY-Www`, in `ACTIVITY_TIMEZONE`). Readers treat an old key as 0; the next write resets it in the same UPDATE. No midnight job exists to miss.
* All reads add the not-yet-written counts, so boards are never a flush interval behind.
* Flood guard (kept from ChatFight): `ACTIVITY_SPAM_THRESHOLD` messages in `ACTIVITY_SPAM_WINDOW` s stops that member counting for `ACTIVITY_SPAM_BLOCK_SECONDS`. It only affects counting; the antiflood middleware still handles moderation. `0` disables it.
* Handler group 14, after all moderation/lock/filter listeners.

## Changes from ChatFight

| ChatFight | Here |
|---|---|
| MongoDB (`MONGO_URL`) | the bot's SQLAlchemy database (`activity_stats`; SQLite or Postgres). Existing Mongo data is not migrated. |
| Daily/weekly counters reset by a job (`update_many`) | lazy rollover (above) - correct even if the bot was down at midnight |
| `%Y-%W` week key | ISO year-week |
| Leaderboard captions ignored unflushed counts; "Total messages" summed only the top 10 | pending counts merged into every board; the total is the chat's real total |
| Names put into HTML unescaped | escaped |
| Unflushed counts lost on a failed write or restart | kept / flushed on shutdown |
| `noop` button had no handler (spinner) | handled (`cf:noop`) |
| Pillow in the event loop; font loaded at import (crash if missing) | worker thread; lazy font with fallback |
| `os.execv` straight from the handler | graceful: flush, `stop_running()`, re-exec from `__main__` after shutdown hooks |
| `/update` embedded `GIT_TOKEN` in the URL; plain `git pull` | token only in `GIT_CONFIG_*` env; `--ff-only`; refuses without a `.git` directory |
| In-memory spam state grew forever | pruned each flush |
| `debug.py` (`DEBUG_UPDATES`) | not carried: it imported a setting that did not exist and was never registered; `logging_middleware` covers it |
| `resource/leaderboard.png` file writer, empty `permissions.py` | not carried (unused) |
| "Developer" button to the original author, README promo | not carried (see NOTICE) |

Settings: `ACTIVITY_*`, `UPSTREAM_REPO`, `UPSTREAM_BRANCH`, `GIT_TOKEN` (see `.env.example`). Group privacy mode must be off (or the bot an admin) for Telegram to deliver ordinary group messages.
