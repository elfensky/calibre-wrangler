#!/usr/bin/env python3
"""Content-based tag classification from a controlled vocabulary (defaults/classify_vocab.txt).
Reads each sparse book's description (#comments) and asks an LLM which vocab tags apply.

  python3 classify.py [--engine apple|claude] [--limit N] [--min-tags 2]   # propose -> classify_proposal.csv
  calibre-debug -e classify.py -- --apply                                  # add proposed tags (Calibre CLOSED)

Engines (--engine):  apple = on-device Apple Foundation Models via ./afm (free, private; macOS 26+).
          claude = Anthropic (ANTHROPIC_API_KEY) | openai = OpenAI (OPENAI_API_KEY) | gemini = Google (GEMINI_API_KEY).
          --model overrides the per-engine default. Only books with < --min-tags tags AND a description are
          classified. Default: dry-run (no writes)."""
import os, sys, re, csv, json, sqlite3, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.expanduser(os.environ.get("CALIBRE_LIBRARY", ""))
if not LIB: raise SystemExit("Set CALIBRE_LIBRARY to your Calibre library folder.")
DB = os.path.join(LIB, "metadata.db")
def argval(flag, default):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default
ENGINE = argval("--engine", "apple")
APPLY = "--apply" in sys.argv
LIMIT = int(argval("--limit", "0"))
MIN_TAGS = int(argval("--min-tags", "2"))
MODEL = argval("--model", "")            # override per-engine default model

VOCAB = [l.strip() for l in open(f"{HERE}/defaults/classify_vocab.txt") if l.strip() and not l.startswith("#")]
VLOW = {v.lower(): v for v in VOCAB}

MAXTAGS = int(argval("--max-tags", "6"))

def prompt_for(desc):
    return ("Tag this fanfiction story. Choose ONLY tags from the list below that are CLEARLY supported by the "
            f"description — at most {MAXTAGS}. Be conservative: if the description is short or vague, return an "
            "empty array []. Do NOT return the whole list. Reply with ONLY a JSON array of exact-spelling tags.\n"
            f"TAGS: {', '.join(VOCAB)}\n\nDESCRIPTION:\n{desc[:1500]}\n\nJSON array (<= %d tags):" % MAXTAGS)

def parse_tags(text):
    m = re.search(r"\[.*?\]", text, re.S)
    if not m: return []
    try: arr = json.loads(m.group(0))
    except Exception: return []
    hit = [VLOW[str(t).strip().lower()] for t in arr if str(t).strip().lower() in VLOW]
    # reject dumps: a description-based pick of >2x the cap is the model echoing the list, not selecting
    if len(hit) > MAXTAGS * 2: return []
    return hit[:MAXTAGS]

# ---- engines ----
class Apple:
    def __init__(self):
        exe = f"{HERE}/afm" if os.path.exists(f"{HERE}/afm") else None
        cmd = [exe] if exe else ["swift", f"{HERE}/afm.swift"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    def ask(self, prompt):
        self.p.stdin.write(prompt.replace("\n", "") + "\n"); self.p.stdin.flush()
        return self.p.stdout.readline()
class Claude:
    def __init__(self):
        self.key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.key: raise SystemExit("claude engine needs ANTHROPIC_API_KEY (or use --engine apple).")
    def ask(self, prompt):
        import urllib.request
        body = json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 200,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        return json.load(urllib.request.urlopen(req))["content"][0]["text"]
class OpenAI:
    def __init__(self):
        self.key = os.environ.get("OPENAI_API_KEY")
        if not self.key: raise SystemExit("openai engine needs OPENAI_API_KEY.")
        self.model = MODEL or "gpt-4o-mini"
    def ask(self, prompt):
        import urllib.request
        body = json.dumps({"model": self.model, "max_tokens": 200,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.key}", "content-type": "application/json"})
        return json.load(urllib.request.urlopen(req))["choices"][0]["message"]["content"]
class Gemini:
    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self.key: raise SystemExit("gemini engine needs GEMINI_API_KEY (or GOOGLE_API_KEY).")
        self.model = MODEL or "gemini-2.0-flash"
    def ask(self, prompt):
        import urllib.request
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.key}"
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
        return json.load(urllib.request.urlopen(req))["candidates"][0]["content"]["parts"][0]["text"]

# ---- gather sparse books (read-only) ----
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); c = con.cursor()
tagn = {b: 0 for (b,) in c.execute("SELECT id FROM books")}
for (b,) in c.execute("SELECT book FROM books_tags_link"): tagn[b] = tagn.get(b, 0) + 1
desc = {b: t for b, t in c.execute("SELECT book, text FROM comments")}
def strip_html(s): return re.sub(r"<[^>]+>", " ", s or "").strip()
targets = [(b, strip_html(desc[b])) for b in tagn if tagn[b] < MIN_TAGS and desc.get(b) and strip_html(desc[b])]
titles = {b: t for b, t in c.execute("SELECT id, title FROM books")}
if LIMIT: targets = targets[:LIMIT]
print(f"engine={ENGINE}  sparse books to classify (< {MIN_TAGS} tags, has description): {len(targets)}")

eng = {"apple": Apple, "claude": Claude, "openai": OpenAI, "gemini": Gemini}[ENGINE]()
proposal = {}
for i, (b, d) in enumerate(targets):
    tags = parse_tags(eng.ask(prompt_for(d)))
    if tags: proposal[b] = tags
    if (i + 1) % 25 == 0 or i + 1 == len(targets): print(f"  {i+1}/{len(targets)} …")
with open(f"{HERE}/classify_proposal.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["book_id", "title", "added_tags"])
    for b, tags in proposal.items(): w.writerow([b, titles.get(b, ""), "; ".join(tags)])
print(f"proposed tags for {len(proposal)} books -> classify_proposal.csv")
print("samples:")
for b, tags in list(proposal.items())[:10]: print(f"   #{b} {titles.get(b,'')[:34]:34} += {', '.join(tags)}")

if APPLY:
    from calibre.library import db as DB_
    api = DB_(LIB).new_api
    chg = {}
    for b, tags in proposal.items():
        cur = api.field_for("tags", b)
        chg[b] = tuple(sorted(set(cur) | set(tags)))
    api.set_field("tags", chg)
    print(f"\nWROTE: added vocab tags to {len(chg)} books.")
else:
    print("\nDry run — review classify_proposal.csv. To write: calibre-debug -e classify.py -- --apply (Calibre closed).")
