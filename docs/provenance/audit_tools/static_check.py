"""Mini static checker: syntax, undefined names, unresolved project imports, duplicate top-level defs. Usage: static_check.py ROOT"""
import ast, sys, os, builtins, json, collections

ROOT = sys.argv[1]
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__builtins__", "__path__", "__loader__", "__debug__", "__annotations__", "__class__"}

def py_files():
    for top in ("bot", "training", "scripts", "tests", "dashboard"):
        base = os.path.join(ROOT, top)
        for dp, dn, fn in os.walk(base):
            dn[:] = [d for d in dn if d not in ("__pycache__", "_private")]
            for f in fn:
                if f.endswith(".py"): yield os.path.join(dp, f)
    for f in os.listdir(ROOT):
        if f.endswith(".py"): yield os.path.join(ROOT, f)

class Scope:
    def __init__(self, kind, parent=None):
        self.kind, self.parent, self.names = kind, parent, set()
        self.globals_decl, self.nonlocal_decl = set(), set()

def target_names(t, out):
    if isinstance(t, ast.Name): out.add(t.id)
    elif isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts: target_names(e, out)
    elif isinstance(t, ast.Starred): target_names(t.value, out)

def collect_bindings(body_nodes, scope, module_scope):
    """Collect names bound directly in this scope (not descending into nested function/class/lambda/comprehension scopes)."""
    stack = list(body_nodes)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.names.add(n.name)
            continue
        if isinstance(n, ast.ClassDef):
            scope.names.add(n.name)
            continue
        if isinstance(n, ast.Lambda): continue
        if isinstance(n, ast.Import):
            for a in n.names: scope.names.add((a.asname or a.name.split(".")[0]))
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "*": scope.names.add("*")
                else: scope.names.add(a.asname or a.name)
        elif isinstance(n, ast.Global):
            for x in n.names: scope.globals_decl.add(x); module_scope.names.add(x)
        elif isinstance(n, ast.Nonlocal):
            for x in n.names: scope.nonlocal_decl.add(x)
        elif isinstance(n, (ast.Assign,)):
            for t in n.targets: target_names(t, scope.names)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            target_names(n.target, scope.names)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            target_names(n.target, scope.names)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None: target_names(it.optional_vars, scope.names)
        elif isinstance(n, ast.ExceptHandler):
            if n.name: scope.names.add(n.name)
        elif isinstance(n, ast.NamedExpr):
            target_names(n.target, scope.names)
        elif isinstance(n, ast.Delete):
            pass
        elif hasattr(ast, "MatchAs") and isinstance(n, ast.MatchAs):
            if n.name: scope.names.add(n.name)
        elif hasattr(ast, "MatchStar") and isinstance(n, ast.MatchStar):
            if n.name: scope.names.add(n.name)
        elif hasattr(ast, "MatchMapping") and isinstance(n, ast.MatchMapping):
            if n.rest: scope.names.add(n.rest)
        for c in ast.iter_child_nodes(n):
            stack.append(c)

def check_file(path):
    src = open(path, encoding="utf-8").read()
    try:
        tree = ast.parse(src, path)
    except SyntaxError as e:
        return [("SYNTAX", e.lineno, str(e))]
    problems = []
    mod = Scope("module")
    collect_bindings(tree.body, mod, mod)
    star = "*" in mod.names

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            for d in list(args.defaults) + [x for x in args.kw_defaults if x is not None]:
                visit(d, scope)
            if not isinstance(node, ast.Lambda):
                for dec in node.decorator_list: visit(dec, scope)
                for a in args.posonlyargs + args.args + args.kwonlyargs:
                    if a.annotation is not None: visit(a.annotation, scope)
                for a in (args.vararg, args.kwarg):
                    if a is not None and a.annotation is not None: visit(a.annotation, scope)
                if node.returns is not None: visit(node.returns, scope)
            fs = Scope("function", scope)
            for a in args.posonlyargs + args.args + args.kwonlyargs: fs.names.add(a.arg)
            if args.vararg: fs.names.add(args.vararg.arg)
            if args.kwarg: fs.names.add(args.kwarg.arg)
            body = [node.body] if isinstance(node, ast.Lambda) else node.body
            collect_bindings(body, fs, mod)
            for b in body: visit(b, fs)
            return
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list: visit(dec, scope)
            for b in node.bases: visit(b, scope)
            for k in node.keywords: visit(k.value, scope)
            cs = Scope("class", scope)
            collect_bindings(node.body, cs, mod)
            for b in node.body: visit(b, cs)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            cs = Scope("comp", scope)
            for g in node.generators: target_names(g.target, cs.names)
            for g in node.generators:
                visit(g.iter, cs if g is not node.generators[0] else scope)
                for i in g.ifs: visit(i, cs)
            if isinstance(node, ast.DictComp):
                visit(node.key, cs); visit(node.value, cs)
            else:
                visit(node.elt, cs)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            s, first = scope, True
            found = False
            while s is not None:
                if s.kind != "class" or first:
                    if name in s.names: found = True; break
                first = False
                s = s.parent
            if not found and name not in BUILTINS and not star:
                problems.append(("UNDEFINED", node.lineno, name))
        for c in ast.iter_child_nodes(node):
            visit(c, scope)

    for b in tree.body: visit(b, mod)
    # duplicate top-level function/class definitions
    seen = collections.defaultdict(list)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            seen[n.name].append(n.lineno)
    for k, v in seen.items():
        if len(v) > 1: problems.append(("DUPDEF", v[1], f"{k} defined at lines {v}"))
    return problems

def module_exports(modpath):
    """top-level bound names of a module file (or None if it has star imports / __getattr__)."""
    try:
        tree = ast.parse(open(modpath, encoding="utf-8").read())
    except Exception:
        return None
    s = Scope("module"); collect_bindings(tree.body, s, s)
    if "*" in s.names or "__getattr__" in s.names: return None
    return s.names

def resolve_module(dotted):
    parts = dotted.split(".")
    p = os.path.join(ROOT, *parts)
    if os.path.isfile(p + ".py"): return p + ".py", False
    if os.path.isdir(p):
        init = os.path.join(p, "__init__.py")
        return (init if os.path.isfile(init) else None), True
    return "MISSING", False

def check_imports(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    problems = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in ("bot", "training", "scripts", "dashboard"):
                    r, _ = resolve_module(a.name)
                    if r == "MISSING": problems.append(("IMPORT", n.lineno, f"import {a.name}"))
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            if n.module.split(".")[0] in ("bot", "training", "scripts", "dashboard"):
                r, isdir = resolve_module(n.module)
                if r == "MISSING":
                    problems.append(("IMPORT", n.lineno, f"from {n.module} import ..."))
                    continue
                exports = module_exports(r) if r else set()
                for a in n.names:
                    if a.name == "*": continue
                    sub_py = os.path.join(ROOT, *n.module.split("."), a.name + ".py")
                    sub_dir = os.path.join(ROOT, *n.module.split("."), a.name)
                    if os.path.isfile(sub_py) or os.path.isdir(sub_dir): continue
                    if exports is None: continue
                    if a.name not in exports:
                        problems.append(("IMPORTNAME", n.lineno, f"from {n.module} import {a.name}"))
    return problems

total = collections.Counter(); out = {}
for f in sorted(py_files()):
    rel = os.path.relpath(f, ROOT)
    pr = check_file(f)
    if not pr or pr[0][0] != "SYNTAX":
        pr += check_imports(f)
    if pr:
        out[rel] = pr
        for p in pr: total[p[0]] += 1
nfiles = sum(1 for _ in py_files())
print(f"files scanned: {nfiles}; files with findings: {len(out)}; by kind: {dict(total)}")
json.dump(out, open(sys.argv[2] if len(sys.argv) > 2 else "/dev/null", "w"), indent=1)
for rel, pr in out.items():
    for p in pr[:12]: print(f"  {rel}:{p[1]}: {p[0]} {p[2]}")
