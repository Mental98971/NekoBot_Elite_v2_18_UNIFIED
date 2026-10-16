"""Tests for bot/utils/symbolic.py + bot/plugins/symbolic_math.py (v2.17): correctness, input hardening, sandbox."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_TMP = tempfile.NamedTemporaryFile(prefix="test_symbolic_", suffix=".db", delete=False)
_TMP.close()
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_ID", "123456")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP.name}")
os.environ["SYMBOLIC_TEST_OPS"] = "1"          # lets the worker run the test-only "_sleep" operation

from bot.utils import symbolic as S  # noqa: E402
import bot.plugins.symbolic_math as sm  # noqa: E402
from tests._fakes import FakeBot, make_context, make_update  # noqa: E402


class TestEngine(unittest.TestCase):
    CASES = [
        ("simplify", "(x^2-1)/(x-1)", "x + 1"),
        ("simplify", "2x+3x", "5*x"),
        ("simplify", "10!/8!", "90"),
        ("factor", "x^2-5x+6", "(x - 3)*(x - 2)"),
        ("derive", "x^3+2x", "3*x^2 + 2"),
        ("derive", "5", "0"),
        ("derive", "sqrt(x)", "1/(2*sqrt(x))"),
        ("integrate", "x^2", "x^3/3 + C"),
        ("integrate", "sin(x)", "-cos(x) + C"),
        ("zeroes", "x^2-4", "-2, 2"),
        ("zeroes", "x^2+1", "No real zeroes."),
        ("tangent", "2|x^3", "y = 12*x - 16"),
        ("area", "2:4|x^3", "60"),
        ("area", "0:pi|sin(x)", "2"),
    ]

    def test_known_answers(self):
        for op, text, expected in self.CASES:
            with self.subTest(op=op, text=text):
                self.assertEqual(S.execute(op, text), expected)

    def test_fn_gives_exact_value_and_decimal(self):
        self.assertEqual(S.execute("fn", "pi/6", "sin"), "1/2 ≈ 0.5000000000")
        self.assertEqual(S.execute("fn", "2pi", "cos"), "1")
        self.assertEqual(S.execute("fn", "-3", "abs"), "3")

    def test_unsolvable_things_report_instead_of_crashing(self):
        for op, text in (("integrate", "x^x"), ("zeroes", "x^x-y^x*sin(x)^3-cos(x)"),):
            with self.subTest(op=op), self.assertRaises(S.SymbolicError):
                S.execute(op, text)

    def test_usage_errors(self):
        for op, text in (("tangent", "x^3"), ("area", "x^3"), ("area", "2|x^3"), ("simplify", ""), ("nope", "x")):
            with self.subTest(op=op, text=text), self.assertRaises(S.SymbolicError):
                S.execute(op, text)

    def test_hostile_text_never_reaches_the_parser(self):
        hostile = ["__import__('os').system('id')", "x.__class__", "().__class__.__bases__", "lambda x: x", "open('/etc/passwd')",
                   "x;y", "[1,2]", "'a'", "a=1", "Symbol('x')", "sympify('1')", "x[0]", "{1}", "x@y", "x_y", "x\\y", "x#y", "x$y", "a\nb\\"]
        for bad in hostile:
            with self.subTest(bad=bad), self.assertRaises(S.SymbolicError):
                S.tokenize(bad)

    def test_size_limits(self):
        for bad in ("x" * 200, "1234567890123", "2^10001", "9^9^9", "1^2^3^4^5^6", "3.14159265358979323846264338327950288"):
            with self.subTest(bad=bad[:20]), self.assertRaises(S.SymbolicError):
                S.tokenize(bad)

    def test_unicode_math_symbols_are_normalised(self):
        self.assertEqual(S.execute("simplify", "2×x + x÷1"), "3*x")
        self.assertEqual(S.execute("fn", "π", "cos"), "-1")

    def test_unknown_names_are_just_symbols_not_python(self):
        self.assertEqual(S.execute("simplify", "exec(1)"), "c*x*exp(2)")        # split into e*x*e*c, harmless
        self.assertEqual(S.execute("derive", "abc"), "b*c")


class TestSandbox(unittest.IsolatedAsyncioTestCase):
    async def test_roundtrip_error_passthrough_and_hard_timeout_with_recovery(self):
        sb = S.SymbolicSandbox(timeout=1.5, memory_mb=1024)
        try:
            self.assertEqual(await sb.run("factor", "x^2-5x+6"), "(x - 3)*(x - 2)")
            with self.assertRaises(S.SymbolicError):
                await sb.run("simplify", "x;y")                                      # rejected before the worker
            with self.assertRaises(S.SymbolicError) as ctx:
                await sb.run("area", "x^3")                                          # usage error from inside the worker
            self.assertIn("Usage", str(ctx.exception))
            first_pid = sb._proc.pid
            with self.assertRaises(S.SymbolicTimeout):
                await sb.run("_sleep", "30")                                         # runaway job: worker is killed
            self.assertIsNone(sb._proc)
            self.assertEqual(await sb.run("derive", "x^3"), "3*x^2")                 # a fresh worker takes over
            self.assertNotEqual(sb._proc.pid, first_pid)
        finally:
            await sb.close()

    async def test_runaway_power_tower_is_stopped(self):
        sb = S.SymbolicSandbox(timeout=3, memory_mb=512)
        try:
            with self.assertRaises(S.SymbolicError):
                await sb.run("simplify", "9^(9^(9^9))")
            self.assertEqual(await sb.run("simplify", "1+1"), "2")
        finally:
            await sb.close()

    async def test_jobs_from_many_callers_are_serialised_and_all_answered(self):
        import asyncio
        sb = S.SymbolicSandbox(timeout=10)
        try:
            results = await asyncio.gather(*(sb.run("derive", f"x^{n}") for n in range(2, 8)))
            self.assertEqual(results, [f"{n}*x^{n - 1}" if n > 2 else "2*x" for n in range(2, 8)])
        finally:
            await sb.close()


class TestPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        sm._last_use.clear()

    async def test_command_answers_and_formats(self):
        upd = make_update("/derive", user_id=1, chat_id=-1)
        await sm._CMDS["derive"](upd, make_context(FakeBot(), ["x^3+2x"]))
        self.assertEqual(upd.effective_message.reply_text.await_args.args[0], "<code>3*x^2 + 2</code>")

    async def test_usage_bad_input_and_reply_source(self):
        upd = make_update("/area", user_id=2, chat_id=-1)
        await sm._CMDS["area"](upd, make_context(FakeBot(), []))
        self.assertIn("Usage: /area", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/derive", user_id=3, chat_id=-1)
        await sm._CMDS["derive"](upd, make_context(FakeBot(), ["x;"]))
        self.assertIn("❌", upd.effective_message.reply_text.await_args.args[0])
        upd = make_update("/factor", user_id=4, chat_id=-1)
        upd.effective_message.reply_to_message = SimpleNamespace(text="x^2-1")
        await sm._CMDS["factor"](upd, make_context(FakeBot(), []))
        self.assertIn("(x - 1)*(x + 1)", upd.effective_message.reply_text.await_args.args[0])

    async def test_per_user_cooldown(self):
        for expected in ("<code>", "One calculation"):
            upd = make_update("/derive", user_id=5, chat_id=-1)
            await sm._CMDS["derive"](upd, make_context(FakeBot(), ["x^2"]))
            self.assertIn(expected, upd.effective_message.reply_text.await_args.args[0])

    async def test_queue_limit(self):
        sm._waiting = sm.MAX_QUEUE
        try:
            upd = make_update("/derive", user_id=6, chat_id=-1)
            await sm._CMDS["derive"](upd, make_context(FakeBot(), ["x^2"]))
            self.assertIn("busy", upd.effective_message.reply_text.await_args.args[0])
        finally:
            sm._waiting = 0

    def test_registration(self):
        added = []
        sm.register(SimpleNamespace(add_handler=lambda h, group=0: added.append(h)))
        names = {c for h in added for c in (getattr(h, "commands", ()) or ())}
        self.assertEqual(names, {"simplify", "factor", "derive", "integrate", "zeroes", "tangent", "area"})


if __name__ == "__main__":
    unittest.main()
