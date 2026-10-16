"""
Games — new plugin, not present in any of the original four projects.

Betting commands (coinflip, slots) and rewards (trivia, guess) go
through bot.services.economy_service, the same module plugins/economy.py
uses, so balances stay consistent everywhere. Game state (trivia in
progress, an active tic-tac-toe board, a number-guessing target) is kept
in memory, scoped per chat — restarting the bot mid-game simply ends it,
which is the standard, acceptable trade-off for lightweight chat games.

All inline-keyboard callback_data is namespaced "game:" so it can never
collide with the font picker's "font:" callbacks or the main menu's
bare "page:"/"close" callbacks — see plugins/fonts.py for the full
explanation of why that matters.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import settings
from bot.services.economy_service import add_coins, add_xp, try_spend
from bot.utils.helpers import escape_html, resolve_target_user
from bot.core.errors import safe_handler
from bot.utils import unogame, wordgames

TRIVIA_QUESTIONS = [
    ("general", "What is the largest planet in our solar system?", ["Jupiter", "Saturn", "Neptune", "Earth"], 0),
    ("general", "How many continents are there?", ["5", "6", "7", "8"], 2),
    ("general", "What is the smallest prime number?", ["0", "1", "2", "3"], 2),
    ("science", "What gas do plants absorb from the atmosphere?", ["Oxygen", "Nitrogen", "CO2", "Hydrogen"], 2),
    ("science", "What is the chemical symbol for gold?", ["Go", "Gd", "Au", "Ag"], 2),
    ("science", "How many bones are in the adult human body?", ["186", "206", "226", "246"], 1),
    ("geography", "What is the longest river in the world?", ["Amazon", "Nile", "Yangtze", "Mississippi"], 1),
    ("geography", "Which country has the most time zones?", ["Russia", "USA", "France", "China"], 2),
    ("geography", "What is the smallest country in the world?", ["Monaco", "Vatican City", "San Marino", "Malta"], 1),
    ("movies", "Who directed 'Jaws' and 'E.T.'?", ["George Lucas", "Steven Spielberg", "James Cameron", "Ridley Scott"], 1),
    ("movies", "What is the highest-grossing film of all time (unadjusted)?", ["Titanic", "Avatar", "Avengers: Endgame", "Star Wars"], 1),
    ("programming", "What does 'HTTP' stand for?", [
        "HyperText Transfer Protocol", "HighText Transmission Process",
        "HyperText Transmission Protocol", "HyperTransfer Text Protocol",
    ], 0),
    ("programming", "Which language is famous for significant whitespace?", ["Java", "C++", "Python", "Rust"], 2),
    ("programming", "What does 'SQL' stand for?", [
        "Structured Query Language", "Simple Query Logic",
        "Sequential Query Language", "Standard Query Language",
    ], 0),
    ("programming", "What year was Python first released?", ["1989", "1991", "1995", "2000"], 1),
    ("general", "What is the tallest mountain in the world?", ["K2", "Kangchenjunga", "Mount Everest", "Denali"], 2),
    ("science", "What planet is known as the Red Planet?", ["Venus", "Mars", "Jupiter", "Mercury"], 1),
    ("geography", "What is the capital of Australia?", ["Sydney", "Melbourne", "Canberra", "Perth"], 2),
    ("movies", "Which studio produced 'Toy Story'?", ["DreamWorks", "Disney/Pixar", "Universal", "Warner Bros"], 1),
    ("programming", "What does 'CSS' stand for?", [
        "Computer Style Sheets", "Cascading Style Sheets",
        "Creative Style Syntax", "Colorful Style Sheets",
    ], 1),
]

RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}

SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
SLOT_PAYOUTS = {"7️⃣": 10, "💎": 6, "🔔": 4, "🍇": 3, "🍋": 2, "🍒": 2}


@dataclass
class TriviaState:
    question: str
    options: List[str]
    correct_index: int
    category: str
    solved: bool = False


@dataclass
class GuessState:
    target: int
    max_value: int
    attempts: int = 0


@dataclass
class TicTacToeState:
    board: List[str] = field(default_factory=lambda: [""] * 9)
    players: List[int] = field(default_factory=list)  # [X's id, O's id]
    names: List[str] = field(default_factory=lambda: ["", ""])
    turn: int = 0  # index into players/names

    @property
    def symbol(self) -> str:
        return "X" if self.turn == 0 else "O"


_trivia_by_key: Dict[str, TriviaState] = {}
_guess_state: Dict[int, GuessState] = {}       # keyed by chat_id
_ttt_state: Dict[int, TicTacToeState] = {}     # keyed by chat_id


def _clamp_bet(amount: int) -> Optional[int]:
    if amount < settings.games_min_bet or amount > settings.games_max_bet:
        return None
    return amount


# ==================== TRIVIA ====================

@safe_handler
async def trivia_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = context.args[0].lower() if context.args else None
    pool = [q for q in TRIVIA_QUESTIONS if not category or q[0] == category]
    if not pool:
        cats = sorted({q[0] for q in TRIVIA_QUESTIONS})
        await update.message.reply_text(f"❌ Unknown category. Try one of: {', '.join(cats)}")
        return

    cat, question, options, correct_idx = random.choice(pool)
    order = list(range(len(options)))
    random.shuffle(order)
    shuffled = [options[i] for i in order]
    new_correct_idx = order.index(correct_idx)

    msg = await update.message.reply_text(
        f"🧠 <b>Trivia</b> <i>({escape_html(cat)})</i>\n\n{escape_html(question)}",
        parse_mode="HTML",
    )
    key = f"{update.effective_chat.id}:{msg.message_id}"
    _trivia_by_key[key] = TriviaState(question, shuffled, new_correct_idx, cat)

    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"game:trivia:{key}:{i}")]
        for i, opt in enumerate(shuffled)
    ]
    await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(buttons))


async def _handle_trivia_answer(query, key: str, choice: int):
    state = _trivia_by_key.get(key)
    if not state or state.solved:
        await query.answer("This question is already closed.", show_alert=False)
        return

    if choice != state.correct_index:
        await query.answer("❌ Wrong!", show_alert=False)
        return

    state.solved = True
    user = query.from_user
    chat_id = int(key.split(":")[0])
    new_level, leveled_up = await add_xp(chat_id, user.id, 20)
    await add_coins(chat_id, user.id, 30)

    await query.answer("✅ Correct!", show_alert=False)
    await query.edit_message_text(
        f"🧠 <b>Trivia</b> <i>({escape_html(state.category)})</i>\n\n{escape_html(state.question)}\n\n"
        f"✅ <b>{escape_html(user.first_name)}</b> got it! +20 XP, +30 🪙"
        + (f" · leveled up to {new_level}!" if leveled_up else ""),
        parse_mode="HTML",
    )
    _trivia_by_key.pop(key, None)


# ==================== ROCK PAPER SCISSORS ====================

@safe_handler
async def rps_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = (context.args[0].lower() if context.args else "")
    if choice not in RPS_BEATS:
        await update.message.reply_text("Usage: /rps rock|paper|scissors")
        return
    bot_choice = random.choice(list(RPS_BEATS))

    if choice == bot_choice:
        result = "🤝 It's a tie!"
    elif RPS_BEATS[choice] == bot_choice:
        result = "🎉 You win!"
    else:
        result = "🤖 I win!"

    await update.message.reply_text(
        f"{RPS_EMOJI[choice]} vs {RPS_EMOJI[bot_choice]}\n{result}"
    )


# ==================== COINFLIP & SLOTS (betting) ====================

@safe_handler
async def coinflip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("🎲 Betting games are per-group — try this in a group chat.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Usage: /coinflip <amount {settings.games_min_bet}-{settings.games_max_bet}>")
        return
    amount = _clamp_bet(int(context.args[0]))
    if amount is None:
        await update.message.reply_text(f"❌ Bet must be between {settings.games_min_bet} and {settings.games_max_bet}.")
        return

    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    if not await try_spend(chat_id, user_id, amount):
        await update.message.reply_text("❌ You don't have enough coins.")
        return

    won = random.random() < 0.5
    if won:
        new_balance = await add_coins(chat_id, user_id, amount * 2)
        await update.message.reply_text(f"🪙 Heads! You won {amount:,} 🪙 (balance: {new_balance:,})")
    else:
        new_balance = await add_coins(chat_id, user_id, 0)
        await update.message.reply_text(f"🪙 Tails! You lost {amount:,} 🪙 (balance: {new_balance:,})")


@safe_handler
async def slots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("🎰 Betting games are per-group — try this in a group chat.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(f"Usage: /slots <amount {settings.games_min_bet}-{settings.games_max_bet}>")
        return
    amount = _clamp_bet(int(context.args[0]))
    if amount is None:
        await update.message.reply_text(f"❌ Bet must be between {settings.games_min_bet} and {settings.games_max_bet}.")
        return

    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    if not await try_spend(chat_id, user_id, amount):
        await update.message.reply_text("❌ You don't have enough coins.")
        return

    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    display = " | ".join(reels)

    if reels[0] == reels[1] == reels[2]:
        payout = amount * SLOT_PAYOUTS[reels[0]]
        new_balance = await add_coins(chat_id, user_id, payout)
        await update.message.reply_text(f"🎰 {display}\n🎉 JACKPOT! +{payout:,} 🪙 (balance: {new_balance:,})")
    elif reels[0] == reels[1] or reels[1] == reels[2]:
        payout = int(amount * 1.5)
        new_balance = await add_coins(chat_id, user_id, payout)
        await update.message.reply_text(f"🎰 {display}\n✨ Two of a kind! +{payout:,} 🪙 (balance: {new_balance:,})")
    else:
        new_balance = await add_coins(chat_id, user_id, 0)
        await update.message.reply_text(f"🎰 {display}\n💨 No match. Lost {amount:,} 🪙 (balance: {new_balance:,})")


# ==================== NUMBER GUESSING ====================

@safe_handler
async def guess_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if context.args and context.args[0].lower() == "start":
        max_value = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 100
        _guess_state[chat_id] = GuessState(target=random.randint(1, max_value), max_value=max_value)
        await update.message.reply_text(f"🔢 I'm thinking of a number between 1 and {max_value}. /guess <number> to guess!")
        return

    state = _guess_state.get(chat_id)
    if not state:
        await update.message.reply_text("No game running. Start one with /guess start [max]")
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /guess <number>  (or /guess start [max] for a new game)")
        return

    guess = int(context.args[0])
    state.attempts += 1

    if guess == state.target:
        reward = max(10, 100 - state.attempts * 5)
        new_balance = await add_coins(chat_id, update.effective_user.id, reward)
        await add_xp(chat_id, update.effective_user.id, 10)
        await update.message.reply_text(
            f"🎉 Correct! It was {state.target}. Solved in {state.attempts} guesses. "
            f"+{reward:,} 🪙 (balance: {new_balance:,})"
        )
        del _guess_state[chat_id]
    elif guess < state.target:
        await update.message.reply_text("📈 Higher!")
    else:
        await update.message.reply_text("📉 Lower!")


# ==================== TIC-TAC-TOE ====================

def _ttt_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    state = _ttt_state[chat_id]
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            label = state.board[i] or "·"
            row.append(InlineKeyboardButton(label, callback_data=f"game:ttt:{chat_id}:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _ttt_winner(board: List[str]) -> Optional[str]:
    lines = [
        (0, 1, 2), (3, 4, 5), (6, 7, 8),
        (0, 3, 6), (1, 4, 7), (2, 5, 8),
        (0, 4, 8), (2, 4, 6),
    ]
    for a, b, c in lines:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


@safe_handler
async def tictactoe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ Tic-tac-toe needs two players — try this in a group chat.")
        return

    opponent_id, opponent_name = await resolve_target_user(update, context)
    if not opponent_id:
        await update.message.reply_text("Usage: reply to someone with /tictactoe, or /tictactoe @username")
        return
    if opponent_id == update.effective_user.id:
        await update.message.reply_text("❌ You can't play against yourself.")
        return

    chat_id = update.effective_chat.id
    state = TicTacToeState(players=[update.effective_user.id, opponent_id], names=[update.effective_user.first_name, opponent_name])
    _ttt_state[chat_id] = state

    await update.message.reply_text(
        f"⭕❌ <b>{escape_html(state.names[0])}</b> (X) vs <b>{escape_html(state.names[1])}</b> (O)\n"
        f"Turn: <b>{escape_html(state.names[0])}</b>",
        parse_mode="HTML",
        reply_markup=_ttt_keyboard(chat_id),
    )


async def _handle_ttt_move(query, chat_id: int, cell: int):
    state = _ttt_state.get(chat_id)
    if not state:
        await query.answer("No game running here.", show_alert=False)
        return

    player = query.from_user
    if player.id != state.players[state.turn]:
        await query.answer("It's not your turn.", show_alert=True)
        return
    if state.board[cell]:
        await query.answer("That cell is taken.", show_alert=False)
        return

    state.board[cell] = state.symbol
    winner = _ttt_winner(state.board)
    draw = not winner and all(state.board)

    await query.answer()

    if winner or draw:
        del _ttt_state[chat_id]
        if winner:
            winner_idx = 0 if winner == "X" else 1
            winner_id = state.players[winner_idx]
            await add_coins(chat_id, winner_id, 50)
            await add_xp(chat_id, winner_id, 15)
            text = f"🏆 <b>{escape_html(state.names[winner_idx])}</b> wins! (+50 🪙, +15 XP)"
        else:
            text = "🤝 It's a draw!"
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=_ttt_keyboard_static(state))
        return

    state.turn = 1 - state.turn
    await query.edit_message_text(
        f"⭕❌ <b>{escape_html(state.names[0])}</b> (X) vs <b>{escape_html(state.names[1])}</b> (O)\n"
        f"Turn: <b>{escape_html(state.names[state.turn])}</b>",
        parse_mode="HTML",
        reply_markup=_ttt_keyboard(chat_id),
    )


def _ttt_keyboard_static(state: TicTacToeState) -> InlineKeyboardMarkup:
    """Render the final board with disabled (no-op) buttons once a game ends."""
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            row.append(InlineKeyboardButton(state.board[i] or "·", callback_data="game:ttt:over"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ==================== CALLBACK ROUTER ====================

@safe_handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data[len("game:"):]

    if data.startswith("trivia:"):
        _, chat_id, msg_id, choice = data.split(":")
        await _handle_trivia_answer(query, f"{chat_id}:{msg_id}", int(choice))
        return

    if data.startswith("ttt:"):
        parts = data.split(":")
        if parts[1] == "over":
            await query.answer("This game has ended.", show_alert=False)
            return
        chat_id, cell = int(parts[1]), int(parts[2])
        await _handle_ttt_move(query, chat_id, cell)
        return

    await query.answer()

# ==================== SHARED: in-memory game state with expiry ====================
# Chat-scoped, single-process. Stale lobbies/games are pruned on access so an
# abandoned game never blocks a chat forever or leaks memory.

_GAME_TTL_SECONDS = 2 * 60 * 60


def _get_state(store: Dict[int, dict], chat_id: int) -> Optional[dict]:
    now = time.monotonic()
    for cid in [c for c, st in store.items() if now - st.get("ts", now) > _GAME_TTL_SECONDS]:
        store.pop(cid, None)
    st = store.get(chat_id)
    if st is not None:
        st["ts"] = now
    return st


def _new_state(store: Dict[int, dict], chat_id: int, **fields) -> dict:
    st = {"ts": time.monotonic(), **fields}
    store[chat_id] = st
    return st


def _grid_text(grid: List[List[str]]) -> str:
    return "\n".join(" ".join(row) for row in grid)


async def _reward(chat_id: int, user_id: int, coins: int, xp: int) -> None:
    try:
        if coins:
            await add_coins(chat_id, user_id, coins)
        if xp:
            await add_xp(chat_id, user_id, xp)
    except Exception:  # economy must never break a game reply
        pass


_wordseek_state: Dict[int, dict] = {}
_wordgrid_state: Dict[int, dict] = {}
_wordchain_state: Dict[int, dict] = {}
_chess_state: Dict[int, dict] = {}
_ludo_state: Dict[int, dict] = {}
_uno_state: Dict[int, dict] = {}


# ==================== WORDSEEK ====================

@safe_handler
async def wordseek_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args and context.args[0].lower() in ("end", "stop", "giveup"):
        st = _wordseek_state.pop(chat_id, None)
        await update.message.reply_text(
            f"🔎 Wordseek ended. The word was <b>{st['word']}</b>." if st else "No active Wordseek.",
            parse_mode="HTML",
        )
        return
    word = random.choice([w for w in wordgames.LEXICON if len(w) <= 8])
    grid = wordgames.make_wordseek_grid(word)
    _new_state(_wordseek_state, chat_id, word=word, grid=grid)
    await update.message.reply_text(
        f"🔎 <b>Wordseek</b> — find the hidden {len(word)}-letter word "
        f"(right, down or diagonal ↘).\n\n<code>{_grid_text(grid)}</code>\n\n"
        "Just send the word in chat. <code>/wordseek end</code> to give up.",
        parse_mode="HTML",
    )


# ==================== WORDGRID ====================

@safe_handler
async def wordgrid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    grid, planted = wordgames.make_wordgrid()
    _new_state(_wordgrid_state, chat_id, grid=grid, planted=planted, found={}, scores={}, names={})
    await update.message.reply_text(
        "🔠 <b>Wordgrid</b> — trace words (3+ letters) through touching tiles, "
        "no tile twice, diagonals allowed.\n\n"
        f"<code>{_grid_text(grid)}</code>\n\n"
        f"There are at least {len(planted)} hidden words from a themed list "
        "(anime, games, science, nature). Send words in chat; /wordgridend to finish.",
        parse_mode="HTML",
    )


@safe_handler
async def wordgrid_end_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    st = _wordgrid_state.pop(chat_id, None)
    if not st:
        await update.message.reply_text("No active Wordgrid.")
        return
    board = sorted(st["scores"].items(), key=lambda kv: -kv[1])
    missed = sorted(set(st["planted"]) - set(st["found"]))
    lines = ["🔠 <b>Wordgrid over</b>"]
    if board:
        for i, (uid, pts) in enumerate(board, 1):
            lines.append(f"{i}. {escape_html(st['names'].get(uid, str(uid)))} — {pts} pts")
        await _reward(chat_id, board[0][0], coins=board[0][1] * 5, xp=10)
    else:
        lines.append("Nobody scored.")
    if missed:
        lines.append("Hidden words nobody found: " + ", ".join(missed))
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ==================== WORDCHAIN ====================

@safe_handler
async def wordchain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if context.args and context.args[0].lower() in ("end", "stop"):
        st = _wordchain_state.pop(chat_id, None)
        await update.message.reply_text(
            f"⛓️ Wordchain over — {len(st['used'])} words chained." if st else "No active Wordchain."
        )
        return
    seed = random.choice(["ANIME", "GAME", "PIXEL", "CHAOS", "BLADE", "STORM"])
    _new_state(_wordchain_state, chat_id, last=seed, used={seed}, last_user=None)
    await update.message.reply_text(
        f"⛓️ <b>Wordchain</b>\nStart: <b>{seed}</b> → next word starts with <b>{seed[-1]}</b>.\n"
        "Send words in chat (3+ letters, no repeats, no going twice in a row). "
        "Honor system: no dictionary is checked. <code>/wordchain end</code> to stop.",
        parse_mode="HTML",
    )


# ==================== TEXT LISTENER (wordseek / wordgrid / wordchain) ====================

@safe_handler
async def games_text_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not msg.text or not chat or not user:
        return
    text = msg.text.strip()
    if " " in text or not text.isalpha() or not text.isascii() or len(text) > 20:
        return
    word = text.upper()
    cid = chat.id

    ws = _get_state(_wordseek_state, cid)
    if ws and word == ws["word"]:
        _wordseek_state.pop(cid, None)
        await _reward(cid, user.id, coins=25, xp=5)
        await msg.reply_text(f"🎉 {escape_html(user.first_name)} found <b>{word}</b>! +25 🪙", parse_mode="HTML")

    wg = _get_state(_wordgrid_state, cid)
    if wg:
        ok, why = wordgames.judge_grid_word(wg["grid"], set(wg["found"]), word)
        if ok:
            pts = wordgames.score_word(word)
            wg["found"][word] = user.id
            wg["scores"][user.id] = wg["scores"].get(user.id, 0) + pts
            wg["names"][user.id] = user.first_name
            await msg.reply_text(f"✅ {word} +{pts}")
        elif why in ("already found", "can't be traced from adjacent tiles") and len(word) >= 3:
            await msg.reply_text(f"❌ {word}: {why}")

    wc = _get_state(_wordchain_state, cid)
    if wc:
        ok, why = wordgames.judge_chain_word(wc["last"], wc["used"], wc["last_user"], user.id, word)
        if ok:
            wc["used"].add(word)
            wc["last"], wc["last_user"] = word, user.id
            await msg.reply_text(f"⛓️ {word} → next starts with <b>{word[-1]}</b> ({len(wc['used']) - 1} chained)", parse_mode="HTML")
        elif why.startswith("already used") or why.startswith("let someone"):
            await msg.reply_text(f"❌ {why}")


# ==================== CHESS (python-chess) ====================

_PIECES = {"r": "♜", "n": "♞", "b": "♝", "q": "♛", "k": "♚", "p": "♟",
           "R": "♖", "N": "♘", "B": "♗", "Q": "♕", "K": "♔", "P": "♙"}


def _chess_board_text(board) -> str:
    rows = []
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            piece = board.piece_at(rank * 8 + file)
            cells.append(_PIECES[piece.symbol()] if piece else ("·" if (rank + file) % 2 == 0 else " "))
        rows.append(f"{rank + 1} " + " ".join(cells))
    rows.append("  a b c d e f g h")
    return "\n".join(rows)


def _chess_status(st: dict) -> str:
    board = st["board"]
    turn_name = st["white_name"] if board.turn else st["black_name"]
    side = "White" if board.turn else "Black"
    check = " — check!" if board.is_check() else ""
    return f"<code>{_chess_board_text(board)}</code>\n{side} to move: {escape_html(turn_name or '?')}{check}"


@safe_handler
async def chess_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/chess [join | move <e2e4|Nf3> | board | resign]"""
    try:
        import chess
    except ImportError:
        await update.message.reply_text("♟️ Chess needs the python-chess package (pip install chess). Ask the bot owner.")
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    action = context.args[0].lower() if context.args else "start"
    st = _get_state(_chess_state, chat_id)

    if action == "start":
        if st:
            await update.message.reply_text("A chess game is already running here. /chess board or /chess resign.")
            return
        _new_state(_chess_state, chat_id, board=chess.Board(), white=user.id, white_name=user.first_name,
                   black=None, black_name=None)
        await update.message.reply_text(
            f"♟️ <b>Chess</b> — {escape_html(user.first_name)} plays White.\n"
            "Someone send <code>/chess join</code> to play Black.\n"
            "Moves: <code>/chess move e2e4</code> or <code>/chess move Nf3</code>.",
            parse_mode="HTML",
        )
        return
    if not st:
        await update.message.reply_text("No chess game here. /chess to start one.")
        return
    if action == "join":
        if st["black"] is not None:
            await update.message.reply_text("Black is already taken.")
        elif user.id == st["white"]:
            await update.message.reply_text("You can't play yourself.")
        else:
            st["black"], st["black_name"] = user.id, user.first_name
            await update.message.reply_text(f"♟️ {escape_html(user.first_name)} joined as Black!\n" + _chess_status(st), parse_mode="HTML")
        return
    if action == "board":
        await update.message.reply_text(_chess_status(st), parse_mode="HTML")
        return
    if action in ("resign", "stop"):
        if user.id not in (st["white"], st["black"]):
            await update.message.reply_text("Only the players can resign.")
            return
        _chess_state.pop(chat_id, None)
        other = st["black_name"] if user.id == st["white"] else st["white_name"]
        await update.message.reply_text(f"🏳️ {escape_html(user.first_name)} resigned." + (f" {escape_html(other)} wins." if other else ""), parse_mode="HTML")
        return
    if action == "move":
        board = st["board"]
        if st["black"] is None:
            await update.message.reply_text("Waiting for an opponent: /chess join")
            return
        mover = st["white"] if board.turn else st["black"]
        if user.id != mover:
            await update.message.reply_text("Not your turn.")
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /chess move e2e4 (or SAN like Nf3)")
            return
        raw = context.args[1]
        try:
            try:
                move = board.parse_san(raw)
            except ValueError:
                move = board.parse_uci(raw.lower())
                if move not in board.legal_moves:
                    raise ValueError("illegal")
        except ValueError:
            await update.message.reply_text("❌ Illegal or unreadable move.")
            return
        board.push(move)
        if board.is_game_over():
            _chess_state.pop(chat_id, None)
            result = board.result(claim_draw=True)
            if board.is_checkmate():
                winner_id, winner_name = (user.id, user.first_name)
                await _reward(chat_id, winner_id, coins=50, xp=15)
                verdict = f"Checkmate — {escape_html(winner_name)} wins! +50 🪙"
            else:
                verdict = "Draw."
            await update.message.reply_text(f"<code>{_chess_board_text(board)}</code>\n{verdict} ({result})", parse_mode="HTML")
            return
        await update.message.reply_text(_chess_status(st), parse_mode="HTML")
        return
    await update.message.reply_text("Usage: /chess [join|move <m>|board|resign]")


# ==================== LUDO (simplified race) ====================

@safe_handler
async def ludo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    action = context.args[0].lower() if context.args else "start"
    st = _get_state(_ludo_state, chat_id)
    if action == "start":
        if st and st["started"]:
            await update.message.reply_text("A Ludo game is in progress. Finish it, or the host can /ludo end.")
            return
        _new_state(_ludo_state, chat_id, host=user.id,
                   players=[{"id": user.id, "name": user.first_name, "pos": 0}], turn=0, started=False)
        await update.message.reply_text(
            f"🎲 <b>LUDO</b> (race to 56)\nLobby opened by {escape_html(user.first_name)}.\n"
            "/ludo join (up to 4) · /ludo go · /ludo roll · /ludo end",
            parse_mode="HTML",
        )
        return
    if not st:
        await update.message.reply_text("No Ludo lobby. /ludo start")
        return
    if action == "end":
        if user.id != st["host"]:
            await update.message.reply_text("Only the host can end the game.")
            return
        _ludo_state.pop(chat_id, None)
        await update.message.reply_text("🎲 Ludo ended.")
    elif action == "join":
        if st["started"]:
            await update.message.reply_text("Game already started.")
        elif any(p["id"] == user.id for p in st["players"]):
            await update.message.reply_text("Already in lobby.")
        elif len(st["players"]) >= 4:
            await update.message.reply_text("Lobby full (4).")
        else:
            st["players"].append({"id": user.id, "name": user.first_name, "pos": 0})
            await update.message.reply_text("Joined. Players: " + ", ".join(p["name"] for p in st["players"]))
    elif action == "go":
        if user.id != st["host"]:
            await update.message.reply_text("Only the host can start.")
        elif st["started"]:
            await update.message.reply_text("Already started.")
        elif len(st["players"]) < 2:
            await update.message.reply_text("Need at least 2 players.")
        else:
            st["started"] = True
            await update.message.reply_text(f"🏁 Ludo started! {st['players'][0]['name']}'s turn — /ludo roll")
    elif action == "roll":
        if not st["started"]:
            await update.message.reply_text("Game not started. /ludo go")
            return
        cur = st["players"][st["turn"] % len(st["players"])]
        if cur["id"] != user.id:
            await update.message.reply_text(f"Not your turn — wait for {cur['name']}.")
            return
        roll = random.randint(1, 6)
        # exact finish: overshooting 56 means you stay put
        if cur["pos"] + roll <= 56:
            cur["pos"] += roll
        msg = f"🎲 {escape_html(user.first_name)} rolled <b>{roll}</b> → {cur['pos']}/56"
        if cur["pos"] == 56:
            _ludo_state.pop(chat_id, None)
            await _reward(chat_id, user.id, coins=40, xp=10)
            await update.message.reply_text(msg + f"\n🏆 {escape_html(user.first_name)} wins! +40 🪙", parse_mode="HTML")
            return
        if roll != 6:
            st["turn"] += 1
        nxt = st["players"][st["turn"] % len(st["players"])]
        msg += ("\n🎉 Rolled a 6 — roll again!" if roll == 6 else f"\nNext: {escape_html(nxt['name'])}")
        await update.message.reply_text(msg, parse_mode="HTML")
    else:
        await update.message.reply_text("Usage: /ludo start|join|go|roll|end")


# ==================== UNO ====================

async def _send_hand(context, chat_id: int, game: unogame.UnoGame, player: unogame.Player) -> bool:
    """DM the player's hand (never reveal it in the group). False if DMs are closed."""
    cards = ", ".join(unogame.card_str(c) for c in player.hand) or "(empty)"
    try:
        await context.bot.send_message(player.id, f"🃏 Your UNO hand:\n{cards}\n\nColor in play: {unogame.COLOR_EMOJI.get(game.color, '')}")
        return True
    except TelegramError:
        return False


@safe_handler
async def uno_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    action = context.args[0].lower() if context.args else "start"
    st = _get_state(_uno_state, chat_id)
    game: Optional[unogame.UnoGame] = st["game"] if st else None

    if action == "start":
        if game and game.started:
            await update.message.reply_text("A UNO game is in progress. Host: /uno end")
            return
        _new_state(_uno_state, chat_id, game=unogame.UnoGame(user.id, user.first_name), host=user.id)
        await update.message.reply_text(
            f"🃏 <b>UNO</b> — lobby by {escape_html(user.first_name)}.\n"
            "/uno join · /uno go · /uno play &lt;card&gt; · /uno draw · /uno hand · /uno end\n"
            "<i>Open a DM with me first (send /start there) so I can deal you your hand.</i>",
            parse_mode="HTML",
        )
        return
    if not game:
        await update.message.reply_text("No UNO lobby. /uno start")
        return
    if action == "end":
        if user.id != st["host"]:
            await update.message.reply_text("Only the host can end the game.")
            return
        _uno_state.pop(chat_id, None)
        await update.message.reply_text("🃏 UNO ended.")
    elif action == "join":
        ok, why = game.add_player(user.id, user.first_name)
        await update.message.reply_text(f"Joined UNO ({len(game.players)} players)." if ok else why)
    elif action == "go":
        if user.id != st["host"]:
            await update.message.reply_text("Only the host can start.")
            return
        ok, why = game.start()
        if not ok:
            await update.message.reply_text(why)
            return
        blocked = [p.name for p in game.players if not await _send_hand(context, chat_id, game, p)]
        text = (f"🃏 UNO started! Top: {unogame.card_str(game.top)}\n{game.current.name}'s turn.\n"
                "Play with <code>/uno play r5</code>, <code>/uno play wild blue</code>, <code>/uno draw</code>.")
        if blocked:
            text += "\n⚠️ Couldn't DM: " + ", ".join(escape_html(n) for n in blocked) + " — open a DM with me, then /uno hand."
        await update.message.reply_text(text, parse_mode="HTML")
    elif action == "hand":
        p = game.player(user.id)
        if not p or not game.started:
            await update.message.reply_text("You're not in a started game.")
        elif await _send_hand(context, chat_id, game, p):
            await update.message.reply_text("📬 Sent your hand in DM.")
        else:
            await update.message.reply_text("I can't DM you — open a chat with me and send /start first.")
    elif action in ("draw", "play"):
        res = game.draw_turn(user.id) if action == "draw" else game.play(user.id, context.args[1:])
        await update.message.reply_text(escape_html(res.message), parse_mode="HTML")
        if not res.ok:
            return
        if res.winner:
            _uno_state.pop(chat_id, None)
            await _reward(chat_id, res.winner.id, coins=60, xp=15)
            return
        p = game.player(user.id)
        if p and not await _send_hand(context, chat_id, game, p):
            await update.message.reply_text("⚠️ I can't DM your updated hand — open a chat with me and send /start.")
    else:
        await update.message.reply_text("Usage: /uno start|join|go|hand|draw|play <card>|end")


def register(app):
    if not settings.enable_games:
        return
    app.add_handler(CommandHandler("trivia", trivia_cmd))
    app.add_handler(CommandHandler("rps", rps_cmd))
    app.add_handler(CommandHandler("coinflip", coinflip_cmd))
    app.add_handler(CommandHandler("slots", slots_cmd))
    app.add_handler(CommandHandler("guess", guess_cmd))
    app.add_handler(CommandHandler("tictactoe", tictactoe_cmd))
    app.add_handler(CommandHandler("wordseek", wordseek_cmd))
    app.add_handler(CommandHandler("wordgrid", wordgrid_cmd))
    app.add_handler(CommandHandler("wordgridend", wordgrid_end_cmd))
    app.add_handler(CommandHandler("wordchain", wordchain_cmd))
    app.add_handler(CommandHandler("chess", chess_cmd))
    app.add_handler(CommandHandler("ludo", ludo_cmd))
    app.add_handler(CommandHandler("uno", uno_cmd))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^game:"))
    # Own handler group (10): PTB runs one handler per group per update, and
    # groups 1-9 are already claimed by other plugins' ambient listeners.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, games_text_listener), group=10)
