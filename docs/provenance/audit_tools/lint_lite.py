"""F401 (unused import), F811 (redefined unused name at module level), F841 (unused local) approximation, per file. Prints findings as 'file:line CODE name'."""
import ast, sys, os, collections
ROOT=sys.argv[1]; out=set()
for top in ("bot","dashboard","tests","scripts","training"):
    for dp,dn,fn in os.walk(os.path.join(ROOT,top)):
        dn[:]=[d for d in dn if d!="__pycache__"]
        for f in fn:
            if not f.endswith(".py"): continue
            p=os.path.join(dp,f); rel=os.path.relpath(p,ROOT); src=open(p,encoding="utf-8").read()
            t=ast.parse(src)
            used=set()
            for n in ast.walk(t):
                if isinstance(n,ast.Name): used.add(n.id)
                elif isinstance(n,ast.Attribute):
                    b=n
                    while isinstance(b,ast.Attribute): b=b.value
                    if isinstance(b,ast.Name): used.add(b.id)
            # names in __all__ and in string annotations / type comments
            alls=set()
            for n in t.body:
                if isinstance(n,ast.Assign) and any(getattr(x,"id","")=="__all__" for x in n.targets) and isinstance(n.value,(ast.List,ast.Tuple)):
                    alls={e.value for e in n.value.elts if isinstance(e,ast.Constant)}
            strs=" ".join(n.value for n in ast.walk(t) if isinstance(n,ast.Constant) and isinstance(n.value,str) and len(n.value)<200)
            is_init=f=="__init__.py"
            for n in t.body:
                imps=[]
                if isinstance(n,ast.Import): imps=[(a.asname or a.name.split(".")[0],a.name) for a in n.names]
                elif isinstance(n,ast.ImportFrom) and n.module!="__future__": imps=[(a.asname or a.name,a.name) for a in n.names if a.name!="*"]
                for nm,orig in imps:
                    if nm not in used and nm not in alls and not is_init and not (nm in strs):
                        out.add(f"{rel}:{n.lineno} F401 {nm}")
            # nested imports in try/except at module level
            for n in ast.walk(t):
                if isinstance(n,ast.Try):
                    for s in n.body:
                        if isinstance(s,(ast.Import,ast.ImportFrom)):
                            for a in s.names:
                                nm=(a.asname or a.name.split(".")[0]) if isinstance(s,ast.Import) else (a.asname or a.name)
                                if nm!="*" and nm not in used and nm not in alls and not is_init: out.add(f"{rel}:{s.lineno} F401 {nm} (try)")
            # F841: simple local assigned and never used
            for fnode in ast.walk(t):
                if isinstance(fnode,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    assigned={}; loads=set()
                    for n in ast.walk(fnode):
                        if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load): loads.add(n.id)
                    for n in ast.walk(fnode):
                        if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name): assigned.setdefault(n.targets[0].id,n.lineno)
                    glob=set()
                    for n in ast.walk(fnode):
                        if isinstance(n,(ast.Global,ast.Nonlocal)): glob|=set(n.names)
                    for nm,ln in assigned.items():
                        if nm not in loads and nm not in glob and not nm.startswith("_"): out.add(f"{rel}:{ln} F841 {nm} in {fnode.name}")
for l in sorted(out): print(l)
