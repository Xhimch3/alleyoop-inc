#!/usr/bin/env python3
"""
Alleyoop: automatska sinhronizacija rezultata u Supabase.

Superliga e Kosovës: cita https://www.basketbolli.com/Results?leagueId=150 i rezultate uzivo (LiveStats).
Evroliga: https://api-live.euroleague.net/v1/schedules i /v1/results (neslužbeni javni API, bez ključa).

Koristi se ovako:
  python sync.py                       # jedan prolaz
  python sync.py --loop 240 --every 40 # ponavlja 4 minuta, na svakih 40 s
  python sync.py --dry-run             # samo ispise sta bi upisao
  python sync.py --file stranica.html --dry-run   # test na sacuvanoj stranici

Potrebne promenljive okruzenja (osim za --dry-run):
  SUPABASE_URL          npr. https://abcd.supabase.co
  SUPABASE_SERVICE_KEY  service_role kljuc (TAJAN, nikad u index.html)
"""
import argparse, json, os, re, sys, time, unicodedata
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

FBK_URL = "https://www.basketbolli.com/Results?leagueId=150"
TZ = ZoneInfo("Europe/Belgrade")      # vreme na sajtu je srednjoevropsko
UA = "Mozilla/5.0 (compatible; AlleyoopSync/1.0)"


def slug(s):
    s = unicodedata.normalize("NFD", str(s).lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:70] or "x"


def roman(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50}
    s = s.strip().strip(".").upper()
    tot = 0
    for i, ch in enumerate(s):
        v = vals.get(ch, 0)
        tot += -v if i + 1 < len(s) and vals.get(s[i + 1], 0) > v else v
    return tot


def to_int(t):
    t = (t or "").strip().replace("−", "-")
    return int(t) if re.fullmatch(r"-?\d+", t) else None


# ---------------------------------------------------------------- Superliga
def parse_fbk(html):
    soup = BeautifulSoup(html, "html.parser")
    games = []
    for h in soup.find_all("h2"):
        title = h.get_text(strip=True)
        m = re.match(r"JAVA\s+([IVXL]+)", title, re.I)
        if not m:
            continue
        rnd = roman(m.group(1))
        wrap = h.find_parent("div", class_="schedule-title-wrapper")
        box = wrap.find_next_sibling("div", class_="rp-wrap") if wrap else None
        if not box:
            continue
        for g in box.select(".rp-game"):
            rows = g.select(".rp-game-row")
            if len(rows) != 2:
                continue
            when = g.select_one(".rp-game-when")
            wt = when.get_text(" ", strip=True) if when else ""
            md = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", wt)
            mt = re.search(r"(\d{1,2}):(\d{2})", wt)
            if not md:
                continue
            day, mon, yr = int(md.group(1)), int(md.group(2)), int(md.group(3))
            hh, mm = (int(mt.group(1)), int(mt.group(2))) if mt else (0, 0)
            start = datetime(yr, mon, day, hh, mm, tzinfo=TZ).astimezone(timezone.utc)
            cls = " ".join(g.get("class", []))
            status = "final" if "is-final" in cls else ("live" if "live" in cls else "sched")
            href = g.get("href") or ""
            mid = re.search(r"/(\d+)/?$", href)
            v = g.select_one(".rp-game-venue")
            venue = re.sub(r"[“”]", '"', v.get_text(" ", strip=True)) if v else ""
            teams = [r.select_one("strong").get_text(strip=True) for r in rows]
            scores = [to_int(r.select_one(".rp-game-score").get_text(strip=True)) for r in rows]
            games.append({
                "league": "superliga", "round": rnd, "status": status,
                "home": teams[0], "away": teams[1],
                "home_score": scores[0], "away_score": scores[1],
                "starts_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "venue": venue, "tba": False,
                "ext_id": mid.group(1) if mid else None,
            })
    standings = []
    for r in soup.select(".table .full-width-row-wrapper"):
        if "first-row-wrapper" in r.get("class", []):
            continue
        name = r.select_one("h5")
        nums = [to_int(p.get_text(strip=True)) for p in r.select("p")]
        if not name or len(nums) < 8 or None in nums[:8]:
            continue
        # [rank, ndeshje, fitore, humbje, shenuara, pranuara, diferenca, pike]
        standings.append({"league": "superliga", "team": name.get_text(strip=True),
                          "played": nums[1], "wins": nums[2], "losses": nums[3], "diff": nums[6]})
    return games, standings


def plan_games(parsed, existing):
    """Vraca (redovi_za_upis, vesti_za_upis). 'existing' je lista redova iz baze."""
    by_key = {(e["home"], e["away"], e.get("round") or 0): e for e in existing}
    now = datetime.now(timezone.utc)
    rows, news = [], []
    for g in parsed:
        old = by_key.get((g["home"], g["away"], g["round"]))
        gid = old["id"] if old else ("ks-" + g["ext_id"] if g["ext_id"] else
                                      "su-%s-%s-%s" % (slug(g["home"]), slug(g["away"]), g["starts_at"][:10]))
        # ne vracaj unazad utakmicu koju je urednik vec postavio na LIVE/Zavrseno
        if old and old["status"] in ("live", "final") and g["status"] == "sched":
            continue
        row = {"id": gid, "league": "superliga", "status": g["status"], "home": g["home"], "away": g["away"],
               "home_score": g["home_score"], "away_score": g["away_score"], "starts_at": g["starts_at"],
               "venue": g["venue"], "round": g["round"], "tba": False}
        if old and all(old.get(k) == v for k, v in (
                ("status", row["status"]), ("home_score", row["home_score"]),
                ("away_score", row["away_score"]), ("venue", row["venue"]), ("round", row["round"]))) \
                and old.get("starts_at_norm") == row["starts_at"]:
            continue                          # nista novo
        rows.append(row)
        # vest o zavrsenoj utakmici: samo kad prelazi u "final", i nedavna je
        started = datetime.strptime(g["starts_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if (g["status"] == "final" and (not old or old["status"] != "final")
                and g["home_score"] is not None and g["away_score"] is not None
                and g["home_score"] != g["away_score"] and now - started < timedelta(days=3)):
            hw = g["home_score"] > g["away_score"]
            w, l = (g["home"], g["away"]) if hw else (g["away"], g["home"])
            ws, ls = (g["home_score"], g["away_score"]) if hw else (g["away_score"], g["home_score"])
            body = "%s e fitoi ndeshjen e javës së %d-të të Superligës së Kosovës ndaj %s me rezultat %d:%d%s." % (
                w, g["round"], l, ws, ls, (", në " + g["venue"]) if g["venue"] else "")
            news.append({"id": "result-" + gid, "league": "superliga", "auto": True, "breaking": False,
                         "title": "%s fiton %d:%d ndaj %s" % (w, ws, ls, l), "body": body,
                         "src": "basketbolli.com", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return rows, news


# ---------------------------------------------------------------- Superliga uzivo (Genius Sports LiveStats)
GS_DATA = "https://fibalivestats.dcd.shared.geniussports.com/data/%s/data.json"


def parse_live(js):
    """Cita podatke LiveStats-a. Vraca None ako oblik nije prepoznat."""
    tm = (js or {}).get("tm") or {}
    t1, t2 = tm.get("1"), tm.get("2")
    if not isinstance(t1, dict) or not isinstance(t2, dict):
        return None

    def sc(t):
        for k in ("score", "tot_sPoints"):
            v = t.get(k)
            if v not in (None, ""):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return None

    s1, s2 = sc(t1), sc(t2)
    if s1 is None or s2 is None:
        return None
    try:
        period = int(js.get("period") or 0)
    except (TypeError, ValueError):
        period = 0
    return {"n1": str(t1.get("name") or t1.get("shortName") or ""),
            "n2": str(t2.get("name") or t2.get("shortName") or ""),
            "s1": s1, "s2": s2, "period": period, "clock": str(js.get("clock") or "")}


def live_updates(parsed, existing, probe=False, now=None):
    """Za utakmice koje traju sad: status LIVE i rezultat uzivo; kad je kraj, FINAL."""
    now = now or datetime.now(timezone.utc)
    by_key = {(e["home"], e["away"], e.get("round") or 0): e for e in existing}
    rows, news = [], []
    for g in parsed:
        if not g.get("ext_id") or g["status"] == "final":
            continue
        start = datetime.strptime(g["starts_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if not (start - timedelta(minutes=10) <= now <= start + timedelta(hours=3, minutes=30)):
            continue
        old = by_key.get((g["home"], g["away"], g["round"]))
        if old and old["status"] == "final":
            continue
        try:
            raw = fetch(GS_DATA % g["ext_id"])
            if probe:
                print("=== LIVESTATS PROBA %s (prvih 600 znakova) ===" % g["ext_id"])
                print(raw[:600])
            d = parse_live(json.loads(raw))
        except Exception as e:
            print("UPOZORENJE: LiveStats za %s nije dostupan: %s" % (g["ext_id"], e), file=sys.stderr)
            continue
        if not d:
            print("UPOZORENJE: LiveStats za %s ima nepoznat oblik" % g["ext_id"], file=sys.stderr)
            continue
        if d["s1"] + d["s2"] == 0 and d["period"] == 0:
            continue                                  # jos nije pocela
        hs, as_ = d["s1"], d["s2"]
        if slug(d["n1"]) and slug(d["n1"]) in slug(g["away"]) and slug(d["n2"]) in slug(g["home"]):
            hs, as_ = as_, hs                         # ekipe zamenjene
        finished = d["period"] >= 4 and d["clock"] in ("00:00", "0:00") and hs != as_
        status = "final" if finished else "live"
        gid = old["id"] if old else ("ks-" + g["ext_id"])
        if old and old["status"] == status and old.get("home_score") == hs and old.get("away_score") == as_:
            continue
        rows.append({"id": gid, "league": "superliga", "status": status, "home": g["home"], "away": g["away"],
                     "home_score": hs, "away_score": as_, "starts_at": g["starts_at"],
                     "venue": g["venue"], "round": g["round"], "tba": False})
        print("LIVE %s %s %d:%d %s %s" % (status, g["home"], hs, as_, g["away"], "Q%d %s" % (d["period"], d["clock"])))
        if status == "final":
            hw = hs > as_
            w, l = (g["home"], g["away"]) if hw else (g["away"], g["home"])
            ws, ls = (hs, as_) if hw else (as_, hs)
            body = "%s e fitoi ndeshjen e javës së %d-të të Superligës së Kosovës ndaj %s me rezultat %d:%d%s." % (
                w, g["round"], l, ws, ls, (", në " + g["venue"]) if g["venue"] else "")
            news.append({"id": "result-" + gid, "league": "superliga", "auto": True, "breaking": False,
                         "title": "%s fiton %d:%d ndaj %s" % (w, ws, ls, l), "body": body,
                         "src": "basketbolli.com", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return rows, news


# ---------------------------------------------------------------- Supabase
class Supa:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def _req(self, method, path, body=None, extra=None):
        h = {"apikey": self.key, "Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        h.update(extra or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + "/rest/v1/" + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                t = r.read().decode()
                return json.loads(t) if t else None
        except urllib.error.HTTPError as e:
            raise RuntimeError("Supabase %s %s: %s %s" % (method, path, e.code, e.read().decode()[:300]))

    def get(self, path):
        return self._req("GET", path)

    def upsert(self, table, rows, conflict):
        if rows:
            self._req("POST", "%s?on_conflict=%s" % (table, conflict), rows,
                      {"Prefer": "resolution=merge-duplicates,return=minimal"})


def norm_existing(rows):
    for r in rows:
        try:
            r["starts_at_norm"] = datetime.fromisoformat(r["starts_at"].replace("Z", "+00:00")) \
                .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            r["starts_at_norm"] = None
    return rows


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sq,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def sync_kosovo(args, db):
    html = open(args.file, encoding="utf-8", errors="replace").read() if args.file else fetch(FBK_URL)
    games, standings = parse_fbk(html)
    if len(games) < 4 or len(standings) < 4:
        raise RuntimeError("Stranica FBK izgleda drugacije nego pre (nadjeno %d utakmica, %d redova tabele). "
                           "Nista nije upisano." % (len(games), len(standings)))
    existing = norm_existing(db.get("games?league=eq.superliga&select=*&limit=2000") if db else [])
    rows, news = plan_games(games, existing)
    try:
        lrows, lnews = live_updates(games, existing, probe=args.probe)
    except Exception as e:
        print("UPOZORENJE: uzivo nije obradjeno: %s" % e, file=sys.stderr)
        lrows, lnews = [], []
    if lrows:
        ids = {r["id"] for r in lrows}
        rows = [r for r in rows if r["id"] not in ids] + lrows
        nids = {n["id"] for n in lnews}
        news = [n for n in news if n["id"] not in nids] + lnews
    print("Superliga: %d utakmica na sajtu, %d za upis, %d vesti, %d redova tabele" %
          (len(games), len(rows), len(news), len(standings)))
    if args.dry_run or not db:
        for r in rows[:50]:
            print("  UTAKMICA", r["id"], r["status"], r["home"], r["home_score"], ":", r["away_score"], r["away"], r["starts_at"])
        for n in news:
            print("  VEST", n["title"])
        return
    db.upsert("games", rows, "id")
    db.upsert("news", news, "id")
    db.upsert("standings", standings, "league,team")


# ---------------------------------------------------------------- Evroliga
EL_SEASON = os.environ.get("EL_SEASON", "E2026")     # sezona 2026/27
EL_SCHEDULE = "https://api-live.euroleague.net/v1/schedules?seasonCode=" + EL_SEASON
EL_RESULTS = "https://api-live.euroleague.net/v1/results?seasonCode=" + EL_SEASON
EL_TZ = ZoneInfo("Europe/Berlin")                     # satnica Evrolige je srednjoevropska

# kljucna rec iz naziva u API-ju -> naziv kakav je u nasoj bazi
EL_TEAMS = [
    ("PARTIZAN", "Partizan"), ("BASKONIA", "Baskonia"), ("PARIS", "Paris Basketball"),
    ("FENERBAHCE", "Fenerbahçe"), ("MACCABI", "Maccabi Tel Aviv"), ("HAPOEL", "Hapoel Tel Aviv"),
    ("OLYMPIACOS", "Olympiacos"), ("DUBAI", "Dubai Basketball"), ("MILAN", "Olimpia Milano"),
    ("ZALGIRIS", "Žalgiris Kaunas"), ("VALENCIA", "Valencia Basket"), ("BAYERN", "FC Bayern"),
    ("REAL MADRID", "Real Madrid"), ("ZVEZDA", "Crvena Zvezda"), ("EFES", "Anadolu Efes"),
    ("VIRTUS", "Virtus Bologna"), ("PANATHINAIKOS", "Panathinaikos"), ("BARCELONA", "FC Barcelona"),
    ("ASVEL", "LDLC ASVEL"), ("BESIKTAS", "Beşiktaş"),
]


def el_team(raw):
    s = unicodedata.normalize("NFD", str(raw or "").upper())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    for key, name in EL_TEAMS:
        if key in s:
            return name
    return None


def el_start(date_s, time_s):
    """API daje datum i vreme kao tekst; probamo nekoliko uobicajenih oblika."""
    date_s, time_s = (date_s or "").strip(), (time_s or "").strip() or "20:00"
    time_s = time_s.replace(".", ":")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            d = datetime.strptime(date_s.split("T")[0] if fmt == "%Y-%m-%d" else date_s, fmt)
            break
        except ValueError:
            d = None
    if d is None:
        raise RuntimeError("Ne razumem datum iz API-ja: %r" % date_s)
    hh, mm = (time_s.split(":") + ["0"])[:2]
    local = d.replace(hour=int(hh), minute=int(re.sub(r"\D", "", mm) or 0), tzinfo=EL_TZ)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_el_schedule(xml_text):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    out, unknown = [], set()
    for it in root.iter("item"):
        g = {c.tag.lower(): (c.text or "").strip() for c in it}
        if not g.get("hometeam") or not g.get("awayteam"):
            continue
        home, away = el_team(g["hometeam"]), el_team(g["awayteam"])
        if home is None:
            unknown.add(g["hometeam"])
        if away is None:
            unknown.add(g["awayteam"])
        if home is None or away is None:
            continue
        played = g.get("played", "").lower() == "true"
        hs, as_ = to_int(g.get("homescore")), to_int(g.get("awayscore"))
        out.append({
            "home": home, "away": away,
            "round": to_int(g.get("gameday") or g.get("gamenumber") or g.get("round")) or 0,
            "starts_at": el_start(g.get("date"), g.get("startime") or g.get("time")),
            "venue": g.get("arenaname", ""), "played": played,
            "home_score": hs if played else None, "away_score": as_ if played else None,
            "code": g.get("gamecode", ""),
        })
    if unknown:
        raise RuntimeError("Nepoznati nazivi ekipa iz API-ja Evrolige: %s (dodaj ih u EL_TEAMS)" %
                           ", ".join(sorted(unknown)))
    return out


def el_standings(games):
    """Tabela se racuna iz odigranih utakmica (pobede, porazi, kos razlika)."""
    t = {name: {"played": 0, "wins": 0, "losses": 0, "diff": 0} for _, name in EL_TEAMS}
    for g in games:
        if not g["played"] or g["home_score"] is None or g["away_score"] is None:
            continue
        for me, mine, theirs in ((g["home"], g["home_score"], g["away_score"]),
                                 (g["away"], g["away_score"], g["home_score"])):
            r = t[me]
            r["played"] += 1
            r["diff"] += mine - theirs
            r["wins" if mine > theirs else "losses"] += 1
    return [{"league": "euroleague", "team": n, **v} for n, v in t.items()]


def parse_el_results(xml_text):
    """Rezultati: trazi bilo koji element koji ima hometeam, awayteam i oba rezultata."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    out = {}
    for el in root.iter():
        g = {c.tag.lower(): (c.text or "").strip() for c in el if len(c) == 0}
        if "hometeam" not in g or "awayteam" not in g:
            continue
        home, away = el_team(g["hometeam"]), el_team(g["awayteam"])
        hs = to_int(g.get("homescore") or g.get("scorehome"))
        as_ = to_int(g.get("awayscore") or g.get("scoreaway"))
        if home and away and hs is not None and as_ is not None and hs + as_ > 0:
            out[(home, away)] = (hs, as_)
    return out


def sync_euroleague(args, db):
    if args.file:                         # --file se odnosi samo na Superligu
        return
    xml_text = fetch(EL_SCHEDULE)
    if args.probe:
        print("=== EVROLIGA PROBA (prvih 1500 znakova odgovora) ===")
        print(xml_text[:1500])
    games = parse_el_schedule(xml_text)
    try:
        res_xml = fetch(EL_RESULTS)
        if args.probe:
            print("=== EVROLIGA REZULTATI PROBA (prvih 800 znakova) ===")
            print(res_xml[:800])
        scores = parse_el_results(res_xml)
    except Exception as e:
        print("UPOZORENJE: rezultati Evrolige nisu dostupni: %s" % e, file=sys.stderr)
        scores = {}
    print("Evroliga: pronadjeno %d rezultata" % len(scores))
    for g in games:
        sc = scores.get((g["home"], g["away"]))
        g["played"] = sc is not None          # zavrsena samo ako imamo pravi rezultat
        g["home_score"], g["away_score"] = sc if sc else (None, None)
    if len(games) < 100:
        raise RuntimeError("API Evrolige vratio %d utakmica (ocekivano oko 306). Nista nije upisano." % len(games))
    existing = norm_existing(db.get("games?league=eq.euroleague&select=*&limit=2000") if db else [])
    by_key = {(e["home"], e["away"]): e for e in existing}
    now = datetime.now(timezone.utc)
    rows, news = [], []
    for g in games:
        old = by_key.get((g["home"], g["away"]))
        start = datetime.strptime(g["starts_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        status = "final" if g["played"] else "sched"
        if old and old["status"] == "live" and status == "sched":
            status = "live"               # urednik je rucno stavio LIVE, ne vracaj
        if old and old["status"] == "final" and status == "sched" and old.get("home_score") is not None:
            continue                      # ne vracaj unazad pravi rezultat
        gid = old["id"] if old else "eu-%s-%s-%s" % (slug(g["home"]), slug(g["away"]), g["starts_at"][:10])
        row = {"id": gid, "league": "euroleague", "status": status, "home": g["home"], "away": g["away"],
               "home_score": g["home_score"], "away_score": g["away_score"], "starts_at": g["starts_at"],
               "venue": g["venue"], "round": g["round"], "tba": False}
        if old and old["status"] == status and old.get("home_score") == row["home_score"] \
                and old.get("away_score") == row["away_score"] and old.get("starts_at_norm") == row["starts_at"] \
                and old.get("round") == row["round"]:
            continue
        rows.append(row)
        if (status == "final" and (not old or old["status"] != "final")
                and g["home_score"] != g["away_score"] and now - start < timedelta(days=3)):
            hw = g["home_score"] > g["away_score"]
            w, l = (g["home"], g["away"]) if hw else (g["away"], g["home"])
            ws, ls = (g["home_score"], g["away_score"]) if hw else (g["away_score"], g["home_score"])
            body = "%s e fitoi ndeshjen e javës së %d-të të Evroligës ndaj %s me rezultat %d:%d%s." % (
                w, g["round"], l, ws, ls, (", në " + g["venue"]) if g["venue"] else "")
            news.append({"id": "result-" + gid, "league": "euroleague", "auto": True, "breaking": False,
                         "title": "%s fiton %d:%d ndaj %s" % (w, ws, ls, l), "body": body,
                         "src": "euroleaguebasketball.net", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
    standings = el_standings(games)
    print("Evroliga: %d utakmica u rasporedu, %d za upis, %d vesti" % (len(games), len(rows), len(news)))
    if args.dry_run or not db:
        for r in rows[:50]:
            print("  UTAKMICA", r["id"], r["status"], r["home"], r["home_score"], ":", r["away_score"], r["away"])
        for n in news:
            print("  VEST", n["title"])
        return
    db.upsert("games", rows, "id")
    db.upsert("news", news, "id")
    db.upsert("standings", standings, "league,team")


def run_once(args, db):
    errors = 0
    for name, fn in (("Superliga", sync_kosovo), ("Evroliga", sync_euroleague)):
        try:
            fn(args, db)
        except Exception as e:
            errors += 1
            print("GRESKA (%s): %s" % (name, e), file=sys.stderr)
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true", help="ispisi pocetak odgovora API-ja Evrolige")
    ap.add_argument("--loop", type=int, default=0, help="koliko sekundi da ponavlja")
    ap.add_argument("--every", type=int, default=60, help="razmak izmedju prolaza (s)")
    args = ap.parse_args()
    db = None
    if not args.dry_run:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            sys.exit("Fale SUPABASE_URL i SUPABASE_SERVICE_KEY.")
        db = Supa(url, key)
    end = time.time() + args.loop
    errs = run_once(args, db)
    while args.loop and time.time() + args.every < end:
        time.sleep(args.every)
        errs += run_once(args, db)
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
