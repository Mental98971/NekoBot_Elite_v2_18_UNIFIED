"""A v2.10 database must upgrade in place to v2.12 without data loss (run in a subprocess for a clean engine)."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

NEW_CHAT_COLUMNS = (
    "antiflood_action", "antiflood_action_seconds", "antispam_enabled", "slowmode_seconds", "blacklist_duration",
    "reports_enabled", "connect_allowed", "private_rules", "clean_welcome", "last_welcome_id", "clean_service",
    "nightmode_active", "nightmode_prev_permissions",
)

SCRIPT = textwrap.dedent('''
    import asyncio, os, sqlite3, sys
    db, root, cols = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
    os.environ.update(BOT_TOKEN="1:x", OWNER_ID="1", DATABASE_URL=f"sqlite+aiosqlite:///{db}")
    sys.path.insert(0, root)
    from bot.core import database as d

    async def main():
        await d.init_db()                                   # a fresh v2.12 schema ...
        con = sqlite3.connect(db)
        con.execute("DROP INDEX uq_notes_chat_name")        # ... rewound to v2.10: no unique note index,
        con.commit(); con.close()
        async with d.async_session() as s:                   # duplicate notes the old /save produced,
            s.add(d.Chat(id=-1, title="old chat", type="supergroup", rules="keep me"))
            s.add_all([d.Note(chat_id=-1, name="a", content="first"), d.Note(chat_id=-1, name="a", content="second"),
                       d.Note(chat_id=-1, name="b", content="only")])
            await s.commit()
        con = sqlite3.connect(db)
        for col in cols:                                     # no v2.12 chat columns, no approvals/connections tables
            con.execute(f"ALTER TABLE chats DROP COLUMN {col}")
        con.execute("DROP TABLE approvals"); con.execute("DROP TABLE connections")
        con.commit(); con.close()
        await d.init_db()                                    # <- the upgrade under test
        await d.init_db()                                    # <- and it is idempotent
    asyncio.run(main())
''')


class TestV210ToV212Upgrade(unittest.TestCase):
    def test_in_place_upgrade_keeps_data_and_adds_the_new_schema(self):
        if sqlite3.sqlite_version_info < (3, 35):
            self.skipTest("needs SQLite >= 3.35 for ALTER TABLE DROP COLUMN to build the old-schema fixture")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            proc = subprocess.run([sys.executable, "-c", SCRIPT, str(db), str(PROJECT_ROOT), ",".join(NEW_CHAT_COLUMNS)],
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
            con = sqlite3.connect(db)
            try:
                chat_cols = {r[1] for r in con.execute("PRAGMA table_info(chats)")}
                self.assertTrue(set(NEW_CHAT_COLUMNS) <= chat_cols, set(NEW_CHAT_COLUMNS) - chat_cols)
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"approvals", "connections"} <= tables)
                self.assertEqual(con.execute("SELECT rules FROM chats WHERE id=-1").fetchone()[0], "keep me")
                self.assertEqual(sorted(con.execute("SELECT name, content FROM notes WHERE chat_id=-1")), [("a", "second"), ("b", "only")],
                                 "the newest duplicate note survives, nothing else is lost")
                indexes = {r[1]: r[2] for r in con.execute("PRAGMA index_list(notes)")}
                self.assertEqual(indexes.get("uq_notes_chat_name"), 1, "unique index created after the dedupe")
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
