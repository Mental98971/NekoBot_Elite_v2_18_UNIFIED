# Third-party origins

NekoBot Elite v2.15 contains re-implementations of two other open projects. Their behaviour was kept; their code was rewritten for this codebase.

| Feature | Origin | Author | Licence situation |
|---|---|---|---|
| Activity rankings (`/rankings /topusers /userstats /mytop /sysstats /fightinfo /update /restart`), `bot/plugins/chatfight.py`, `bot/utils/sysstats.py`, `bot/assets/chatfight/` | **ChatFight** - github.com/Aadishranjan/chatfight | Aadish Ranjan | **Conflicting.** The repository ships a GPL-3.0 `LICENSE` file, but every source header says "All rights reserved ... not allowed to copy, modify, redistribute, or use this code for commercial or personal projects without explicit permission" and its README badge says MIT. Get the author's written permission (aadishranjan35@gmail.com per the headers) before publishing or deploying this bot beyond personal use. If permission is refused, remove `bot/plugins/chatfight.py`, `bot/utils/sysstats.py` and `bot/assets/chatfight/` (the rest of the bot does not import them except through guarded hooks). |
| Whispers (`/whisper /list /privacy /notifications ...`, inline `@bot @user text`), `bot/plugins/whisper.py`, `bot/utils/whisper_text.py`, `bot/utils/whisper_store.py` | **PsstRobot** - github.com/ALiwoto/PsstRobot (Go) | ALiwoto | **None found** in the supplied archive (no LICENSE file, no headers). Without a licence the default is "all rights reserved"; ask the author or remove the whisper files before redistribution. |
| `bot/assets/chatfight/dejavu-sans.bold.ttf` | DejaVu fonts | Bitstream / DejaVu contributors | Free (Bitstream Vera licence). |

The artwork in `bot/assets/chatfight/bg.png` and `logo.png` comes from ChatFight and is covered by the same open question as its code.
