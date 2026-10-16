# Whispers (PsstRobot port)

Source: PsstRobot (ALiwoto, Go) - see [NOTICE.md](../NOTICE.md). Code: `bot/plugins/whisper.py`, `bot/utils/whisper_text.py`, `bot/utils/whisper_store.py`, `bot/core/inline.py`.

## BotFather setup (required)

`/setinline` (enable inline mode) **and** `/setinlinefeedback` -> `100%`. Without feedback Telegram never tells the bot which inline result was sent, and whispers stay "Generating whisper message...". The Bot API cannot report this setting, so the bot cannot warn you.

## Using it

* Inline, in any chat: `@bot @alice meet at 8` (also `@bot meet at 8 @alice`, `@bot 123456789 meet at 8`). With no recipient it is a whisper to anyone - the **first** person to open it consumes it. After the first reads, "X read the whisper" replaces the message.
* Opening: the recipient taps 🔐 and the text appears as a pop-up alert. Text over 199 characters and every media whisper open in the bot's PM instead (the button redirects).
* `/whisper` (`/create`) in PM: choose a target (`@username`, id, text-mention, or `everyone`), then send text of any length, one media file, or an album; a "share whisper" button then inserts it inline. An advanced whisper can be sent once.
* `/list` pending whispers with delete buttons · `/privacy on|off` · `/notifications` (DM when someone reads your whisper) · `/cancel` · `/whisperhelp`. Sudo: `/whisperban`, `/whisperunban` (users banned with `/gban` or `/block` cannot whisper either).
* Recent recipients: the last `WHISPER_HISTORY_SIZE` people who read your whispers are offered as one-tap results for "anyone" whispers (switch off in the help panel).

## Inline sharing with the collector

Both features answer `@bot ...`. `bot/core/inline.py` is the single `InlineQueryHandler`; plugins register providers. Queries with a recipient, an advanced token, or over 253 characters are whisper-only; plain text shows character cards first and the whisper-to-anyone option last; an empty query shows characters plus a "Send a whisper" pill.

## Changes from PsstRobot

| PsstRobot | Here |
|---|---|
| 10 sharded SQLite files + an in-memory copy | one table in the bot database; a read is an atomic `DELETE` claim, so two readers cannot both get a one-shot whisper; a failed alert restores it |
| ids `base32=base32` (same sender, same second collides; `=` is not a legal deep-link character) | 12-char URL-safe random ids, payload `w_<id>` |
| a trailing number in any text became a recipient id ("see you at 5") | ids must be >= 10000 |
| advanced whisper marked "sent" as soon as results were shown, even if never chosen; fall-through into the plain flow | marked when chosen; a second send is refused |
| photo whispers stored the smallest size; dice sent from the wrong field; albums assumed one type, joined by separator characters | largest size; dice emoji kept; albums stored as JSON with per-item types |
| nil dereference when the sender could not be looked up | handled |
| privacy-mode readers in PM did not consume the whisper, but did via the button | same rule everywhere: any non-sender read consumes; privacy only hides the announcement/history |
| `/list`, `/notifications`, the Privacy Policy button advertised but not implemented | implemented |
| `/help`, `/start` | `/whisperhelp`; `/start w_<id>`, `/start create`, `/start help-inline` are routed by the main `/start` |
| author's support/channel buttons | optional `WHISPER_SUPPORT_URL` |
| `max_expiry`, `interval_check`, `single_db`, `dev_users` (config.ini) | `WHISPER_MAX_EXPIRY_DAYS`, `WHISPER_CLEANUP_MINUTES`; sharding dropped; `dev_users` -> `OWNER_ID`/`ADMIN_IDS` |

Privacy: content is stored until read, deleted by its sender, or `WHISPER_MAX_EXPIRY_DAYS` pass. It is not encrypted at rest; anyone with database access can read pending whispers. Telegram clients cache the callback answer for ~40 minutes so a privacy-mode reader can re-open it.
