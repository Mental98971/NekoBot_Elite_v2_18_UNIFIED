# Languages (v2.15)

## Use

| Command | Effect |
|---|---|
| `/lang` (also `/language`, `/languages`, `/setlang`) | shows the current language and the available ones |
| `/lang <code>` | sets the language of **this chat** - group admins only in groups, anyone in a private chat |
| `/lang auto` | no explicit setting: follow the Telegram client language of whoever writes |

Languages: `en` English, `hi` हिन्दी, `hl` Hinglish (Hindi in Latin script), `es` Español, `ru` Русский, `id` Bahasa Indonesia, `ar` العربية, `zh` 中文, `fr` Français, `pt` Português.
Order of precedence: the chat's `/lang` setting, then the user's Telegram language (`pt-BR` -> `pt`, `zh-CN` -> `zh`), then `DEFAULT_LANGUAGE`. `SUPPORTED_LANGUAGES` can restrict the list (English is always kept).

## What is translated

- `/start`, `/help` (headings and category labels; the command names are language-neutral), `/settings`, `/lang` itself, the admin-only refusals (`admin_only`), and the cooldown message of the `extras` commands.
- **`/menu`** (v2.15.1): the page header, the *Menu* / *Close* buttons, all 53 category buttons and the title of each panel. The panel *bodies* (command lists with short English glosses) stay as written, so a command can
  never be missing from a translated panel. Button callbacks are derived from the English labels, so a translation can never change what a button does; a missing translation falls back to the English label.
- **AI replies:** `/ai` and the other AI commands, and the Neko personality's LLM rewrite, receive one instruction line - answer in the chat language, unless the user clearly writes in another one, in which case match theirs (names,
  commands and code unchanged). English chats get no extra instruction at all, so nothing changes for them.
- The real-person personas (Tanvi, Yashmeet, Sonal) are **not** affected: they imitate how that person writes (largely Hinglish), so they keep following the conversation's own language.

## What is not (yet) translated

Most feature output (moderation confirmations, music, economy, games, anime...), the *bodies* of the `/menu` panels, and the English parentheticals inside `/help` stay in English (about 950 call sites; the high-traffic shared helpers are the next candidates). Nothing is machine-translated at run time (it would
break HTML formatting and add latency). The catalogs are written by the project's AI assistant, not reviewed by native speakers - expect to polish some phrasing, particularly Hinglish, Arabic and Hindi register.

## Adding strings or a language

Catalogs are flat JSON files in `data/locales/<code>.json`; `en.json` is the source of truth and a missing key falls back to English, then to the key itself, so a half-translated file never breaks a reply.

1. Use `i18n.t("key", lang, **values)` (pure) or `await tr(update, "key", **values)` (resolves the chat's language: `bot/utils/langctx.py`) in the handler.
2. Add the key to `en.json` and to every other catalog. `tests/test_i18n.py` fails if a catalog lacks a key or has extra ones, if a `{placeholder}` differs from English, if HTML tags are unbalanced, if a key used in the
   code is missing, or if a catalog entry is unused.
3. New language: add `data/locales/<code>.json`, its names to `LANGUAGE_NAMES` / `_ENGLISH_NAMES` in `bot/utils/i18n.py`, and the code to `SUPPORTED_LANGUAGES`'s default in `bot/config.py`.

Storage: `chats.language` (nullable `VARCHAR(8)`), added to existing databases by the automatic column migration at start-up; `NULL` means automatic.
