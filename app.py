import os
import re
import json
import sqlite3
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, g, redirect, url_for, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.config["DATABASE"] = os.path.join(app.root_path, "phd.db")
app.config["UPLOAD_FOLDER"] = os.path.join(app.root_path, "uploads")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")


@app.template_filter("rowtodict")
def rowtodict_filter(row):
    return dict(row)


app.jinja_env.filters["tojson_safe"] = lambda val: json.dumps(
    dict(val) if isinstance(val, sqlite3.Row) else val)


@app.template_filter("load_checklist")
def load_checklist_filter(val):
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return {}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(filename="scheduler.log", level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("phd")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


def query_db(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


def init_db():
    db = sqlite3.connect(app.config["DATABASE"])
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    db.executescript("""
    CREATE TABLE IF NOT EXISTS profile (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE IF NOT EXISTS programs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        country    TEXT,
        deadline   TEXT,
        status     TEXT DEFAULT 'researching',
        fit_score  INTEGER DEFAULT 0,
        notes      TEXT,
        supervisor TEXT DEFAULT '',
        checklist  TEXT DEFAULT '{}',
        created    TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        priority    TEXT DEFAULT 'medium',
        category    TEXT DEFAULT 'writing',
        deadline    TEXT,
        done        INTEGER DEFAULT 0,
        attachments TEXT DEFAULT '[]',
        created     TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS open_calls (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        type            TEXT,
        category        TEXT DEFAULT '',
        description     TEXT,
        deadline        TEXT,
        url             TEXT,
        source          TEXT DEFAULT 'scraped',
        relevance_score INTEGER DEFAULT 0,
        saved           INTEGER DEFAULT 0,
        created         TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS inbox (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        type      TEXT NOT NULL,
        title     TEXT,
        body      TEXT,
        status    TEXT DEFAULT 'pending',
        created   TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS voice_samples (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        text      TEXT NOT NULL,
        analysis  TEXT,
        created   TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS chat_history (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        role      TEXT NOT NULL,
        content   TEXT NOT NULL,
        created   TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS xp_log (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        action  TEXT NOT NULL,
        xp      INTEGER NOT NULL,
        detail  TEXT DEFAULT '',
        created TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS achievements (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        key     TEXT UNIQUE NOT NULL,
        title   TEXT NOT NULL,
        description TEXT,
        xp_bonus INTEGER DEFAULT 0,
        unlocked TEXT DEFAULT NULL
    );

    CREATE TABLE IF NOT EXISTS pitch_targets (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        type        TEXT DEFAULT 'magazine',
        url         TEXT,
        description TEXT,
        suggested   INTEGER DEFAULT 0,
        saved       INTEGER DEFAULT 0,
        created     TEXT DEFAULT (datetime('now'))
    );
    """)

    # Add columns to existing tables if missing
    for col, tbl, default in [
        ("supervisor", "programs", "''"),
        ("checklist", "programs", "'{}'"),
        ("category", "open_calls", "''"),
        ("relevance_score", "open_calls", "0"),
        ("saved", "open_calls", "0"),
        ("attachments", "tasks", "'[]'"),
    ]:
        try:
            db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Seed data
    existing = db.execute("SELECT value FROM profile WHERE key='research_angle'").fetchone()
    if not existing:
        db.execute("INSERT INTO profile (key, value) VALUES (?, ?)",
                   ("research_angle",
                    "Transformation og gjenbruk av etterkrigstidens boligbygg som arkitektonisk og sosial praksis — med Rågsved som case."))
        db.execute("INSERT INTO profile (key, value) VALUES (?, ?)",
                   ("voice_profile", ""))

        programs = [
            ("Columbia GSAPP", "USA", "2026-12-15", "researching", 78,
             "Strong urban housing research group. Look into CCCP lab.",
             "Mabel O. Wilson, Kate Orff"),
            ("Harvard GSD", "USA", "2027-01-02", "researching", 72,
             "MDes or DDes track. Check Inaba's studio.",
             "K. Michael Hays, Jennifer Bonner"),
            ("ETH Zürich", "Sveits", "2026-11-01", "researching", 85,
             "Chair of Architecture and Urban Design. Strong on European postwar housing.",
             "Tom Emerson, Charlotte Malterre-Barthes"),
            ("KTH Stockholm", "Sverige", "2026-10-15", "shortlisted", 90,
             "Closest geographically. Arkitekturskolan has active housing research.",
             "Meike Schalk, Helena Mattsson"),
            ("TU Delft", "Nederland", "2026-12-01", "researching", 80,
             "Heritage & Architecture dept. Good fit for adaptive reuse angle.",
             "Lidwine Spoormans, Hielkje Zijlstra"),
        ]
        default_checklist = json.dumps({
            "Research program requirements": False,
            "Identify potential supervisor": False,
            "Draft research proposal": False,
            "Prepare writing sample": False,
            "Request recommendation letters": False,
            "Prepare portfolio": False,
            "Submit application": False,
        })
        for p in programs:
            db.execute(
                "INSERT INTO programs (name, country, deadline, status, fit_score, notes, supervisor, checklist) VALUES (?,?,?,?,?,?,?,?)",
                (*p, default_checklist))

        # Seed starter tasks
        tasks = [
            ("Draft 2-page research proposal", "high", "writing", "2026-04-15"),
            ("Map potential supervisors at each program", "high", "networking", "2026-04-01"),
            ("Write 500-word abstract for Rågsved case", "medium", "writing", "2026-04-10"),
            ("Create academic CV / portfolio", "high", "writing", "2026-05-01"),
            ("Reach out to KTH faculty for informal chat", "medium", "networking", "2026-04-05"),
            ("Post research reflection on LinkedIn", "low", "visibility", "2026-04-07"),
        ]
        db.executemany(
            "INSERT INTO tasks (title, priority, category, deadline) VALUES (?,?,?,?)", tasks)

    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def ollama_generate(prompt, system=None, as_json=False):
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    if as_json:
        payload["format"] = "json"
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=120)
        r.raise_for_status()
        return r.json().get("response", "")
    except Exception as e:
        log.error("Ollama error: %s", e)
        return f"[Ollama unavailable: {e}]"


def parse_json_response(raw, expect_list=False):
    """Robustly extract JSON from Ollama responses."""
    cleaned = re.sub(r'```(?:json)?\s*', '', raw).strip()
    try:
        parsed = json.loads(cleaned)
        if expect_list and isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return v
        return parsed
    except json.JSONDecodeError:
        pass
    bracket = "[" if expect_list else "{"
    end_bracket = "]" if expect_list else "}"
    try:
        start = cleaned.index(bracket)
        end = cleaned.rindex(end_bracket) + 1
        return json.loads(cleaned[start:end])
    except (ValueError, json.JSONDecodeError):
        pass
    return None


def ollama_chat(messages):
    payload = {"model": OLLAMA_MODEL, "messages": messages, "stream": False}
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")
    except Exception as e:
        log.error("Ollama chat error: %s", e)
        return f"[Ollama unavailable: {e}]"


def build_system_context():
    db = sqlite3.connect(app.config["DATABASE"])
    db.row_factory = sqlite3.Row
    angle = db.execute("SELECT value FROM profile WHERE key='research_angle'").fetchone()
    voice = db.execute("SELECT value FROM profile WHERE key='voice_profile'").fetchone()
    programs = db.execute("SELECT name, country, status, fit_score, supervisor FROM programs").fetchall()
    tasks = db.execute("SELECT title, priority, category, done FROM tasks ORDER BY done, priority").fetchall()
    angle_text = angle["value"] if angle else "Not set"
    voice_text = voice["value"] if voice and voice["value"] else "Not yet defined"
    prog_lines = "\n".join(
        f"- {p['name']} ({p['country']}) — status: {p['status']}, fit: {p['fit_score']}/100, supervisors: {p['supervisor']}"
        for p in programs)
    task_lines = "\n".join(
        f"- {'[x]' if t['done'] else '[ ]'} {t['title']} ({t['priority']}, {t['category']})"
        for t in tasks)
    db.close()
    return f"""You are the AI assistant for a PhD Command Center. The user is applying for PhD programs in architecture.

Research angle: {angle_text}

Programs:
{prog_lines or 'None added yet.'}

Tasks:
{task_lines or 'None added yet.'}

Voice profile: {voice_text}

Always respond in English. Be precise and actionable. Focus on architecture, housing, urban studies, and adaptive reuse."""


# ---------------------------------------------------------------------------
# Gamification — XP, levels, achievements
# ---------------------------------------------------------------------------

ACHIEVEMENT_DEFS = [
    ("first_program", "First Steps", "Add your first PhD program", 50),
    ("five_programs", "Building a Shortlist", "Track 5 programs", 100),
    ("first_task_done", "Getting Things Done", "Complete your first task", 30),
    ("ten_tasks_done", "Productivity Machine", "Complete 10 tasks", 150),
    ("first_voice_sample", "Finding Your Voice", "Submit a writing sample for voice training", 50),
    ("three_voice_samples", "Voice Master", "Submit 3 writing samples", 100),
    ("first_draft", "Draft Day", "Generate your first AI draft", 30),
    ("first_outreach", "Reaching Out", "Generate an outreach email", 50),
    ("five_drafts_approved", "Editor in Chief", "Approve 5 drafts", 150),
    ("first_scan", "Scout", "Run your first open calls scan", 30),
    ("first_save", "Bookmark Keeper", "Save an open call", 20),
    ("ten_saves", "Opportunity Collector", "Save 10 open calls", 100),
    ("first_keyword_search", "Keyword Hunter", "Run a custom keyword search", 30),
    ("first_pitch", "Pitch Perfect", "Save a magazine pitch target", 50),
    ("first_own_draft", "Original Writer", "Write your own draft in the inbox", 50),
    ("streak_3", "On a Roll", "Use the app 3 days in a row", 75),
    ("streak_7", "Week Warrior", "Use the app 7 days in a row", 200),
    ("streak_14", "Unstoppable", "Use the app 14 days in a row", 500),
    ("level_5", "Rising Scholar", "Reach level 5", 100),
    ("level_10", "PhD Ready", "Reach level 10", 250),
]

XP_PER_LEVEL = 200  # XP needed per level (level = total_xp // 200)

XP_ACTIONS = {
    "add_program": 20,
    "complete_task": 15,
    "submit_voice": 25,
    "generate_draft": 10,
    "approve_draft": 10,
    "scan_calls": 15,
    "save_call": 5,
    "keyword_search": 10,
    "save_pitch": 10,
    "write_own_draft": 30,
    "daily_login": 10,
    "checklist_item": 5,
}


def _seed_achievements(db):
    """Seed achievement definitions if not present."""
    for key, title, desc, bonus in ACHIEVEMENT_DEFS:
        db.execute(
            "INSERT OR IGNORE INTO achievements (key, title, description, xp_bonus) VALUES (?,?,?,?)",
            (key, title, desc, bonus))
    db.commit()


def award_xp(db, action, detail=""):
    """Award XP for an action. Returns (xp_gained, new_total, achievements_unlocked)."""
    xp = XP_ACTIONS.get(action, 0)
    if xp <= 0:
        return 0, 0, []
    db.execute("INSERT INTO xp_log (action, xp, detail) VALUES (?,?,?)", (action, xp, detail))

    # Track daily login streak
    today = datetime.now().strftime("%Y-%m-%d")
    existing_today = db.execute(
        "SELECT 1 FROM xp_log WHERE action='daily_login' AND date(created)=?", (today,)).fetchone()
    if not existing_today and action != "daily_login":
        db.execute("INSERT INTO xp_log (action, xp, detail) VALUES ('daily_login', ?, 'auto')",
                   (XP_ACTIONS["daily_login"],))

    total_xp = db.execute("SELECT COALESCE(SUM(xp),0) FROM xp_log").fetchone()[0]
    unlocked = _check_achievements(db, total_xp)
    # Add bonus XP from newly unlocked achievements
    for ach in unlocked:
        db.execute("INSERT INTO xp_log (action, xp, detail) VALUES ('achievement',?,?)",
                   (ach["xp_bonus"], ach["title"]))
    db.commit()
    total_xp = db.execute("SELECT COALESCE(SUM(xp),0) FROM xp_log").fetchone()[0]
    return xp, total_xp, unlocked


def _check_achievements(db, total_xp):
    """Check and unlock achievements based on current state. Returns list of newly unlocked."""
    unlocked = []

    def _try_unlock(key):
        row = db.execute("SELECT unlocked FROM achievements WHERE key=?", (key,)).fetchone()
        if row and not row["unlocked"]:
            db.execute("UPDATE achievements SET unlocked=datetime('now') WHERE key=?", (key,))
            ach = db.execute("SELECT * FROM achievements WHERE key=?", (key,)).fetchone()
            unlocked.append(dict(ach))

    # Program achievements
    prog_count = db.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
    if prog_count >= 1:
        _try_unlock("first_program")
    if prog_count >= 5:
        _try_unlock("five_programs")

    # Task achievements
    done_count = db.execute("SELECT COUNT(*) FROM tasks WHERE done=1").fetchone()[0]
    if done_count >= 1:
        _try_unlock("first_task_done")
    if done_count >= 10:
        _try_unlock("ten_tasks_done")

    # Voice achievements
    voice_count = db.execute("SELECT COUNT(*) FROM voice_samples").fetchone()[0]
    if voice_count >= 1:
        _try_unlock("first_voice_sample")
    if voice_count >= 3:
        _try_unlock("three_voice_samples")

    # Draft achievements
    draft_count = db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    if draft_count >= 1:
        _try_unlock("first_draft")
    outreach = db.execute("SELECT 1 FROM inbox WHERE type='outreach'").fetchone()
    if outreach:
        _try_unlock("first_outreach")
    approved_count = db.execute("SELECT COUNT(*) FROM inbox WHERE status='approved'").fetchone()[0]
    if approved_count >= 5:
        _try_unlock("five_drafts_approved")

    # Open calls achievements
    scan_count = db.execute("SELECT COUNT(*) FROM xp_log WHERE action='scan_calls'").fetchone()[0]
    if scan_count >= 1:
        _try_unlock("first_scan")
    saved_count = db.execute("SELECT COUNT(*) FROM open_calls WHERE saved=1").fetchone()[0]
    if saved_count >= 1:
        _try_unlock("first_save")
    if saved_count >= 10:
        _try_unlock("ten_saves")

    # Keyword search
    kw_count = db.execute("SELECT COUNT(*) FROM xp_log WHERE action='keyword_search'").fetchone()[0]
    if kw_count >= 1:
        _try_unlock("first_keyword_search")

    # Pitch targets
    pitch_saved = db.execute("SELECT COUNT(*) FROM pitch_targets WHERE saved=1").fetchone()[0]
    if pitch_saved >= 1:
        _try_unlock("first_pitch")

    # Own draft
    own_count = db.execute("SELECT COUNT(*) FROM xp_log WHERE action='write_own_draft'").fetchone()[0]
    if own_count >= 1:
        _try_unlock("first_own_draft")

    # Streak achievements
    streak = _get_streak(db)
    if streak >= 3:
        _try_unlock("streak_3")
    if streak >= 7:
        _try_unlock("streak_7")
    if streak >= 14:
        _try_unlock("streak_14")

    # Level achievements
    level = total_xp // XP_PER_LEVEL
    if level >= 5:
        _try_unlock("level_5")
    if level >= 10:
        _try_unlock("level_10")

    return unlocked


def _get_streak(db):
    """Calculate current daily streak from xp_log."""
    rows = db.execute(
        "SELECT DISTINCT date(created) as d FROM xp_log ORDER BY d DESC").fetchall()
    if not rows:
        return 0
    streak = 0
    today = datetime.now().date()
    for row in rows:
        expected = today - timedelta(days=streak)
        try:
            d = datetime.strptime(row["d"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            break
        if d == expected:
            streak += 1
        else:
            break
    return streak


def get_gamification_stats(db):
    """Get all gamification data for display."""
    total_xp = db.execute("SELECT COALESCE(SUM(xp),0) FROM xp_log").fetchone()[0]
    level = total_xp // XP_PER_LEVEL
    xp_in_level = total_xp % XP_PER_LEVEL
    streak = _get_streak(db)
    all_achievements = db.execute("SELECT * FROM achievements ORDER BY unlocked IS NULL, unlocked DESC").fetchall()
    unlocked_count = sum(1 for a in all_achievements if a["unlocked"])
    recent_xp = db.execute(
        "SELECT action, xp, detail, created FROM xp_log ORDER BY created DESC LIMIT 10").fetchall()
    return {
        "total_xp": total_xp,
        "level": level,
        "xp_in_level": xp_in_level,
        "xp_per_level": XP_PER_LEVEL,
        "streak": streak,
        "achievements": [dict(a) for a in all_achievements],
        "unlocked_count": unlocked_count,
        "total_achievements": len(all_achievements),
        "recent_xp": [dict(r) for r in recent_xp],
    }


# ---------------------------------------------------------------------------
# Web scraping — clean, filtered open calls
# ---------------------------------------------------------------------------

SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Words that indicate junk / navigation / irrelevant scrape results
JUNK_PATTERNS = re.compile(
    r'^(filter by|sort by|search|login|sign up|cookie|menu|navigation|home|'
    r'about us|contact|privacy|terms|subscribe|newsletter|loading|page \d|'
    r'next|previous|show more|read more|view all|see all|back to)$',
    re.IGNORECASE
)

# Fields that have NOTHING to do with architecture
IRRELEVANT_FIELDS = re.compile(
    r'(agricultural|veterinary|zoology|zootechnics|medical technology|'
    r'pharmacology|dentistry|nursing|clinical|biochemistry|molecular biology|'
    r'particle physics|astrophysics|organic chemistry|marine biology|'
    r'fisheries|forestry|crop science|animal science|petroleum|mining|'
    r'metallurgy|telecommunications)',
    re.IGNORECASE
)

# Keywords that signal relevance to the research angle
RELEVANCE_KEYWORDS = {
    "high": ["postwar", "post-war", "housing", "adaptive reuse", "reuse",
             "bolig", "rågsved", "million programme", "miljonprogram",
             "brutalism", "social housing", "public housing", "modernist housing",
             "renovation", "transformation", "heritage building", "retrofit"],
    "medium": ["architecture", "urban design", "urban planning", "urban studies",
               "built environment", "heritage", "preservation", "conservation",
               "spatial", "dwelling", "residential", "neighborhood",
               "gentrification", "placemaking", "landscape architecture",
               "architectural history", "architectural theory", "sustainability",
               "building culture", "urban transformation", "urban renewal"],
    "low": ["design", "planning", "city", "building", "construction",
            "infrastructure", "community", "participatory", "cultural",
            "museum", "exhibition", "public space"],
}


def _get(url, timeout=15):
    try:
        r = requests.get(url, headers=SCRAPE_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r
        log.warning("HTTP %d for %s", r.status_code, url)
    except Exception as e:
        log.warning("Request failed for %s: %s", url, e)
    return None


def _clean_text(text):
    """Clean scraped text — collapse whitespace, remove noise."""
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^(Deadline|Event|Date|When|Where)[:\s]*', '', text)
    return text


def _is_junk(title, description=""):
    """Check if a scraped result is junk (nav element, irrelevant field)."""
    title_clean = title.strip()
    if len(title_clean) < 4:
        return True
    if JUNK_PATTERNS.match(title_clean):
        return True
    combined = f"{title} {description}"
    if IRRELEVANT_FIELDS.search(combined):
        return True
    # Reject if title is just a date or number
    if re.match(r'^[\d\s,\.\-/]+$', title_clean):
        return True
    # Reject if title looks like navigation
    if title_clean.lower() in ('filter by', 'sort', 'search results', 'no results'):
        return True
    return False


def _score_relevance(title, description=""):
    """Score relevance 0-100 based on keyword matches."""
    combined = f"{title} {description}".lower()
    score = 0
    matched_cats = set()

    for keyword in RELEVANCE_KEYWORDS["high"]:
        if keyword in combined:
            score += 25
            matched_cats.add("core-match")

    for keyword in RELEVANCE_KEYWORDS["medium"]:
        if keyword in combined:
            score += 10
            matched_cats.add("related")

    for keyword in RELEVANCE_KEYWORDS["low"]:
        if keyword in combined:
            score += 3

    # Cap at 100
    score = min(score, 100)

    # Categorize
    cats = []
    if any(k in combined for k in ["housing", "bolig", "dwelling", "residential", "social housing"]):
        cats.append("housing")
    if any(k in combined for k in ["heritage", "preservation", "conservation", "reuse", "retrofit"]):
        cats.append("heritage")
    if any(k in combined for k in ["urban", "city", "neighborhood", "placemaking"]):
        cats.append("urban")
    if any(k in combined for k in ["postwar", "post-war", "modernist", "brutali", "miljonprogram"]):
        cats.append("postwar")
    if any(k in combined for k in ["theory", "history", "critique", "discourse"]):
        cats.append("theory")
    if any(k in combined for k in ["phd", "doctoral", "fellowship", "grant", "funding"]):
        cats.append("opportunity")
    if any(k in combined for k in ["conference", "symposium", "workshop", "seminar"]):
        cats.append("event")
    if not cats and score > 0:
        cats.append("architecture")

    return score, cats


# --- RSS feed helper ---

def _parse_rss(url, source, rtype="cfp"):
    """Parse RSS/Atom feed and return standardized results."""
    results = []
    r = _get(url, timeout=20)
    if not r:
        return results
    soup = BeautifulSoup(r.text, "xml")
    if not soup.find("item") and not soup.find("entry"):
        soup = BeautifulSoup(r.text, "html.parser")
    items = soup.find_all("item") or soup.find_all("entry")
    for item in items[:30]:
        title_el = item.find("title")
        if not title_el:
            continue
        title = _clean_text(title_el.get_text())
        link_el = item.find("link")
        link = ""
        if link_el:
            link = link_el.get("href", "") or link_el.get_text("").strip()
        desc_el = item.find("description") or item.find("summary") or item.find("content")
        desc = ""
        if desc_el:
            desc_text = desc_el.get_text()
            desc = _clean_text(desc_text[:300])
        if not _is_junk(title, desc):
            score, cats = _score_relevance(title, desc)
            results.append({
                "title": title[:150], "type": rtype,
                "description": desc[:200], "deadline": "",
                "url": link, "source": source,
                "relevance_score": score, "category": ", ".join(cats),
            })
    return results


# --- Scrapers ---

def scrape_eflux():
    results = []
    for section in ["announcements", "architecture"]:
        r = _get(f"https://www.e-flux.com/{section}/")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article, [class*='Item'], [class*='card']")
        if not items:
            items = soup.find_all("a", href=re.compile(r"/(announcements|architecture)/\d+"))
        for item in items[:40]:
            a_tag = item.find("a", href=True) if item.name != "a" else item
            if not a_tag or not a_tag.get("href"):
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = _clean_text(title_el.get_text())
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://www.e-flux.com{href}"
            desc_el = item.find("p")
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc) and title.lower() != "e-flux":
                score, cats = _score_relevance(title, desc)
                results.append({
                    "title": title[:150], "type": "cfp",
                    "description": desc, "deadline": "",
                    "url": href, "source": "e-flux",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_archinect():
    results = []
    for path in ["/competitions", "/features"]:
        r = _get(f"https://archinect.com{path}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article, [class*='ListItem'], [class*='card'], .item")
        if not items:
            items = soup.find_all("a", href=re.compile(r"/(competitions|features)/"))
        for item in items[:30]:
            a_tag = item.find("a", href=True) if item.name != "a" else item
            if not a_tag or not a_tag.get("href"):
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = _clean_text(title_el.get_text())
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://archinect.com{href}"
            desc_el = item.find("p")
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                results.append({
                    "title": title[:150], "type": "competition",
                    "description": desc, "deadline": "",
                    "url": href, "source": "Archinect",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_findaphd():
    results = []
    for q in ["architecture+housing", "urban+design+heritage",
              "adaptive+reuse", "postwar+housing+architecture",
              "social+housing+renovation", "built+environment+sustainability",
              "architectural+history+conservation"]:
        r = _get(f"https://www.findaphd.com/phds/?Keywords={q}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select(".resultsRow, .phd-result, [class*='Result'], article")
        for item in items[:10]:
            title_el = item.find(["h3", "h4", "a"])
            if not title_el:
                continue
            title = _clean_text(title_el.get_text())
            a_tag = item.find("a", href=True)
            link = ""
            if a_tag:
                link = a_tag["href"]
                if not link.startswith("http"):
                    link = f"https://www.findaphd.com{link}"
            desc_el = item.find("p")
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                if "opportunity" not in cats:
                    cats.append("opportunity")
                results.append({
                    "title": title[:150], "type": "phd",
                    "description": desc, "deadline": "",
                    "url": link, "source": "FindAPhD",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_wikicfp():
    results = []
    for term in ["architecture", "urban+design", "heritage+building", "housing+urban",
                  "social+housing", "adaptive+reuse", "built+environment"]:
        r = _get(f"http://www.wikicfp.com/cfp/servlet/tool.search?q={term}&year=f")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            link_el = cells[0].find("a")
            if not link_el:
                continue
            title = _clean_text(link_el.get_text())
            if not title or len(title) < 3:
                continue
            href = link_el.get("href", "")
            if href and not href.startswith("http"):
                href = f"http://www.wikicfp.com{href}"
            desc = _clean_text(cells[1].get_text()[:200]) if len(cells) > 1 else ""
            deadline = ""
            for cell in cells:
                text = cell.get_text(strip=True)
                if re.search(r"\d{4}-\d{2}-\d{2}|\w+ \d{1,2},? \d{4}", text):
                    deadline = text
                    break
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                results.append({
                    "title": title[:150], "type": "cfp",
                    "description": desc, "deadline": deadline,
                    "url": href, "source": "WikiCFP",
                    "relevance_score": score, "category": ", ".join(cats),
                })
        if len(results) >= 25:
            break
    return results


def scrape_euraxess():
    results = []
    for kw in ["architecture+urban+design", "housing+heritage+building"]:
        r = _get(f"https://euraxess.ec.europa.eu/jobs/search?keywords={kw}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select(".views-row, article, .node, .card")
        for item in items[:10]:
            title_el = item.find(["h2", "h3", "h4", "a"])
            if not title_el:
                continue
            title = _clean_text(title_el.get_text())
            a_tag = title_el if title_el.name == "a" else item.find("a", href=True)
            link = ""
            if a_tag and a_tag.get("href"):
                link = a_tag["href"]
                if not link.startswith("http"):
                    link = f"https://euraxess.ec.europa.eu{link}"
            desc_el = item.find(class_=re.compile(r"field|body|summary", re.I))
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                results.append({
                    "title": title[:150], "type": "fellowship",
                    "description": desc, "deadline": "",
                    "url": link, "source": "EURAXESS",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_jobs_ac_uk():
    results = []
    r = _get("https://www.jobs.ac.uk/search/?keywords=architecture+housing+phd")
    if not r:
        return results
    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select(".job-result, .search-result, article, [class*='job']")
    for item in items[:10]:
        title_el = item.find(["h3", "h4", "a"])
        if not title_el:
            continue
        title = _clean_text(title_el.get_text())
        a_tag = item.find("a", href=True)
        link = ""
        if a_tag:
            link = a_tag["href"]
            if not link.startswith("http"):
                link = f"https://www.jobs.ac.uk{link}"
        desc_el = item.find("p")
        desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
        if not _is_junk(title, desc):
            score, cats = _score_relevance(title, desc)
            results.append({
                "title": title[:150], "type": "phd",
                "description": desc, "deadline": "",
                "url": link, "source": "jobs.ac.uk",
                "relevance_score": score, "category": ", ".join(cats),
            })
    return results


def scrape_archdaily():
    """Scrape ArchDaily competitions and calls."""
    results = []
    for path in ["/search/projects/categories/housing",
                 "/tag/adaptive-reuse", "/tag/social-housing",
                 "/tag/renovation", "/tag/heritage"]:
        r = _get(f"https://www.archdaily.com{path}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article, [class*='card'], [class*='afd-search-list__item'], li[class*='result']")
        for item in items[:25]:
            a_tag = item.find("a", href=True) if item.name != "a" else item
            if not a_tag or not a_tag.get("href"):
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = _clean_text(title_el.get_text())
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://www.archdaily.com{href}"
            desc_el = item.find("p")
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                results.append({
                    "title": title[:150], "type": "project",
                    "description": desc, "deadline": "",
                    "url": href, "source": "ArchDaily",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_competitionline():
    """Scrape competitionline.com for architecture competitions."""
    results = []
    for path in ["/en/competitions/", "/en/competitions/results/"]:
        r = _get(f"https://www.competitionline.com{path}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article, .item, [class*='competition'], [class*='card'], .teaser")
        for item in items[:25]:
            a_tag = item.find("a", href=True) if item.name != "a" else item
            if not a_tag or not a_tag.get("href"):
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = _clean_text(title_el.get_text())
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://www.competitionline.com{href}"
            desc_el = item.find("p")
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                results.append({
                    "title": title[:150], "type": "competition",
                    "description": desc, "deadline": "",
                    "url": href, "source": "competitionline",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_cordis():
    """Scrape EU CORDIS for architecture/housing research projects and calls."""
    results = []
    queries = [
        "architecture+housing+urban",
        "heritage+building+renovation",
        "social+housing+adaptive+reuse",
        "urban+transformation+sustainability",
    ]
    for q in queries:
        r = _get(f"https://cordis.europa.eu/search?q={q}&type=project")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article, .result, [class*='card'], [class*='search-result'], .item")
        for item in items[:15]:
            a_tag = item.find("a", href=True)
            if not a_tag:
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = _clean_text(title_el.get_text())
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://cordis.europa.eu{href}"
            desc_el = item.find("p")
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                results.append({
                    "title": title[:150], "type": "eu_project",
                    "description": desc, "deadline": "",
                    "url": href, "source": "CORDIS",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_scholarshipdb():
    """Scrape ScholarshipDB for architecture PhD opportunities."""
    results = []
    for q in ["architecture+phd", "urban+design+phd", "housing+heritage+phd"]:
        r = _get(f"https://www.scholarshipdb.net/scholarships?q={q}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article, .scholarship, [class*='result'], [class*='card'], .item")
        for item in items[:15]:
            a_tag = item.find("a", href=True)
            if not a_tag:
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = _clean_text(title_el.get_text())
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://www.scholarshipdb.net{href}"
            desc_el = item.find("p")
            desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
            if not _is_junk(title, desc):
                score, cats = _score_relevance(title, desc)
                if "opportunity" not in cats:
                    cats.append("opportunity")
                results.append({
                    "title": title[:150], "type": "phd",
                    "description": desc, "deadline": "",
                    "url": href, "source": "ScholarshipDB",
                    "relevance_score": score, "category": ", ".join(cats),
                })
    return results


def scrape_rss_feeds():
    """Scrape RSS feeds — these are the most reliable sources."""
    results = []
    feeds = [
        # e-flux architecture RSS
        ("https://www.e-flux.com/rss/", "e-flux", "cfp"),
        # ArchDaily RSS
        ("https://www.archdaily.com/search/api/v1/us/articles?q=housing&type=news&format=rss", "ArchDaily", "project"),
        # Archinect RSS
        ("https://archinect.com/news.xml", "Archinect", "cfp"),
        # AJAR (Arena Journal of Architectural Research) — open access
        ("https://ajar.arena-architecture.eu/feed/", "AJAR", "cfp"),
        # Places Journal RSS
        ("https://placesjournal.org/feed/", "Places Journal", "essay"),
        # Failed Architecture RSS
        ("https://failedarchitecture.com/feed/", "Failed Architecture", "essay"),
        # The Funambulist RSS
        ("https://thefunambulist.net/feed", "The Funambulist", "essay"),
        # Dezeen architecture RSS
        ("https://www.dezeen.com/architecture/feed/", "Dezeen", "project"),
        # DOCOMOMO (modern heritage)
        ("https://docomomo.com/feed/", "DOCOMOMO", "cfp"),
    ]
    for url, source, rtype in feeds:
        try:
            items = _parse_rss(url, source, rtype)
            results.extend(items)
            log.info("  RSS %s: %d results", source, len(items))
        except Exception as e:
            log.warning("  RSS %s: failed — %s", source, e)
    return results


def get_curated_calls():
    """Known, manually curated open calls and standing submission opportunities.
    Updated periodically. These are real, verified opportunities."""
    return [
        {
            "title": "AJAR — Arena Journal of Architectural Research (rolling submissions)",
            "type": "cfp", "source": "Curated",
            "description": "Open-access, double-blind peer-reviewed journal. Accepts essays, design research, and technical research on architecture on a rolling basis.",
            "deadline": "Rolling", "url": "https://ajar.arena-architecture.eu/",
            "relevance_score": 60, "category": "architecture, opportunity",
        },
        {
            "title": "The Journal of Architecture — Rolling thematic calls for papers",
            "type": "cfp", "source": "Curated",
            "description": "Taylor & Francis. Seeks theoretical, historical, and empirical research that challenges disciplinary assumptions in architecture.",
            "deadline": "Rolling", "url": "https://www.tandfonline.com/toc/rjar20/current",
            "relevance_score": 55, "category": "architecture, opportunity",
        },
        {
            "title": "Scaffold Journal — Open call for visual essays",
            "type": "cfp", "source": "Curated",
            "description": "Calling for submissions including visual essays that explore architectural research through creative means.",
            "deadline": "Rolling", "url": "https://www.scaffold-journal.com/",
            "relevance_score": 50, "category": "architecture, opportunity",
        },
        {
            "title": "Places Journal — Accepting pitches (public scholarship on architecture & urbanism)",
            "type": "cfp", "source": "Curated",
            "description": "Open to pitches on architecture, landscape, and urbanism. Peer-reviewed, pays contributors. Strong fit for housing and urban transformation research.",
            "deadline": "Rolling", "url": "https://placesjournal.org/about/submissions/",
            "relevance_score": 75, "category": "housing, urban, opportunity",
        },
        {
            "title": "The Avery Review — Essay submissions (critical essays on architecture)",
            "type": "cfp", "source": "Curated",
            "description": "Invites critical essays on books, buildings, and other architectural media. Focus on students and early-career researchers.",
            "deadline": "Rolling", "url": "https://averyreview.com/about",
            "relevance_score": 60, "category": "architecture, theory, opportunity",
        },
        {
            "title": "OASE Journal — Thematic open calls for architecture essays",
            "type": "cfp", "source": "Curated",
            "description": "Dutch/Belgian architectural journal. Publishes thematic issues on architecture, urbanism, and landscape. Regular open calls.",
            "deadline": "Check website", "url": "https://www.oasejournal.nl/en/Submit",
            "relevance_score": 65, "category": "architecture, urban, heritage, opportunity",
        },
        {
            "title": "Housing Studies — Paper submissions (Taylor & Francis)",
            "type": "cfp", "source": "Curated",
            "description": "Academic journal on housing policy, markets, and built environments. Accepts papers on social housing, renovation, and housing transformation.",
            "deadline": "Rolling", "url": "https://www.tandfonline.com/toc/chos20/current",
            "relevance_score": 80, "category": "housing, opportunity",
        },
        {
            "title": "Urban Studies — Paper submissions (SAGE)",
            "type": "cfp", "source": "Curated",
            "description": "Interdisciplinary journal on urban issues. Accepts papers on housing, urban transformation, gentrification, and spatial justice.",
            "deadline": "Rolling", "url": "https://journals.sagepub.com/home/usj",
            "relevance_score": 70, "category": "urban, housing, opportunity",
        },
        {
            "title": "Architectural Research Quarterly (arq) — Cambridge University Press",
            "type": "cfp", "source": "Curated",
            "description": "Publishes architectural research including housing studies, heritage, and adaptive reuse. Peer-reviewed.",
            "deadline": "Rolling", "url": "https://www.cambridge.org/core/journals/arq-architectural-research-quarterly",
            "relevance_score": 65, "category": "architecture, heritage, opportunity",
        },
        {
            "title": "Log Journal — Unsolicited manuscripts accepted",
            "type": "cfp", "source": "Curated",
            "description": "Critical journal of architecture theory. Accepts unsolicited manuscripts on architectural theory, history, and criticism.",
            "deadline": "Rolling", "url": "https://www.anycorp.com/log",
            "relevance_score": 50, "category": "architecture, theory, opportunity",
        },
        {
            "title": "Conditions Magazine — Nordic architecture and spatial practice",
            "type": "cfp", "source": "Curated",
            "description": "Publication focused on Nordic architecture. Relevant for research on Scandinavian housing and spatial practice.",
            "deadline": "Rolling", "url": "https://conditionsmagazine.com",
            "relevance_score": 70, "category": "architecture, urban, opportunity",
        },
        {
            "title": "DOCOMOMO Journal — Modern Movement heritage",
            "type": "cfp", "source": "Curated",
            "description": "Documentation and Conservation of buildings of the Modern Movement. Accepts papers on postwar architecture preservation.",
            "deadline": "Rolling", "url": "https://docomomo.com/journal/",
            "relevance_score": 80, "category": "postwar, heritage, architecture, opportunity",
        },
        {
            "title": "FindAPhD — Architecture & Housing PhD positions (live search)",
            "type": "phd", "source": "Curated",
            "description": "Search for current PhD positions in architecture, housing, urban design, and heritage. Updated daily.",
            "deadline": "Varies", "url": "https://www.findaphd.com/phds/?Keywords=architecture+housing+urban",
            "relevance_score": 85, "category": "housing, architecture, opportunity",
        },
        {
            "title": "EURAXESS — PhD/postdoc positions in architecture & built environment",
            "type": "phd", "source": "Curated",
            "description": "EU-funded research positions. Filter by architecture, urban studies, and heritage.",
            "deadline": "Varies", "url": "https://euraxess.ec.europa.eu/jobs/search?keywords=architecture+housing+urban",
            "relevance_score": 80, "category": "architecture, urban, opportunity",
        },
        {
            "title": "jobs.ac.uk — UK academic positions in architecture",
            "type": "phd", "source": "Curated",
            "description": "UK university jobs in architecture, planning, and built environment. PhD studentships regularly posted.",
            "deadline": "Varies", "url": "https://www.jobs.ac.uk/search/?keywords=architecture+housing+phd",
            "relevance_score": 70, "category": "architecture, housing, opportunity",
        },
    ]


def scrape_all_sources():
    all_results = []

    # 1. Curated calls (always reliable)
    all_results.extend(get_curated_calls())
    log.info("  Curated: %d results", len(get_curated_calls()))

    # 2. RSS feeds (reliable)
    try:
        rss_results = scrape_rss_feeds()
        all_results.extend(rss_results)
    except Exception as e:
        log.warning("  RSS feeds failed: %s", e)

    # 3. HTML scrapers (best effort, may fail)
    scrapers = [
        ("e-flux", scrape_eflux),
        ("Archinect", scrape_archinect),
        ("FindAPhD", scrape_findaphd),
        ("WikiCFP", scrape_wikicfp),
        ("EURAXESS", scrape_euraxess),
        ("jobs.ac.uk", scrape_jobs_ac_uk),
        ("ArchDaily", scrape_archdaily),
        ("competitionline", scrape_competitionline),
        ("CORDIS", scrape_cordis),
        ("ScholarshipDB", scrape_scholarshipdb),
    ]
    for name, fn in scrapers:
        try:
            items = fn()
            all_results.extend(items)
            log.info("  %s: %d results", name, len(items))
        except Exception as e:
            log.warning("  %s: failed — %s", name, e)

    # Deduplicate by normalized title
    seen = set()
    unique = []
    for c in all_results:
        key = re.sub(r'\s+', ' ', c["title"].lower().strip())
        if key not in seen and len(key) > 4:
            seen.add(key)
            unique.append(c)

    # Sort by relevance score descending
    unique.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    # Filter: only keep items with score > 0 (at least loosely related)
    relevant = [c for c in unique if c.get("relevance_score", 0) > 0]

    log.info("Scraped %d total, %d unique, %d relevant",
             len(all_results), len(unique), len(relevant))

    return relevant if relevant else unique[:30]


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

def job_scan_open_calls():
    log.info("Running scheduled open-calls scan")
    with app.app_context():
        db = get_db()
        scraped = scrape_all_sources()
        if not scraped:
            log.warning("No results scraped")
            return
        for c in scraped[:50]:
            existing = db.execute("SELECT 1 FROM open_calls WHERE title=? AND source=?",
                                  (c["title"], c["source"])).fetchone()
            if existing:
                continue
            db.execute(
                "INSERT INTO open_calls (title, type, category, description, deadline, url, source, relevance_score) VALUES (?,?,?,?,?,?,?,?)",
                (c["title"], c["type"], c.get("category", ""),
                 c["description"], c["deadline"], c["url"],
                 c["source"], c.get("relevance_score", 0)))
        db.commit()
        log.info("Added up to %d open calls", min(len(scraped), 50))


def job_generate_drafts():
    log.info("Running scheduled draft generation")
    with app.app_context():
        db = get_db()
        ctx = build_system_context()
        types = [
            ("substack", "Write a short Substack post (300-400 words) reflecting on the research angle. Thoughtful, personal, academic but accessible."),
            ("linkedin", "Write a LinkedIn update (150-200 words) sharing a research insight or milestone. Professional but warm."),
            ("outreach", "Write an outreach email (200-300 words) to a potential PhD supervisor at one of the listed programs. Be specific about their work and propose a connection."),
        ]
        voice = db.execute("SELECT value FROM profile WHERE key='voice_profile'").fetchone()
        voice_instruction = ""
        if voice and voice["value"]:
            voice_instruction = f"\n\nWrite in this voice/style:\n{voice['value']}"
        for dtype, instruction in types:
            prompt = instruction + voice_instruction + '\n\nReturn valid JSON: {"title": "...", "body": "..."}'
            raw = ollama_generate(prompt, system=ctx, as_json=True)
            data = parse_json_response(raw)
            if not data or not isinstance(data, dict):
                data = {"title": f"Draft {dtype}", "body": raw}
            db.execute("INSERT INTO inbox (type, title, body) VALUES (?,?,?)",
                       (dtype, data.get("title", f"Draft {dtype}"), data.get("body", "")))
        db.commit()
        log.info("Generated 3 inbox drafts")


# ---------------------------------------------------------------------------
# Routes — Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    db = get_db()
    angle = db.execute("SELECT value FROM profile WHERE key='research_angle'").fetchone()
    programs = query_db("SELECT * FROM programs ORDER BY deadline")
    tasks = query_db("SELECT * FROM tasks ORDER BY done, priority='high' DESC, deadline")
    total_tasks = len(tasks)
    done_tasks = sum(1 for t in tasks if t["done"])
    upcoming_deadlines = query_db(
        "SELECT name, deadline FROM programs WHERE deadline >= date('now') ORDER BY deadline LIMIT 5")
    inbox_pending = query_db("SELECT COUNT(*) as c FROM inbox WHERE status='pending'", one=True)["c"]
    open_calls_count = query_db("SELECT COUNT(*) as c FROM open_calls", one=True)["c"]
    saved_calls = query_db("SELECT COUNT(*) as c FROM open_calls WHERE saved=1", one=True)["c"]

    # Days until next deadline
    days_left = None
    if upcoming_deadlines:
        try:
            next_dl = datetime.strptime(upcoming_deadlines[0]["deadline"], "%Y-%m-%d")
            days_left = (next_dl - datetime.now()).days
        except (ValueError, TypeError):
            pass

    # Suggested next actions based on current state
    next_actions = []
    if not query_db("SELECT 1 FROM voice_samples LIMIT 1"):
        next_actions.append({"text": "Train your voice — submit a writing sample", "url": "/voice"})
    undone_high = [t for t in tasks if not t["done"] and t["priority"] == "high"]
    if undone_high:
        next_actions.append({"text": f"Focus: {undone_high[0]['title']}", "url": "/tasks"})
    if inbox_pending > 0:
        next_actions.append({"text": f"Review {inbox_pending} pending draft(s)", "url": "/inbox"})
    if not query_db("SELECT 1 FROM open_calls LIMIT 1"):
        next_actions.append({"text": "Scan for open calls & opportunities", "url": "/open-calls"})

    gstats = get_gamification_stats(db)

    return render_template("dashboard.html",
                           angle=angle["value"] if angle else "",
                           programs=programs,
                           tasks=tasks,
                           total_tasks=total_tasks,
                           done_tasks=done_tasks,
                           upcoming_deadlines=upcoming_deadlines,
                           inbox_pending=inbox_pending,
                           open_calls_count=open_calls_count,
                           saved_calls=saved_calls,
                           days_left=days_left,
                           next_actions=next_actions,
                           g=gstats)


@app.route("/api/profile", methods=["POST"])
def update_profile():
    data = request.json
    db = get_db()
    db.execute("INSERT OR REPLACE INTO profile (key, value) VALUES ('research_angle', ?)",
               (data.get("research_angle", ""),))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — Programs
# ---------------------------------------------------------------------------

@app.route("/programs")
def programs_page():
    programs = query_db("SELECT * FROM programs ORDER BY fit_score DESC")
    return render_template("programs.html", programs=programs)


@app.route("/api/programs", methods=["POST"])
def create_program():
    d = request.json
    db = get_db()
    default_checklist = json.dumps({
        "Research program requirements": False,
        "Identify potential supervisor": False,
        "Draft research proposal": False,
        "Prepare writing sample": False,
        "Request recommendation letters": False,
        "Prepare portfolio": False,
        "Submit application": False,
    })
    db.execute(
        "INSERT INTO programs (name, country, deadline, status, fit_score, notes, supervisor, checklist) VALUES (?,?,?,?,?,?,?,?)",
        (d["name"], d.get("country", ""), d.get("deadline", ""),
         d.get("status", "researching"), d.get("fit_score", 0),
         d.get("notes", ""), d.get("supervisor", ""), default_checklist))
    xp, total, achievements = award_xp(db, "add_program", d["name"])
    db.commit()
    return jsonify(ok=True, xp=xp, achievements=[a["title"] for a in achievements])


@app.route("/api/programs/<int:pid>", methods=["PUT"])
def update_program(pid):
    d = request.json
    db = get_db()
    db.execute("""UPDATE programs SET name=?, country=?, deadline=?, status=?, fit_score=?, notes=?, supervisor=?
                  WHERE id=?""",
               (d["name"], d.get("country", ""), d.get("deadline", ""),
                d.get("status", "researching"), d.get("fit_score", 0),
                d.get("notes", ""), d.get("supervisor", ""), pid))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/programs/<int:pid>/checklist", methods=["POST"])
def update_checklist(pid):
    d = request.json
    db = get_db()
    db.execute("UPDATE programs SET checklist=? WHERE id=?", (json.dumps(d.get("checklist", {})), pid))
    award_xp(db, "checklist_item")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/programs/<int:pid>", methods=["DELETE"])
def delete_program(pid):
    db = get_db()
    db.execute("DELETE FROM programs WHERE id=?", (pid,))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — Tasks
# ---------------------------------------------------------------------------

@app.route("/tasks")
def tasks_page():
    tasks = query_db("SELECT * FROM tasks ORDER BY done, priority='high' DESC, deadline")
    return render_template("tasks.html", tasks=tasks)


@app.route("/api/tasks", methods=["POST"])
def create_task():
    d = request.json
    db = get_db()
    db.execute("INSERT INTO tasks (title, priority, category, deadline) VALUES (?,?,?,?)",
               (d["title"], d.get("priority", "medium"), d.get("category", "writing"),
                d.get("deadline", "")))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/tasks/<int:tid>", methods=["PUT"])
def update_task(tid):
    d = request.json
    db = get_db()
    db.execute("UPDATE tasks SET title=?, priority=?, category=?, deadline=?, done=? WHERE id=?",
               (d["title"], d.get("priority", "medium"), d.get("category", "writing"),
                d.get("deadline", ""), d.get("done", 0), tid))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/tasks/<int:tid>/toggle", methods=["POST"])
def toggle_task(tid):
    db = get_db()
    task = db.execute("SELECT done FROM tasks WHERE id=?", (tid,)).fetchone()
    db.execute("UPDATE tasks SET done = NOT done WHERE id=?", (tid,))
    if task and not task["done"]:  # Was undone, now completing
        xp, total, achievements = award_xp(db, "complete_task")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
def delete_task(tid):
    db = get_db()
    # Clean up attachments
    task = db.execute("SELECT attachments FROM tasks WHERE id=?", (tid,)).fetchone()
    if task and task["attachments"]:
        try:
            files = json.loads(task["attachments"])
            for f in files:
                fpath = os.path.join(app.config["UPLOAD_FOLDER"], f["filename"])
                if os.path.exists(fpath):
                    os.remove(fpath)
        except (json.JSONDecodeError, KeyError):
            pass
    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/tasks/<int:tid>/upload", methods=["POST"])
def upload_task_file(tid):
    """Upload a file attachment to a task."""
    if "file" not in request.files:
        return jsonify(error="No file provided"), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify(error="Empty filename"), 400
    # Secure the filename and make it unique
    original = secure_filename(file.filename)
    unique_name = f"{tid}_{int(datetime.now().timestamp())}_{original}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
    db = get_db()
    task = db.execute("SELECT attachments FROM tasks WHERE id=?", (tid,)).fetchone()
    attachments = []
    if task and task["attachments"]:
        try:
            attachments = json.loads(task["attachments"])
        except json.JSONDecodeError:
            attachments = []
    attachments.append({"filename": unique_name, "original": original,
                        "uploaded": datetime.now().isoformat()})
    db.execute("UPDATE tasks SET attachments=? WHERE id=?", (json.dumps(attachments), tid))
    db.commit()
    return jsonify(ok=True, filename=unique_name, original=original)


@app.route("/api/tasks/<int:tid>/files/<filename>", methods=["DELETE"])
def delete_task_file(tid, filename):
    """Remove a file attachment from a task."""
    db = get_db()
    task = db.execute("SELECT attachments FROM tasks WHERE id=?", (tid,)).fetchone()
    if not task:
        return jsonify(error="Task not found"), 404
    attachments = json.loads(task["attachments"] or "[]")
    attachments = [a for a in attachments if a["filename"] != filename]
    db.execute("UPDATE tasks SET attachments=? WHERE id=?", (json.dumps(attachments), tid))
    db.commit()
    fpath = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))
    if os.path.exists(fpath):
        os.remove(fpath)
    return jsonify(ok=True)


@app.route("/uploads/<filename>")
def serve_upload(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], secure_filename(filename))


# ---------------------------------------------------------------------------
# Routes — Open Calls
# ---------------------------------------------------------------------------

@app.route("/open-calls")
def open_calls_page():
    calls = query_db("SELECT * FROM open_calls ORDER BY relevance_score DESC, created DESC")
    return render_template("open_calls.html", calls=calls)


@app.route("/api/open-calls/generate", methods=["POST"])
def generate_open_calls():
    scraped = scrape_all_sources()
    if not scraped:
        return jsonify(error="Could not reach any source. Check your internet connection."), 500

    db = get_db()
    added = 0
    for c in scraped[:50]:
        # Skip duplicates already in DB
        existing = db.execute("SELECT 1 FROM open_calls WHERE title=? AND source=?",
                              (c["title"], c["source"])).fetchone()
        if existing:
            continue
        db.execute(
            "INSERT INTO open_calls (title, type, category, description, deadline, url, source, relevance_score) VALUES (?,?,?,?,?,?,?,?)",
            (c["title"], c["type"], c.get("category", ""),
             c["description"], c["deadline"], c["url"],
             c["source"], c.get("relevance_score", 0)))
        added += 1
    award_xp(db, "scan_calls")
    db.commit()
    return jsonify(ok=True, count=added)


@app.route("/api/open-calls/<int:cid>/save", methods=["POST"])
def save_open_call(cid):
    db = get_db()
    call = db.execute("SELECT saved FROM open_calls WHERE id=?", (cid,)).fetchone()
    db.execute("UPDATE open_calls SET saved = NOT saved WHERE id=?", (cid,))
    if call and not call["saved"]:  # Was unsaved, now saving
        award_xp(db, "save_call")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/open-calls/<int:cid>", methods=["DELETE"])
def delete_open_call(cid):
    db = get_db()
    db.execute("DELETE FROM open_calls WHERE id=?", (cid,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/open-calls/clear-low", methods=["POST"])
def clear_low_relevance():
    db = get_db()
    db.execute("DELETE FROM open_calls WHERE relevance_score < 10 AND saved = 0")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/open-calls/search", methods=["POST"])
def keyword_search_calls():
    """Custom keyword search across all scraper sources."""
    d = request.json
    keywords = d.get("keywords", "").strip()
    if not keywords:
        return jsonify(error="No keywords provided"), 400

    db = get_db()
    # Build search queries from user keywords
    terms = [kw.strip().replace(" ", "+") for kw in keywords.split(",") if kw.strip()]
    all_results = []

    for term in terms[:5]:  # Max 5 keyword groups
        for base_url, source, rtype in [
            ("https://www.findaphd.com/phds/?Keywords={}", "FindAPhD", "phd"),
            ("http://www.wikicfp.com/cfp/servlet/tool.search?q={}&year=f", "WikiCFP", "cfp"),
            ("https://euraxess.ec.europa.eu/jobs/search?keywords={}", "EURAXESS", "fellowship"),
            ("https://www.e-flux.com/announcements/?search={}", "e-flux", "cfp"),
            ("https://www.scholarshipdb.net/scholarships?q={}", "ScholarshipDB", "phd"),
            ("https://cordis.europa.eu/search?q={}&type=project", "CORDIS", "eu_project"),
        ]:
            r = _get(base_url.format(term))
            if not r:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            items = soup.select("article, .resultsRow, .phd-result, [class*='Result'], [class*='card'], .views-row, .item, tr")
            for item in items[:15]:
                title_el = item.find(["h2", "h3", "h4", "a"])
                if not title_el:
                    continue
                title = _clean_text(title_el.get_text())
                if not title or len(title) < 5:
                    continue
                a_tag = item.find("a", href=True)
                link = a_tag["href"] if a_tag else ""
                if link and not link.startswith("http"):
                    link = r.url.split("/")[0] + "//" + r.url.split("/")[2] + link
                desc_el = item.find("p")
                desc = _clean_text(desc_el.get_text()[:200]) if desc_el else ""
                if not _is_junk(title, desc):
                    score, cats = _score_relevance(title, desc)
                    # Boost score for keyword match
                    for kw in keywords.lower().split(","):
                        kw = kw.strip()
                        if kw and (kw in title.lower() or kw in desc.lower()):
                            score = min(100, score + 15)
                    all_results.append({
                        "title": title[:150], "type": rtype,
                        "description": desc, "deadline": "",
                        "url": link, "source": source,
                        "relevance_score": score, "category": ", ".join(cats),
                    })

    # Deduplicate
    seen = set()
    unique = []
    for c in all_results:
        key = re.sub(r'\s+', ' ', c["title"].lower().strip())
        if key not in seen and len(key) > 4:
            seen.add(key)
            unique.append(c)
    unique.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)

    # Store in DB
    added = 0
    for c in unique[:40]:
        existing = db.execute("SELECT 1 FROM open_calls WHERE title=? AND source=?",
                              (c["title"], c["source"])).fetchone()
        if existing:
            continue
        db.execute(
            "INSERT INTO open_calls (title, type, category, description, deadline, url, source, relevance_score) VALUES (?,?,?,?,?,?,?,?)",
            (c["title"], c["type"], c.get("category", ""),
             c["description"], c["deadline"], c["url"],
             c["source"], c.get("relevance_score", 0)))
        added += 1

    award_xp(db, "keyword_search", keywords)
    db.commit()
    return jsonify(ok=True, count=added, total_found=len(unique))


# ---------------------------------------------------------------------------
# Routes — Pitch Targets (proactive magazine/journal suggestions)
# ---------------------------------------------------------------------------

PITCH_TARGETS_SEED = [
    ("Architectural Review", "magazine", "https://www.architectural-review.com",
     "Long-form essays on architecture, urbanism, and building culture. Accepts unsolicited pitches."),
    ("Dezeen", "magazine", "https://www.dezeen.com",
     "World's most popular architecture magazine. Opinion section accepts contributor pieces."),
    ("Failed Architecture", "magazine", "https://failedarchitecture.com",
     "Critical platform about architecture and urban planning. Open to contributor essays."),
    ("The Funambulist", "magazine", "https://thefunambulist.net",
     "Politics of space and bodies. Publishes essays on spatial justice, housing, and urbanism."),
    ("Volume Magazine", "magazine", "https://volumeproject.org",
     "Architecture magazine focusing on spatial practice beyond buildings."),
    ("e-flux Architecture", "magazine", "https://www.e-flux.com/architecture/",
     "Critical essays on architecture theory and practice. Invitation + pitch."),
    ("PLOT Magazine", "magazine", "https://www.plotmag.com",
     "Latin American architecture magazine. Open to international contributors."),
    ("Metropolis", "magazine", "https://metropolismag.com",
     "Design and architecture coverage. Accepts freelance pitches on housing and urban topics."),
    ("Places Journal", "journal", "https://placesjournal.org",
     "Public scholarship on architecture, landscape, and urbanism. Peer-reviewed, open to pitches."),
    ("Journal of Architecture", "journal", "https://www.tandfonline.com/toc/rjar20/current",
     "Academic journal. Publishes research on architectural history, theory, and design."),
    ("Urban Studies", "journal", "https://journals.sagepub.com/home/usj",
     "Interdisciplinary journal on urban policy, planning, and social aspects of cities."),
    ("Housing Studies", "journal", "https://www.tandfonline.com/toc/chos20/current",
     "Academic journal on housing policy, markets, and built environments."),
    ("OASE Journal", "journal", "https://www.oasejournal.nl",
     "Architectural journal from Netherlands. Thematic issues on architecture and urbanism."),
    ("Conditions Magazine", "magazine", "https://conditionsmagazine.com",
     "Architecture and spatial practice publication. Focus on Nordic context."),
    ("Architect Sweden (Arkitektur)", "magazine", "https://arkitektur.se",
     "Swedish architecture magazine. Relevant for Nordic housing research."),
    ("Domus", "magazine", "https://www.domusweb.it",
     "Italian architecture and design magazine with international reach. Accepts pitches."),
    ("Log Journal", "journal", "https://www.anycorp.com/log",
     "Critical journal of architecture. Theory-heavy, accepts unsolicited manuscripts."),
    ("arq: Architectural Research Quarterly", "journal", "https://www.cambridge.org/core/journals/arq-architectural-research-quarterly",
     "Cambridge University Press. Publishes architectural research including housing studies."),
]


@app.route("/pitch-targets")
def pitch_targets_page():
    db = get_db()
    # Seed pitch targets if table is empty
    existing = db.execute("SELECT COUNT(*) FROM pitch_targets").fetchone()[0]
    if existing == 0:
        for name, ptype, url, desc in PITCH_TARGETS_SEED:
            db.execute("INSERT INTO pitch_targets (name, type, url, description) VALUES (?,?,?,?)",
                       (name, ptype, url, desc))
        db.commit()
    targets = query_db("SELECT * FROM pitch_targets ORDER BY saved DESC, name")
    return render_template("pitch_targets.html", targets=targets)


@app.route("/api/pitch-targets/<int:pid>/save", methods=["POST"])
def save_pitch_target(pid):
    db = get_db()
    db.execute("UPDATE pitch_targets SET saved = NOT saved WHERE id=?", (pid,))
    award_xp(db, "save_pitch")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/pitch-targets/suggest", methods=["POST"])
def suggest_pitch():
    """Use AI to suggest a pitch angle for a specific magazine/journal."""
    d = request.json
    target_id = d.get("target_id")
    db = get_db()
    target = db.execute("SELECT * FROM pitch_targets WHERE id=?", (target_id,)).fetchone()
    if not target:
        return jsonify(error="Target not found"), 404
    ctx = build_system_context()
    voice = db.execute("SELECT value FROM profile WHERE key='voice_profile'").fetchone()
    voice_instruction = ""
    if voice and voice["value"]:
        voice_instruction = f"\n\nWrite in this voice/style:\n{voice['value']}"
    prompt = f"""You are a sharp editorial strategist. Based on the research context above, write an actual pitch email for:

Publication: {target['name']} ({target['type']})
About this publication: {target['description']}

IMPORTANT RULES:
- Write as if you are the researcher pitching to a real editor
- The pitch must be SPECIFIC — not generic. Name concrete examples, places, buildings, debates
- Do NOT use vague phrases like "this article will examine" or "this piece will explore"
- Instead, LEAD with the argument: what is the actual claim or provocation?
- Reference specific buildings (Rågsved, Tensta, Rosengård, etc.), architects, policies, or debates
- The title should be punchy and editorial, not academic
- Keep it under 200 words — editors want brevity
- End with 1-2 sentences about who you are and why you're the right person to write this{voice_instruction}

Return JSON: {{"title": "article title (punchy, editorial)", "pitch": "the pitch email body"}}"""
    raw = ollama_generate(prompt, system=ctx, as_json=True)
    data = parse_json_response(raw)
    if not data or not isinstance(data, dict):
        data = {"title": "Pitch draft", "pitch": raw}
    return jsonify(ok=True, title=data.get("title", ""), pitch=data.get("pitch", ""))


# ---------------------------------------------------------------------------
# Routes — Inbox
# ---------------------------------------------------------------------------

@app.route("/inbox")
def inbox_page():
    items = query_db("SELECT * FROM inbox ORDER BY status='pending' DESC, created DESC")
    return render_template("inbox.html", items=items)


@app.route("/api/inbox/generate", methods=["POST"])
def generate_inbox_draft():
    d = request.json
    dtype = d.get("type", "substack")
    db = get_db()
    ctx = build_system_context()
    voice = db.execute("SELECT value FROM profile WHERE key='voice_profile'").fetchone()
    voice_instruction = ""
    if voice and voice["value"]:
        voice_instruction = f"\n\nWrite in this voice/style:\n{voice['value']}"
    type_prompts = {
        "substack": "Write a short Substack post (300-400 words) reflecting on the research angle. Thoughtful, personal, academic but accessible.",
        "linkedin": "Write a LinkedIn update (150-200 words) sharing a research insight or milestone. Professional but warm.",
        "outreach": "Write an outreach email (200-300 words) to a potential PhD supervisor at one of the listed programs. Be specific about their work and propose a connection.",
    }
    prompt = type_prompts.get(dtype, type_prompts["substack"]) + voice_instruction
    prompt += '\n\nReturn valid JSON: {"title": "...", "body": "..."}'
    raw = ollama_generate(prompt, system=ctx, as_json=True)
    data = parse_json_response(raw)
    if not data or not isinstance(data, dict):
        data = {"title": f"Draft {dtype}", "body": raw}
    db.execute("INSERT INTO inbox (type, title, body) VALUES (?,?,?)",
               (dtype, data.get("title", ""), data.get("body", "")))
    award_xp(db, "generate_draft", dtype)
    db.commit()
    return jsonify(ok=True)


@app.route("/api/inbox/<int:iid>/<action>", methods=["POST"])
def inbox_action(iid, action):
    if action not in ("approve", "reject"):
        return jsonify(error="Invalid action"), 400
    db = get_db()
    status = "approved" if action == "approve" else "rejected"
    db.execute("UPDATE inbox SET status=? WHERE id=?", (status, iid))
    if action == "approve":
        award_xp(db, "approve_draft")
    db.commit()
    return jsonify(ok=True)


@app.route("/api/inbox/write", methods=["POST"])
def write_own_draft():
    """Let user write their own draft directly."""
    d = request.json
    dtype = d.get("type", "substack")
    title = d.get("title", "").strip()
    body = d.get("body", "").strip()
    if not body:
        return jsonify(error="Body cannot be empty"), 400
    if not title:
        title = f"My {dtype} draft"
    db = get_db()
    db.execute("INSERT INTO inbox (type, title, body) VALUES (?,?,?)", (dtype, title, body))
    award_xp(db, "write_own_draft", title)
    db.commit()
    return jsonify(ok=True)


@app.route("/api/inbox/<int:iid>/edit", methods=["PUT"])
def edit_inbox_draft(iid):
    """Let user edit an existing draft."""
    d = request.json
    db = get_db()
    db.execute("UPDATE inbox SET title=?, body=? WHERE id=?",
               (d.get("title", ""), d.get("body", ""), iid))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/inbox/<int:iid>", methods=["DELETE"])
def delete_inbox(iid):
    db = get_db()
    db.execute("DELETE FROM inbox WHERE id=?", (iid,))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — Voice Training
# ---------------------------------------------------------------------------

@app.route("/voice")
def voice_page():
    samples = query_db("SELECT * FROM voice_samples ORDER BY created DESC")
    profile = query_db("SELECT value FROM profile WHERE key='voice_profile'", one=True)
    return render_template("voice.html", samples=samples,
                           voice_profile=profile["value"] if profile else "")


@app.route("/api/voice/submit", methods=["POST"])
def submit_voice_sample():
    d = request.json
    text = d.get("text", "").strip()
    if not text:
        return jsonify(error="Empty text"), 400
    db = get_db()
    analysis_prompt = f"""Analyze this writing sample for voice characteristics. Identify:
- Tone (formal/informal/academic/personal)
- Sentence structure patterns
- Vocabulary level
- Stylistic quirks
- Emotional register

Text:
\"{text}\"

Return a concise voice profile paragraph (3-5 sentences) that could be used as a writing style guide."""
    analysis = ollama_generate(analysis_prompt)
    db.execute("INSERT INTO voice_samples (text, analysis) VALUES (?,?)", (text, analysis))
    all_samples = query_db("SELECT analysis FROM voice_samples")
    if len(all_samples) > 1:
        analyses = "\n---\n".join(s["analysis"] for s in all_samples if s["analysis"])
        merge_prompt = f"""Here are voice analyses from multiple writing samples:

{analyses}

Synthesize into ONE concise voice profile (4-6 sentences) capturing consistent style, tone, and patterns."""
        merged = ollama_generate(merge_prompt)
    else:
        merged = analysis
    db.execute("INSERT OR REPLACE INTO profile (key, value) VALUES ('voice_profile', ?)", (merged,))
    award_xp(db, "submit_voice")
    db.commit()
    return jsonify(ok=True, analysis=analysis)


@app.route("/api/voice/<int:vid>", methods=["DELETE"])
def delete_voice_sample(vid):
    db = get_db()
    db.execute("DELETE FROM voice_samples WHERE id=?", (vid,))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — Chat
# ---------------------------------------------------------------------------

@app.route("/chat")
def chat_page():
    history = query_db("SELECT * FROM chat_history ORDER BY created")
    return render_template("chat.html", history=history)


@app.route("/api/chat", methods=["POST"])
def chat_send():
    d = request.json
    user_msg = d.get("message", "").strip()
    if not user_msg:
        return jsonify(error="Empty message"), 400
    db = get_db()
    db.execute("INSERT INTO chat_history (role, content) VALUES ('user', ?)", (user_msg,))
    system = build_system_context()
    recent = query_db("SELECT role, content FROM chat_history ORDER BY created DESC LIMIT 20")
    recent.reverse()
    messages = [{"role": "system", "content": system}]
    for m in recent:
        messages.append({"role": m["role"], "content": m["content"]})
    reply = ollama_chat(messages)
    db.execute("INSERT INTO chat_history (role, content) VALUES ('assistant', ?)", (reply,))
    db.commit()
    return jsonify(ok=True, reply=reply)


@app.route("/api/gamification")
def gamification_api():
    db = get_db()
    return jsonify(get_gamification_stats(db))


@app.route("/api/chat/clear", methods=["POST"])
def chat_clear():
    db = get_db()
    db.execute("DELETE FROM chat_history")
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

init_db()

# Seed achievement definitions
_adb = sqlite3.connect(app.config["DATABASE"])
_adb.row_factory = sqlite3.Row
_seed_achievements(_adb)
_adb.close()

scheduler = BackgroundScheduler()
scheduler.add_job(job_scan_open_calls, "cron", hour=2, minute=0, id="nightly_calls")
scheduler.add_job(job_generate_drafts, "cron", day_of_week="mon", hour=7, minute=0, id="weekly_drafts")
scheduler.start()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
