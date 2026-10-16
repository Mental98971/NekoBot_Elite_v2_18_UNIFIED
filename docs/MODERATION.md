# Moderation guide (v2.12)

Everything below works in a group. Commands marked **DM** also work from a private chat after `/connect` (see the last section).

## Who may do what

The bot checks the Telegram admin right of the person typing the command *and* of the bot itself, and says which right is missing.

| Right (Telegram admin setting) | Commands |
|---|---|
| Ban users | `/ban /tban /sban /dban /dtban` · `/mute /tmute /smute /dmute /dtmute` · `/kick /skick /dkick` · `/unban /unmute` · `/warn /swarn /dwarn /rmwarn /resetwarns` · `/approve /unapprove` · `/zombies /rmzombies /unbanall` |
| Delete messages | `/purge /spurge /del` and the `d…` variants (they also delete the replied message) |
| Pin messages | `/pin /unpin /unpinall` |
| Add new admins | `/promote /fullpromote /demote /title` (nobody can grant a right they do not hold) |
| Change group info | `/setgtitle /setdesc` · `/setflood /setfloodmode /slowmode /antispam /reports /unsetlog /allowconnect` · blacklist edits · `/privaterules /cleanwelcome /cleanservice` |
| Invite users | `/invitelink` |
| any admin | locks, filters, notes, rules, welcome, captcha, night mode, `/setwarnlimit /setwarnaction /setlog /disable /enable`, read-only lists (`/modsettings /blacklist /approved /filters` ...) |

Never targetable: admins (demote first), the chat owner, the bot, bot owners, anonymous-admin and channel identities. Anonymous admins can use every command except `/promote /fullpromote /demote /title` (their individual admin rights cannot be verified).

## Targets and times

`/ban` and friends take, in this order: the replied-to user, a numeric user ID, a tapped mention, or an `@username` the bot has seen before. Then an optional time and a reason:
`/ban 12345 2h spamming links`. Times: `30s 15m 2h 1d 1w` - between **30 s and 366 d** (Telegram treats anything outside that as permanent, so the bot refuses it). `/tban` and `/tmute` require a time.

Variants: `s…` removes your command and says nothing; `d…` also deletes the message you replied to; `dt…` is both timed and deleting.

## Automatic moderation

**Flood / slow mode → word blacklist → spam heuristics** run before any command or other handler and stop the update when they act; **locks** run right after the command handlers. Exempt from all of them: admins, `/approve`d users, bot owners, Telegram's own account.

| Feature | Commands | Notes |
|---|---|---|
| Anti-flood | `/antiflood` · `/setflood <3-100\|off>` · `/setfloodmode <ban\|mute\|kick\|tban\|tmute> [time]` | warns at the limit, acts 3 messages later and deletes only the flooder's own messages. Edits do not count. |
| Slow mode | `/slowmode <seconds\|off>` | The Bot API cannot set Telegram's native slow mode, so early messages are deleted. Needs the bot's *Delete messages* right. |
| Word blacklist | `/addblacklist` (one entry per line) · `/unblacklist` · `/blacklist` · `/blacklistmode <delete\|warn\|mute\|kick\|ban\|tmute\|tban> [time]` | Whole words/phrases only, case-insensitive, `*` is a wildcard (`spam*`), full-width and zero-width tricks are normalised. Also checks captions and edited messages. Max 200 entries. |
| Anti-spam | `/antispam on\|off` | scoring unchanged from v2.10 |
| Locks | `/lock /unlock /locks /locktypes` | |
| Warns | `/setwarnlimit /setwarnaction mute\|kick\|ban` | at the limit the action is applied and the user's warns are cleared |
| Reports | `/report` (reply) · `/reports on\|off` | pings the admins in the chat, stores the report, posts to the log channel |
| Log channel | `/setlog <id\|@channel>` · `/unsetlog` · `/logchannel` | the bot posts a test message first, so an unusable channel is refused |

`/modsettings` shows all of it at once.

## Approved users
`/approve` (reply or ID) exempts someone from locks, flood, spam, slow mode and the blacklist *in this chat*; `/unapprove`, `/approved`, `/approval`, `/unapproveall` (owner, asks first).

## Managing a group from a private chat
`/connect` inside the group (or `/connect <chat id|@group>` in a private chat), then use the commands marked below in the private chat; `/disconnect` ends it, `/connection` shows it.
A connection requires current membership (it is re-checked on every command, so leaving the chat ends it) and every admin command still checks *your* right in *that* chat. `/allowconnect off` limits connecting to admins.
Works for: moderation, warns, approvals, blacklist, notes, filters, locks, rules, captcha, `/disable`, all settings above, promote/demote/title, `/admins`, `/invitelink`, `/setgtitle`, `/setdesc`, `/pinned`.
Group-only because they act on messages in the chat itself: `/purge /spurge /del /pin /unpin /unpinall /report /kickme /approval`. Welcome/goodbye *text* is also group-only.

## Upgrading from v2.10
Nothing to do: the database upgrades itself (new tables/columns are additive, duplicate notes are collapsed to the newest). Review the "Changed" list in [CHANGELOG](CHANGELOG.md) - the visible differences are
the specific-right requirement, `/tban`/`/tmute` needing a time, `/unpin` no longer clearing all pins, whole-word filters, and warns being cleared after the action.
