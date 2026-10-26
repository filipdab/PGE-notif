#!/usr/bin/env python3
"""
pge_wylaczenia.py — powiadomienia o planowanych wyłączeniach prądu (PGE Dystrybucja)

Co 30 minut (systemd timer / cron) pyta publiczne API, z którego korzysta strona
pgedystrybucja.pl/wylaczenia, i wysyła e-mail (opcjonalnie SMS) DZIEŃ PRZED
planowanym wyłączeniem na wskazanych ulicach. Każde wyłączenie zgłaszane jest raz.

Źródło danych (nieoficjalne, to samo co strona PGE):
  https://falcon.gkpge.pl/public/teryt-city   – symbol TERYT miejscowości
  https://falcon.gkpge.pl/public/street       – symbol TERYT ulicy
  https://power-outage.gkpge.pl/api/power-outage – lista wyłączeń
  type: 2 = planowane, 1 = awaria; czasy startAt/stopAt w czasie lokalnym (PL)

Konfiguracja — plik .env obok skryptu (KLUCZ=wartość, nie trafia do gita):
  PGE_ADRESY               – wymagane, np.  Miasto: Ulica1, Ulica2; Inna Miejscowość: Ulica3
  PGE_POWIAT               – opcjonalnie, gdy w Polsce jest kilka miejscowości o tej nazwie
  EMAIL_FROM, EMAIL_PASSWORD, SMTP_SERVER, SMTP_PORT – poczta (np. Gmail + hasło aplikacji)

Dane poczty mogą też pochodzić z innego skryptu na tym samym komputerze: skrypt czyta
pierwszy istniejący plik z SHARED_ENV_FILES / stałe z SHARED_PY_FILES, crontab
i /etc/environment (zmienne już ustawione w środowisku mają pierwszeństwo).
Inny plik .env wskażesz zmienną PGE_ENV_FILE.

Opcjonalnie:
  PGE_MAIL_TO              – adresat(ci) powiadomień, po przecinku (domyślnie EMAIL_FROM)
  SMSAPI_TOKEN, SMS_TO     – SMS przez SMSAPI.pl
  PGE_STATE_FILE           – plik stanu (domyślnie ~/.local/state/pge_wylaczenia.json)

Użycie:
  pge_wylaczenia.py              – normalny przebieg (do timera)
  pge_wylaczenia.py --list       – pokaż wszystkie nadchodzące wyłączenia, nic nie wysyłaj
  pge_wylaczenia.py --dry-run    – pokaż, co zostałoby wysłane, nie wysyłaj i nie zapisuj stanu
  pge_wylaczenia.py --test-mail  – wyślij testowy e-mail
  pge_wylaczenia.py --list --zrzut – zapisz surowe dane PGE do ~/pge_zrzut_*.json (diagnostyka)
  pge_wylaczenia.py --test-powiadomienie – wyślij przykładowe powiadomienie o wyłączeniu (jutro 8–14)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

# ───────────────────────── KONFIGURACJA ─────────────────────────
# Adresy podajesz w pliku .env obok skryptu (nie w kodzie), np.:
#   PGE_ADRESY=Miasto: Ulica1, Ulica2; Inna Miejscowość: Ulica3
#   PGE_POWIAT=nazwa_powiatu   (opcjonalnie — gdy w Polsce jest kilka miejscowości o tej nazwie)
# Rejon energetyczny obejmuje zwykle też okoliczne gminy: wpisz miejscowość, w której
# ulica faktycznie leży (wg TERYT), a nie najbliższe miasto.
NOTIFY_DAYS_BEFORE = 1        # 1 = dzień przed
INCLUDE_FAILURES = True       # powiadamiaj o awariach (type=1) — przy sprawdzaniu co 30 min z opóźnieniem do ~30 min
QUIET_HOURS = (22, 7)         # planowane wyłączenia nie są wysyłane między 22:00 a 7:00 (awarie — zawsze)

FALCON = "https://falcon.gkpge.pl/public"
OUTAGE_API = "https://power-outage.gkpge.pl/api/power-outage"
TZ = ZoneInfo("Europe/Warsaw")
HTTP_TIMEOUT = 90             # API PGE bywa wolne
HTTP_RETRIES = 3
STATE_FILE = Path(os.environ.get(
    "PGE_STATE_FILE", Path.home() / ".local/state/pge_wylaczenia.json"))
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "pge-wylaczenia/1.0 (skrypt prywatny)",
}

# Pliki .env z danymi poczty (pierwszy istniejący wygrywa) — wspólne z rybackie_prognozy
SHARED_ENV_FILES = [
    "~/rybackie-prognozy/.env",
    "~/rybackie_prognozy/.env",
]
# Skrypty, z których można odczytać stałe EMAIL_FROM / SMTP_* (bez ich uruchamiania)
SHARED_PY_FILES = [
    "~/rybackie_prognozy.py",
    "~/na_ryby2.py",
    "~/rybackie-prognozy/rybackie_prognozy.py",
]
LOCAL_ENV_FILE = Path(__file__).resolve().parent / ".env"   # własne dodatki PGE (SMS, PGE_MAIL_TO)

AUTHOR_FOOTER = ("Autor aplikacji: Filip Dąbrowski, filip@dabrowski.in\n"
                 "pge_wylaczenia.py — powiadomienia o wyłączeniach prądu PGE Dystrybucja")
# ─────────────────────────────────────────────────────────────────


def load_env_file(path: Path) -> bool:
    """Wczytaj KEY=VALUE z pliku .env, nie nadpisując zmiennych już ustawionych."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)
    return True


MAIL_KEYS = {   # nazwa docelowa ← nazwy, pod którymi może występować w innych skryptach
    "EMAIL_FROM": ("EMAIL_FROM", "EMAIL_USER", "SMTP_USER", "SENDER_EMAIL", "EMAIL_SENDER"),
    "EMAIL_PASSWORD": ("EMAIL_PASSWORD", "SMTP_PASSWORD", "SMTP_PASS", "EMAIL_PASS"),
    "SMTP_SERVER": ("SMTP_SERVER", "SMTP_HOST"),
    "SMTP_PORT": ("SMTP_PORT",),
}


def _set_aliases(values: dict) -> list[str]:
    """Ustaw brakujące EMAIL_* z wartości znalezionych pod dowolną nazwą-aliasem."""
    got = []
    for target, names in MAIL_KEYS.items():
        if os.environ.get(target):
            continue
        for n in names:
            v = values.get(n)
            if v not in (None, ""):
                os.environ[target] = str(v)
                got.append(target)
                break
    return got


def load_from_python(path: Path) -> list[str]:
    """Odczytaj stałe (EMAIL_FROM = "...") z innego skryptu — przez AST, bez uruchamiania go."""
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, PermissionError, SyntaxError, UnicodeDecodeError):
        return []
    values = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, (str, int))):
            values[node.targets[0].id] = node.value.value
    return _set_aliases(values)


def _scan_kv_text(text: str) -> dict:
    """Wyłuskaj KLUCZ=wartość (także 'KLUCZ=x /usr/bin/python3 ...' w linii crona)."""
    names = {n for ns in MAIL_KEYS.values() for n in ns}
    values = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for m in re.finditer(r"(?:^|\s)(?:export\s+)?([A-Z_]+)=(\"[^\"]*\"|'[^']*'|\S+)", line):
            k, v = m.group(1), m.group(2)
            if k in names:
                values[k] = v[1:-1] if v[:1] in "\"'" else v
    return values


def load_from_crontab() -> list[str]:
    import subprocess
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return _set_aliases(_scan_kv_text(out))


def load_config() -> str | None:
    """Zbiera dane poczty; zwraca opis źródeł. Kolejność = priorytet."""
    sources = []
    if load_env_file(LOCAL_ENV_FILE):
        sources.append(str(LOCAL_ENV_FILE))
    candidates = [os.environ["PGE_ENV_FILE"]] if os.environ.get("PGE_ENV_FILE") else SHARED_ENV_FILES
    for c in candidates:
        p = Path(c).expanduser()
        if load_env_file(p):
            sources.append(str(p))
            break
    for c in SHARED_PY_FILES:
        p = Path(c).expanduser()
        if got := load_from_python(p):
            sources.append(f"{p} ({', '.join(got)})")
    if got := load_from_crontab():
        sources.append(f"crontab ({', '.join(got)})")
    try:
        if got := _set_aliases(_scan_kv_text(Path("/etc/environment").read_text())):
            sources.append(f"/etc/environment ({', '.join(got)})")
    except OSError:
        pass
    return "; ".join(sources) or None


def targets() -> list[tuple[str, str]]:
    """PGE_ADRESY='Miasto: Ulica1, Ulica2; Miasto2: Ulica3' → [(miasto, ulica), …]"""
    raw = os.environ.get("PGE_ADRESY", "").strip()
    out = []
    for part in raw.split(";"):
        if ":" not in part:
            continue
        city, streets = part.split(":", 1)
        for st in streets.split(","):
            st = re.sub(r"^\s*(ul\.|ulica)\s*", "", st.strip(), flags=re.I).strip()
            if city.strip() and st:
                out.append((city.strip(), st))
    if not out:
        raise SystemExit("Brak adresów: dopisz do .env obok skryptu np.\n"
                         "  PGE_ADRESY=Miasto: Ulica1, Ulica2\n  PGE_POWIAT=nazwa_powiatu")
    return out


def log(msg: str) -> None:
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def http_json(url: str, params: dict) -> object:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    last_err: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(full, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                body = r.read().decode("utf-8", errors="replace")
            return json.loads(body)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            log(f"  próba {attempt}/{HTTP_RETRIES} nieudana: {e}")
            if attempt < HTTP_RETRIES:
                time.sleep(15 * attempt)
    raise RuntimeError(f"Brak odpowiedzi z {url}: {last_err}")


def norm(s: str) -> str:
    """małe litery, bez polskich znaków, bez 'ul.'"""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"\b(ul|ulica|al|aleja)\.?\s+", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def stem(street: str) -> str:
    """Rdzeń do szukania w opisach: Lipowa → lipow (łapie Lipowej, Lipową)."""
    n = norm(street)
    return n[:-1] if len(n) > 4 and n[-1] in "aeiouy" else n


# ─────────────── TERYT (cache w pliku stanu) ───────────────

def resolve_city(name: str, cache: dict) -> str:
    key = f"city:{name}"
    if key in cache:
        return cache[key]
    data = http_json(f"{FALCON}/teryt-city", {"cityName": name})
    exact = [c for c in data if norm(c.get("cityName", "")) == norm(name)] or list(data)
    if len(exact) > 1:
        hint = norm(os.environ.get("PGE_POWIAT", ""))
        if hint:
            exact = [c for c in exact if hint in norm(c.get("countyName", ""))] or exact
    if not exact:
        raise RuntimeError(f"Nie znaleziono miejscowości '{name}' w TERYT PGE")
    c = exact[0]
    log(f"TERYT: {c.get('cityName')} ({c.get('countyName')}, {c.get('voivodeshipName')}) "
        f"citySym={c.get('citySym')}")
    cache[key] = str(c["citySym"])
    return cache[key]


def resolve_street(city_sym: str, street: str, cache: dict) -> str | None:
    key = f"street:{city_sym}:{street}"
    if key in cache:
        return cache[key]
    data = http_json(f"{FALCON}/street", {"citySym": city_sym, "name": street})
    hits = [s for s in data if norm(s.get("name", "")) == norm(street)]
    if not hits:
        hits = [s for s in data if norm(street) in norm(s.get("name", ""))]
    if not hits:
        log(f"UWAGA: brak ulicy '{street}' w TERYT dla citySym={city_sym} — "
            f"będę szukać tylko po opisach wyłączeń w miejscowości")
        return None
    s = hits[0]
    log(f"TERYT: {s.get('name')} symUl={s.get('symUl')}")
    cache[key] = str(s["symUl"])
    return cache[key]


# ─────────────── wyłączenia ───────────────

def fetch_outages(city_sym: str, street_sym: str | None = None) -> list[dict]:
    out, page, size = [], 0, 500
    while True:
        params = {"page": page, "size": size, "type": "teryt", "citySym": city_sym}
        if street_sym:
            params["streetSym"] = street_sym
        data = http_json(OUTAGE_API, params)
        items = data if isinstance(data, list) else (data.get("items") or data.get("content") or [])
        out.extend(items)
        if len(items) < size or page >= 9:
            return out
        page += 1


def parse_dt(x: str | None) -> datetime | None:
    if not x:
        return None
    try:
        dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)


def clean_text(s: str | None) -> str:
    s = html.unescape(re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", s).strip()


CITY_KEY = re.compile(r"city|miejsc|locality|town|place|gmina|commune", re.I)
SYM_KEY = re.compile(r"citysym|simc|teryt|sym(?!ul)", re.I)
STREETISH = re.compile(r"street|ulic|sym_?ul", re.I)


def location_fields(o, path="") -> tuple[set[str], set[str]]:
    """Zbiera z całego rekordu (także zagnieżdżonego) nazwy miejscowości i kody TERYT/SIMC."""
    names, syms = set(), set()
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                n, s = location_fields(v, k)
                names |= n
                syms |= s
            elif v not in (None, "") and not STREETISH.search(k):
                if SYM_KEY.search(k) and str(v).isdigit():
                    syms.add(str(v).lstrip("0"))
                elif CITY_KEY.search(k) and isinstance(v, str) and not v.isdigit():
                    names.add(v.strip())
    elif isinstance(o, list):
        for v in o:
            n, s = location_fields(v, path)
            names |= n
            syms |= s
    return names, syms


def city_matches(o: dict, city: str, city_sym: str) -> tuple[bool, str]:
    """Czy wyłączenie dotyczy naszej miejscowości? Zwraca (tak/nie, opis do logu)."""
    names, syms = location_fields(o)
    if city_sym.lstrip("0") in syms:
        return True, city
    if any(norm(n) == norm(city) for n in names):
        return True, city
    if names or syms:
        return False, ", ".join(sorted(names)) or f"TERYT {', '.join(sorted(syms))}"
    # brak danych o miejscowości — akceptujemy tylko, gdy opis wprost wymienia naszą miejscowość
    desc = norm(" ".join(str(o.get(k) or "") for k in ("description", "message", "address")))
    if re.search(rf"\b{re.escape(stem(city))}\w*", desc):
        return True, city
    return False, "brak miejscowości w danych PGE"


SEG_RE = re.compile(r"^\s*([^:;]{2,60}?)\s*:\s*(.*)$", re.S)


def desc_segments(desc: str) -> list[tuple[str, str]] | None:
    """Opis PGE dla wielu miejscowości: 'Miasto A: ul. X, ul. Y; Miasto B: ul. Z; Wieś C'.
    Zwraca [(miejscowość, ulice)], albo None, gdy opis nie ma takiej struktury."""
    parts = [p.strip() for p in desc.split(";") if p.strip()]
    segs, structured = [], False
    for p in parts:
        m = SEG_RE.match(p)
        if m and not re.match(r"^(ul|al|os|pl)\.?\s", m.group(1), re.I):
            segs.append((m.group(1).strip(), m.group(2)))
            structured = True
        elif "ul." not in p and len(p) <= 60:
            segs.append((p, ""))                     # sama miejscowość, bez ulic
        else:
            segs.append(("", p))
    return segs if structured else None


def streets_in(text: str, streets: list[str]) -> set[str]:
    t = norm(text)
    return {st for st in streets if re.search(rf"\b{re.escape(stem(st))}\w*", t)}


def collect(dump: list | None = None) -> list[dict]:
    """Zwraca listę wyłączeń dotyczących ulic z PGE_ADRESY (bez duplikatów)."""
    state = load_state()
    cache = state.setdefault("teryt", {})
    found: dict[str, dict] = {}

    by_city: dict[str, list[str]] = {}
    for city, street in targets():
        by_city.setdefault(city, []).append(street)

    for city, streets in by_city.items():
        city_sym = resolve_city(city, cache)
        # 1) cała miejscowość — łapie wpisy, gdzie ulica jest tylko w opisie
        pool = {str(o.get("id")): o for o in fetch_outages(city_sym)}
        # 2) dokładnie po ulicy (TERYT) — oznaczamy jako pewne trafienie
        sure: dict[str, set[str]] = {}
        for st in streets:
            ssym = resolve_street(city_sym, st, cache)
            if ssym:
                for o in fetch_outages(city_sym, ssym):
                    oid = str(o.get("id"))
                    pool.setdefault(oid, o)
                    sure.setdefault(oid, set()).add(st)

        if dump is not None:
            dump.extend(pool.values())
        for oid, o in pool.items():
            if o.get("revoked") or o.get("deleted"):
                continue
            raw_desc = clean_text(o.get("description") or o.get("message"))
            segs = desc_segments(raw_desc)
            if segs:
                # opis wielu miejscowości: ulica liczy się tylko w odcinku NASZEJ miejscowości
                ours = [s for c, s in segs if norm(c) == norm(city)]
                matched = set().union(*(streets_in(s, streets) for s in ours)) if ours else set()
                if not matched:
                    elsewhere = sorted({c for c, s in segs if c and streets_in(s, streets)})
                    found_st = set().union(*(streets_in(s, streets) for c, s in segs)) or set(sure.get(oid, ()))
                    if elsewhere or sure.get(oid):
                        log(f"Pominięto wyłączenie {oid}: ul. {', '.join(sorted(found_st))} "
                            f"występuje w {', '.join(elsewhere) or 'innej miejscowości'}, "
                            f"nie w {city}" + (f" ({city}: {'; '.join(ours)})" if ours else ""))
                    continue
                where = city
                shown = "; ".join(f"{city}: {s}" for s in ours)
            else:
                text = norm(" ".join(str(o.get(k) or "") for k in ("streetName", "address")) + " " + raw_desc)
                matched = set(sure.get(oid, set())) | streets_in(text, streets)
                if not matched:
                    continue
                # TERYT ulicy (SYM_UL) jest wspólny dla tej samej nazwy w całym kraju,
                # a API potrafi zwrócić wpisy spoza miejscowości — sprawdzamy ją zawsze
                ok, where = city_matches(o, city, city_sym)
                if not ok:
                    log(f"Pominięto wyłączenie {oid} (ul. {', '.join(sorted(matched))}): "
                        f"dotyczy innej miejscowości — {where}")
                    continue
                shown = raw_desc
            start, end = parse_dt(o.get("startAt")), parse_dt(o.get("stopAt"))
            rec = found.setdefault(oid, {
                "id": oid,
                "kind": "awaria" if o.get("type") == 1 else "planowane",
                "city": city if segs else (o.get("cityName") or where),
                "streets": set(),
                "start": start,
                "end": end,
                "description": shown,                 # tylko adresy z naszej miejscowości
            })
            rec["streets"] |= matched

    save_state(state)  # zapis cache TERYT
    return sorted(found.values(), key=lambda r: r["start"] or datetime.max.replace(tzinfo=TZ))


# ─────────────── stan ───────────────

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def prune_notified(notified: dict, today: date) -> dict:
    """Usuń wpisy starsze niż 60 dni."""
    limit = (today - timedelta(days=60)).isoformat()
    return {k: v for k, v in notified.items() if v >= limit}


# ─────────────── wysyłka ───────────────

DNI = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]


def fmt(r: dict) -> str:
    s, e = r["start"], r["end"]
    when = (f"{DNI[s.weekday()]} {s:%d.%m.%Y}, {s:%H:%M}–" +
            (f"{e:%H:%M}" if e and e.date() == s.date() else f"{e:%d.%m %H:%M}" if e else "?")
            ) if s else "termin nieznany"
    lines = [f"• {when}",
             f"  {r['city']}, ul. {', '.join(sorted(r['streets']))} ({r['kind']})"]
    if r["description"]:
        lines.append("  " + r["description"].replace("\n", "\n  "))
    return "\n".join(lines)


def send_mail(subject: str, body: str) -> None:
    user, pw = os.environ.get("EMAIL_FROM"), os.environ.get("EMAIL_PASSWORD")
    if not (user and pw):
        missing = [k for k in ("EMAIL_FROM", "EMAIL_PASSWORD") if not os.environ.get(k)]
        raise RuntimeError(f"Nie znalazłem: {', '.join(missing)} (szukałem w .env, "
                           f"{', '.join(SHARED_PY_FILES)}, crontab, /etc/environment). "
                           f"Dopisz brakujące do {LOCAL_ENV_FILE} jako KLUCZ=wartość")
    to = [a.strip() for a in os.environ.get("PGE_MAIL_TO", user).split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(to)
    msg.set_content(f"{body}\n\n-- \n{AUTHOR_FOOTER}")
    host = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    if port == 587:
        s = smtplib.SMTP(host, port, timeout=60)
        s.starttls()
    else:
        s = smtplib.SMTP_SSL(host, port, timeout=60)
    with s:
        s.login(user, pw)
        s.send_message(msg)
    log(f"Wysłano e-mail do {', '.join(to)}")


def send_sms(text: str) -> None:
    token, to = os.environ.get("SMSAPI_TOKEN"), os.environ.get("SMS_TO")
    if not (token and to):
        return
    data = urllib.parse.urlencode({"to": to, "message": text[:459], "format": "json",
                                   "encoding": "utf-8"}).encode()
    req = urllib.request.Request("https://api.smsapi.pl/sms.do", data=data,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
        if "error" in resp:
            log(f"SMSAPI błąd: {resp}")
        else:
            log(f"Wysłano SMS do {to}")
    except Exception as e:  # SMS jest dodatkiem — nie blokuj maila
        log(f"SMS nieudany: {e}")


# ─────────────── main ───────────────

def build_message(to_send: list[dict]) -> tuple[str, str, str]:
    planned = [r for r in to_send if r["kind"] == "planowane"]
    failures = [r for r in to_send if r["kind"] == "awaria"]
    first = (planned or failures)[0]["start"]
    subject = (f"[PGE] Planowane wyłączenie prądu {first:%d.%m} ({DNI[first.weekday()]})"
               if planned else "[PGE] Awaria — brak prądu")
    parts = []
    if planned:
        parts.append("Planowane wyłączenia:\n\n" + "\n\n".join(fmt(r) for r in planned))
    if failures:
        parts.append("Trwające awarie:\n\n" + "\n\n".join(fmt(r) for r in failures))
    parts.append("Źródło: PGE Dystrybucja, pgedystrybucja.pl/wylaczenia")
    body = "\n\n".join(parts)

    sms = "; ".join(
        f"PGE {r['kind']}: {r['start']:%d.%m %H:%M}-{r['end']:%H:%M} ul. {','.join(sorted(r['streets']))}"
        if r["end"] else f"PGE {r['kind']}: {r['start']:%d.%m %H:%M} ul. {','.join(sorted(r['streets']))}"
        for r in to_send)
    return subject, body, sms


def sample_outages() -> list[dict]:
    """Przykładowe wyłączenie na jutro 8–14 dla ulic z PGE_ADRESY (do testu)."""
    tomorrow = datetime.now(TZ).replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    by_city: dict[str, set[str]] = {}
    for city, street in targets():
        by_city.setdefault(city, set()).add(street)
    return [{
        "id": f"test-{city}", "kind": "planowane", "city": city, "streets": streets,
        "start": tomorrow, "end": tomorrow.replace(hour=14),
        "description": "TO JEST TEST — przykładowe dane, PGE nie planuje tego wyłączenia.",
    } for city, streets in by_city.items()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--list", action="store_true", help="pokaż nadchodzące wyłączenia i zakończ")
    ap.add_argument("--dry-run", action="store_true", help="nic nie wysyłaj, nie zapisuj stanu")
    ap.add_argument("--test-mail", action="store_true", help="wyślij testowy e-mail")
    ap.add_argument("--zrzut", action="store_true",
                    help="zapisz surowe dane z PGE do ~/pge_zrzut_*.json (do diagnostyki; z --list nic nie wysyła)")
    ap.add_argument("--test-powiadomienie", action="store_true",
                    help="wyślij przykładowe powiadomienie (jutro 8–14) — mail + SMS, bez zapisu stanu")
    a = ap.parse_args()

    env_src = load_config()
    if a.test_mail or a.dry_run or a.list or a.test_powiadomienie:
        log(f"Dane poczty z: {env_src or 'nie znaleziono źródeł (tylko zmienne środowiskowe)'}")

    if a.test_mail:
        send_mail("[PGE] test powiadomień", "Działa. Ten e-mail wysłał pge_wylaczenia.py.")
        send_sms("PGE: test powiadomień – działa.")
        return 0

    if a.test_powiadomienie:
        subject, body, sms = build_message(sample_outages())
        send_mail("[TEST] " + subject, body)
        send_sms("TEST " + sms)
        return 0

    now = datetime.now(TZ)
    today = now.date()
    target_day = today + timedelta(days=NOTIFY_DAYS_BEFORE)

    try:
        raw = [] if a.zrzut else None
        outages = collect(raw)
        if a.zrzut:
            out = Path.home() / f"pge_zrzut_{datetime.now(TZ):%Y%m%d_%H%M}.json"
            out.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"Zapisano surowe dane PGE ({len(raw)} wpisów): {out}")
    except Exception as e:
        log(f"BŁĄD pobierania danych z PGE: {e}")
        return 2

    upcoming = [r for r in outages if r["start"] and (r["end"] or r["start"]) >= now]
    log(f"Znaleziono {len(upcoming)} nadchodzących/trwających wyłączeń dla wskazanych ulic")

    if a.list:
        print("\n\n".join(fmt(r) for r in upcoming) or "Brak.")
        return 0

    state = load_state()
    notified = prune_notified(state.get("notified", {}), today)

    q_from, q_to = QUIET_HOURS
    if q_from > q_to:   # okno przez północ, np. 22–7
        quiet = now.hour >= q_from or now.hour < q_to
        quiet_end = now.replace(hour=q_to, minute=0, second=0, microsecond=0) + (
            timedelta(days=1) if now.hour >= q_from else timedelta())
    else:
        quiet = q_from <= now.hour < q_to
        quiet_end = now.replace(hour=q_to % 24, minute=0, second=0, microsecond=0)

    to_send = []
    for r in upcoming:
        if r["id"] in notified:
            continue
        if r["kind"] == "planowane":
            # w nocy czekamy do rana — chyba że wyłączenie zaczyna się jeszcze przed końcem ciszy
            if quiet and r["start"] > quiet_end:
                continue
            # dzień przed, albo nadrabianie: PGE opublikowało późno / skrypt nie działał
            if today < r["start"].date() <= target_day or (
                    r["start"].date() == today and r["start"] > now):
                to_send.append(r)
        elif INCLUDE_FAILURES and r["start"] <= now:
            to_send.append(r)

    if not to_send:
        log("Nic do wysłania")
        return 0

    subject, body, sms = build_message(to_send)

    if a.dry_run:
        print(f"Temat: {subject}\n\n{body}\n\n-- \n{AUTHOR_FOOTER}\n\nSMS: {sms}")
        return 0

    try:
        send_mail(subject, body)
    except Exception as e:
        log(f"BŁĄD wysyłki e-maila: {e}")
        return 3
    send_sms(sms)

    for r in to_send:
        notified[r["id"]] = today.isoformat()
    state = load_state()
    state["notified"] = notified
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
