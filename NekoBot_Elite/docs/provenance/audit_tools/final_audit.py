import os, sys, json, ast, io, zipfile, hashlib, collections, importlib.util, glob
from pathlib import Path
HERE=os.path.dirname(os.path.abspath(__file__))
SRC=os.environ.get("NEKO_SRC","/home/claude/src")      # folder holding the extracted input archives
B=os.environ.get("NEKO_BUILD","/home/claude/build/NekoBot_Elite")  # the tree to audit
ROOTS=json.load(open(os.path.join(HERE,"roots.json")))
def sha(b): return hashlib.sha256(b).hexdigest()
def walk_zip(data,cb,depth=0):
    try: z=zipfile.ZipFile(io.BytesIO(data))
    except Exception: return
    for i in z.infolist():
        if i.is_dir(): continue
        b=z.read(i); cb(i.filename,b)
        if i.filename.lower().endswith(".zip") and depth<4: walk_zip(b,cb,depth+1)
H={}; bfiles={}
for dp,dn,fn in os.walk(B):
    for f in fn:
        p=os.path.join(dp,f); r=os.path.relpath(p,B); b=open(p,'rb').read(); h=sha(b); bfiles[r]=h; H.setdefault(h,r)
        if f.lower().endswith(".zip"): walk_zip(b,lambda n,x: H.setdefault(sha(x),r+"::"+n))
print("build: files",len(bfiles),"| distinct contents incl. zip members",len(H))
res={}
# ---- A/B: every file of every input ----
print(f"\n{'input':8} {'files':>6} {'identical':>9} {'edited':>6} {'elsewhere':>9} {'container-zips(all members kept)':>32} {'MISSING':>7}")
for ver,rel in ROOTS.items():
    root=os.path.join(SRC,rel); n=ident=ed=moved=cz=miss=0; missl=[]
    for dp,dn,fn in os.walk(root):
        for f in fn:
            p=os.path.join(dp,f); r=os.path.relpath(p,root); n+=1; b=open(p,'rb').read(); h=sha(b)
            if r in bfiles:
                if bfiles[r]==h: ident+=1
                else: ed+=1
            elif h in H: moved+=1
            elif f.lower().endswith(".zip"):
                ok=[True]
                def cb(name,x):
                    if sha(x) not in H and not name.lower().endswith(".zip"): ok[0]=False
                walk_zip(b,cb)
                if ok[0]: cz+=1
                else: miss+=1; missl.append(r)
            else: miss+=1; missl.append(r)
    print(f"{ver:8} {n:>6} {ident:>9} {ed:>6} {moved:>9} {cz:>32} {miss:>7}")
    res[ver]=dict(files=n,identical=ident,edited=ed,elsewhere=moved,containers=cz,missing=miss,missing_list=missl[:10])
# ---- C: definitions ----
SCOPES=("bot","training","scripts","tests","dashboard")
def defs_of(path):
    try: t=ast.parse(open(path,encoding="utf-8").read())
    except Exception: return None
    out=[]
    def walk(body,prefix):
        for n in body:
            if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                q=prefix+n.name; out.append(q)
                if isinstance(n,ast.ClassDef): walk(n.body,q+".")
                else: walk([x for x in ast.walk(n) if x is not n and isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))],q+".<l>.")
            elif isinstance(n,(ast.If,ast.Try,ast.With,ast.For)):
                for fld in ("body","orelse","finalbody"): walk(getattr(n,fld,[]),prefix)
                for h in getattr(n,"handlers",[]): walk(h.body,prefix)
    walk(t.body,""); return out
def tree_defs(root):
    d={}
    for top in SCOPES:
        for dp,dn,fn in os.walk(os.path.join(root,top)):
            dn[:]=[x for x in dn if x!="__pycache__"]
            for f in fn:
                if f.endswith(".py"):
                    x=defs_of(os.path.join(dp,f))
                    if x is not None: d[os.path.relpath(os.path.join(dp,f),root)]=set(x)
    return d
bd=tree_defs(B); allq=set(q for s in bd.values() for q in s)
print(f"\n{'input':8} {'defs':>6} {'same file':>9} {'moved':>6} {'MISSING':>7}")
miss_union=collections.defaultdict(list)
for ver,rel in ROOTS.items():
    d=tree_defs(os.path.join(SRC,rel)); n=ok=mv=ms=0
    for f,s in d.items():
        for q in s:
            n+=1
            if f in bd and q in bd[f]: ok+=1
            elif q in allq: mv+=1
            else: ms+=1; miss_union[(f,q)].append(ver)
    print(f"{ver:8} {n:>6} {ok:>9} {mv:>6} {ms:>7}")
    res[ver].update(defs=n,defs_same=ok,defs_moved=mv,defs_missing=ms)
print("distinct missing definitions:",len(miss_union))
for (f,q),v in sorted(miss_union.items()): print("  ",f+"::"+q,"[%s..%s]"%(v[0],v[-1]))
# ---- D: commands ----
spec=importlib.util.spec_from_file_location("gcr",B+"/scripts/gen_command_reference.py"); gcr=importlib.util.module_from_spec(spec); spec.loader.exec_module(gcr)
def cmds(root):
    gcr.ROOT=Path(root); gcr.PLUGINS=gcr.ROOT/"bot"/"plugins"
    try: return {c for rows in gcr.collect().values() for c,_ in rows}
    except BaseException:
        out=set()
        for p in (Path(root)/"bot"/"plugins").glob("*.py"):
            t=ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(t):
                if isinstance(n,ast.Call) and getattr(n.func,"id","")=="CommandHandler" and n.args:
                    a=n.args[0]
                    if isinstance(a,ast.Constant) and isinstance(a.value,str): out.add(a.value)
                    elif isinstance(a,(ast.List,ast.Tuple)): out|={e.value for e in a.elts if isinstance(e,ast.Constant)}
        return out
bc=cmds(B); print("\ncommands in build:",len(bc))
tot=0
for ver,rel in ROOTS.items():
    s=cmds(os.path.join(SRC,rel)); m=sorted(s-bc); tot+=len(m); res[ver].update(commands=len(s),commands_missing=len(m))
print("commands missing from the build across all 15 inputs:",tot)
# ---- E: structured data (strings) incl. preserved superseded copies ----
EXT=(".json",".jsonl",".txt",".csv",".tsv",".yaml",".yml")
def leaves(path):
    out=set()
    try: raw=open(path,encoding="utf-8").read()
    except Exception: return None
    def add(o):
        if isinstance(o,str):
            if len(o)>=2: out.add(o)
        elif isinstance(o,dict):
            for k,v in o.items():
                if isinstance(k,str) and len(k)>=2: out.add(k)
                add(v)
        elif isinstance(o,list):
            for v in o: add(v)
    if path.endswith(".jsonl"):
        for ln in raw.splitlines():
            ln=ln.strip()
            if ln:
                try: add(json.loads(ln))
                except Exception: out.add(ln)
    elif path.endswith(".json"):
        try: add(json.loads(raw))
        except Exception: out.update(l.strip() for l in raw.splitlines() if l.strip())
    else: out.update(l.strip() for l in raw.splitlines() if l.strip())
    return out
SUP=B+"/docs/provenance/superseded/derived"
def preserved_leaves(rel):
    stem,ext=os.path.splitext(rel.replace("/","__")); s=set()
    for p in glob.glob(f"{SUP}/{glob.escape(stem)}.*{ext}"):
        x=leaves(p)
        if x: s|=x
    return s
cache={}
print(f"\n{'input':8} {'edited structured files':>24} {'old strings absent from live file':>34} {'...and not preserved elsewhere':>31}")
for ver,rel in ROOTS.items():
    root=os.path.join(SRC,rel); ne=na=nl=0
    for dp,dn,fn in os.walk(root):
        for f in fn:
            if not f.endswith(EXT): continue
            p=os.path.join(dp,f); r=os.path.relpath(p,root); bp=os.path.join(B,r)
            if not os.path.isfile(bp) or sha(open(p,'rb').read())==bfiles[r]: continue
            ne+=1; old=leaves(p)
            if r not in cache: cache[r]=(leaves(bp),preserved_leaves(r))
            new,pres=cache[r]
            if old is None or new is None: continue
            gone=old-new
            if gone:
                na+=1
                if gone-pres: nl+=1; print("     NOT PRESERVED:",ver,r,len(gone-pres))
    print(f"{ver:8} {ne:>24} {na:>34} {nl:>31}")
    res[ver].update(struct_edited=ne,struct_absent=na,struct_unpreserved=nl)
json.dump(res,open(os.path.join(HERE,"final_audit.json"),"w"),indent=1)
