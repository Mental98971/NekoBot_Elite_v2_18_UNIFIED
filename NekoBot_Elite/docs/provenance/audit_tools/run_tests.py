import subprocess, sys, glob, os, re, json
root = sys.argv[1]
os.chdir(root)
rows = []
for f in sorted(glob.glob("tests/test_*.py")):
    m = os.path.basename(f)[:-3]
    try:
        p = subprocess.run([sys.executable, "-m", "unittest", f"tests.{m}"], capture_output=True, text=True, timeout=600)
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        rows.append((m, "TIMEOUT", "")); continue
    ran = re.search(r"Ran (\d+) tests?", out)
    status = "OK" if re.search(r"^OK", out, re.M) else ("FAILED" if "FAILED" in out else "ERROR")
    miss = re.findall(r"ModuleNotFoundError: No module named '([^']+)'", out)
    skipped = re.search(r"skipped=(\d+)", out)
    rows.append((m, status, f"ran={ran.group(1) if ran else 0}" + (f" skipped={skipped.group(1)}" if skipped else "") + (f" missing={sorted(set(miss))}" if miss else "")))
w = max(len(r[0]) for r in rows)
ok = sum(1 for r in rows if r[1]=="OK")
for r in rows: print(f"{r[0]:<{w}}  {r[1]:<7} {r[2]}")
print("modules OK:", ok, "/", len(rows))
