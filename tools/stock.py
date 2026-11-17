#!/usr/bin/env python3
"""Fills the storehouse: builds each piece from its recipe, checks it, publishes it.

A recipe (recipes/<name>.json) says where a piece comes from and what it must
turn out to be: its origin with that file's sum, which files to take from it,
which small files of our own to add, and the size and sum of the finished
piece. The finished piece is built the same way on any machine, byte for byte,
so its sum can be written down before it is ever built, and a build that comes
out different is refused.

Each piece is published as the files of one release, tagged with its name.
Files above the host's per-file cap are cut into numbered parts; joined back
in order, the parts are the very file the sum names.

    python3 tools/stock.py list               what the recipes hold
    python3 tools/stock.py build NAME         fetch the origin, build, check (nothing leaves)
    python3 tools/stock.py upload NAME        build, check, publish what the release lacks
    python3 tools/stock.py check NAME         download what is published, check every sum
    python3 tools/stock.py index              write index.json from the recipes

upload takes --to OWNER/REPO to publish somewhere other than this storehouse
(a private copy of a piece the index only links to). build and upload take --from DIR: an origin file already on this machine, under
its own name in DIR, is used instead of downloading it, once its sum agrees.

Needs Python 3 only; upload also needs the gh tool, logged in. Work files go
to ./work (several GB for a large model)."""
import hashlib, json, os, subprocess, sys, tarfile, urllib.request, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECIPES = os.path.join(ROOT, "recipes")
WORK = os.path.join(ROOT, "work")
INDEX = os.path.join(ROOT, "index.json")
# Every release file must stay under 2 GiB; STOCK_PART_MB makes parts smaller for a dry test.
PART = int(os.environ.get("STOCK_PART_MB", "1900")) * 1024 * 1024
STAMP = (1980, 1, 1, 0, 0, 0)
LOCAL = None
TO = None


def say(*words):
    print(*words, flush=True)


def fail(why):
    say("FAILED:", why)
    sys.exit(1)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def recipe(name):
    path = os.path.join(RECIPES, name + ".json")
    if not os.path.exists(path):
        fail("no recipe " + path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def names():
    return sorted(n[:-5] for n in os.listdir(RECIPES) if n.endswith(".json"))


def repo():
    with open(INDEX, encoding="utf-8") as f:
        return json.load(f)["repo"]


def fetch(url, path, size=0):
    """Downloads with resume: a cut-off run continues where it stopped."""
    have = os.path.getsize(path) if os.path.exists(path) else 0
    if size and have == size:
        return
    if size and have > size:
        os.remove(path)
        have = 0
    req = urllib.request.Request(url, headers={"Range": "bytes=%d-" % have} if have else {})
    with urllib.request.urlopen(req) as r, open(path, "ab" if have and r.status == 206 else "wb") as out:
        got = have if r.status == 206 else 0
        total = int(r.headers.get("Content-Length", 0)) + got
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            out.write(block)
            got += len(block)
            if total:
                print("\r  %s: %d%%" % (os.path.basename(path), got * 100 // total), end="", flush=True)
    print()


def published(o):
    """Size and sum of a file as its model host states them, for an origin the recipe names no sum for."""
    h = o["host"]
    url = "https://huggingface.co/api/models/%s/tree/%s" % (h["repo"], h["rev"])
    with urllib.request.urlopen(url) as r:
        for f in json.load(r):
            if f.get("path") == h["path"] and f.get("lfs"):
                return f["lfs"]["size"], f["lfs"]["oid"]
    fail("the host does not list " + h["path"])


def origin(r):
    """The origin file in ./work: taken from --from, else fetched; refused if its sum is wrong."""
    o = r["origin"]
    size, want = o.get("bytes"), o.get("sha256")
    if not want:
        size, want = published(o)
        say("the host states", size, "bytes", want)
    # A piece kept as it came (a model file) is the origin itself, under its own name.
    path = os.path.join(WORK, r["file"] if r.get("pack") == "none" else r["name"] + ".origin")
    mine = os.path.join(LOCAL, os.path.basename(o["url"])) if LOCAL else None
    if not os.path.exists(path) and mine and os.path.exists(mine) and os.path.getsize(mine) == size:
        say("using", mine)
        try:
            os.link(mine, path)
        except OSError:
            import shutil
            shutil.copyfile(mine, path)
    if not (os.path.exists(path) and os.path.getsize(path) == size):
        say("fetching", o["url"])
        fetch(o["url"], path, size)
    if sha256(path) != want:
        os.remove(path)
        fail("the origin of %s is not what its host or recipe names (removed; run again)" % r["name"])
    return path


def entry(info, data):
    z = zipfile.ZipInfo(info, STAMP)
    z.compress_type = zipfile.ZIP_STORED
    z.create_system = 3
    z.external_attr = 0o644 << 16
    return z, data


def pack(r, src):
    """The finished piece: the files taken from the origin, then our own, in the recipe's order."""
    out = os.path.join(WORK, r["file"])
    if r.get("pack") == "none":
        return src
    files = []
    with tarfile.open(src, "r:*") as t:
        for n in r["take"]:
            m = t.getmember(r["origin"]["root"] + "/" + n)
            files.append(entry(n, t.extractfile(m).read()))
    for n in r.get("add", []):
        with open(os.path.join(RECIPES, r["name"], n), "rb") as f:
            files.append(entry(n, f.read()))
    for n in r.get("licenses", []):
        with open(os.path.join(ROOT, "licenses", n), "rb") as f:
            files.append(entry("LICENSE-" + n, f.read()))
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w") as z:
        for info, data in files:
            z.writestr(info, data)
    os.replace(tmp, out)
    return out


def cut(path):
    """Cuts a file into numbered parts; a file below the cap stays whole."""
    if os.path.getsize(path) < PART:
        return [path]
    parts, n = [], 1
    with open(path, "rb") as f:
        while True:
            block = f.read(PART)
            if not block:
                break
            p = "%s.part%d" % (path, n)
            with open(p, "wb") as out:
                out.write(block)
            parts.append(p)
            n += 1
    return parts


def build(name):
    r = recipe(name)
    os.makedirs(WORK, exist_ok=True)
    piece = pack(r, origin(r))
    size, total = os.path.getsize(piece), sha256(piece)
    want = r.get("expect", {})
    if want and (size != want["bytes"] or total != want["sha256"]):
        fail("%s came out %d bytes, %s; the recipe expects %d bytes, %s"
             % (name, size, total, want["bytes"], want["sha256"]))
    files = cut(piece)
    if len(files) > 1:
        joined = hashlib.sha256()
        for p in files:
            with open(p, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    joined.update(block)
        if joined.hexdigest() != total:
            fail("the parts of %s do not join back into it" % name)
    say("built", name, size, "bytes", total, "in %d file(s)" % len(files))
    return r, piece, files


def gh(*args, quiet=False):
    p = subprocess.run(["gh"] + list(args), capture_output=True, text=True)
    if p.returncode != 0 and not quiet:
        fail("gh " + " ".join(args) + "\n" + p.stderr)
    return p


def upload(name):
    if recipe(name).get("copy") == "private" and not TO:
        fail(name + " is only linked from here; its own copy goes elsewhere: add --to OWNER/REPO")
    r, piece, files = build(name)
    where = TO or repo()
    if gh("release", "view", name, "--repo", where, quiet=True).returncode != 0:
        notes = "%s, version %d. %s Licence: %s. Size and sum: index.json." % (
            r["id"], r["version"], r.get("about", ""), r["license"])
        gh("release", "create", name, "--repo", where, "--title", name, "--notes", notes)
    have = json.loads(gh("release", "view", name, "--repo", where, "--json", "assets").stdout)["assets"]
    have = {a["name"]: a["size"] for a in have}
    for p in files:
        base = os.path.basename(p)
        if have.get(base) == os.path.getsize(p):
            say("already there:", base)
            continue
        say("uploading", base)
        gh("release", "upload", name, p, "--repo", where, "--clobber")
    for p in files:
        if p != piece:
            os.remove(p)
    say("published", name)


def check(name):
    """Downloads what the index points at, as a phone would, and checks every sum."""
    with open(INDEX, encoding="utf-8") as f:
        idx = json.load(f)
    e = next((m for m in idx["modules"] if m["file"].rsplit(".", 1)[0] == name or m.get("tag") == name), None)
    if e is None:
        fail("index.json has nothing for " + name)
    os.makedirs(os.path.join(WORK, "check"), exist_ok=True)
    s = e["sources"][0]
    total = hashlib.sha256()
    for i, p in enumerate(s.get("parts") or [s]):
        path = os.path.join(WORK, "check", "%s.%d" % (name, i + 1))
        say("downloading", p["url"])
        fetch(p["url"], path)
        if "sha256" in p and sha256(path) != p["sha256"]:
            fail("part %d of %s differs" % (i + 1, name))
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                total.update(block)
        os.remove(path)
    if total.hexdigest() != e["sha256"]:
        fail(name + " as published differs from index.json")
    say("check ok:", name)


def index():
    """index.json from the recipes: what a phone may fetch, and from where."""
    where = repo()
    mods = []
    for n in names():
        r = recipe(n)
        want = r.get("expect")
        if not want and r.get("pack") == "none" and r["origin"].get("sha256") and r["origin"]["bytes"] < PART:
            want = {"bytes": r["origin"]["bytes"], "sha256": r["origin"]["sha256"]}
        if not want:
            say("skipped (no expected sum yet):", n)
            continue
        base = "https://github.com/%s/releases/download/%s/%s" % (where, n, r["file"])
        if r.get("copy") == "private":
            here = {"url": r["origin"]["url"]}
        elif want["bytes"] < PART:
            here = {"url": base}
        else:
            here = {"parts": [{"url": "%s.part%d" % (base, i + 1), "sha256": s}
                              for i, s in enumerate(want["parts"])]}
        e = {"id": r["id"], "version": r["version"], "kind": r["kind"], "tag": n, "file": r["file"],
             "bytes": want["bytes"], "sha256": want["sha256"], "license": r["license"],
             "unpack": "none" if r.get("pack") == "none" else "zip",
             "sources": [here]}
        for k in ("tier", "needs", "group", "role"):
            if k in r:
                e[k] = r[k]
        if r.get("pack") == "none" and r["origin"].get("url") and r.get("copy") != "private":
            e["sources"].append({"url": r["origin"]["url"]})
        mods.append(e)
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump({"format": 1, "repo": where, "modules": mods}, f, indent=1, ensure_ascii=True)
        f.write("\n")
    say("index.json:", len(mods), "module(s)")


def main():
    global LOCAL, TO
    if "--to" in sys.argv:
        i = sys.argv.index("--to")
        if i + 1 >= len(sys.argv) or "/" not in sys.argv[i + 1]:
            fail("--to needs OWNER/REPO")
        TO = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    if "--from" in sys.argv:
        i = sys.argv.index("--from")
        if i + 1 >= len(sys.argv) or not os.path.isdir(sys.argv[i + 1]):
            fail("--from needs a folder")
        LOCAL = os.path.abspath(sys.argv[i + 1])
        del sys.argv[i:i + 2]
    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "build", "upload", "check", "index"):
        print(__doc__)
        sys.exit(2)
    cmd = sys.argv[1]
    if cmd == "list":
        for n in names():
            r = recipe(n)
            say("%-14s %-6s %s" % (n, r["kind"], r["license"]))
        return
    if cmd == "index":
        index()
        return
    if len(sys.argv) != 3:
        fail("say which piece, e.g.: python3 tools/stock.py %s hear-ru-1" % cmd)
    {"build": build, "upload": upload, "check": check}[cmd](sys.argv[2])


if __name__ == "__main__":
    main()
