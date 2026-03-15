import os
import re
import json
import sqlite3
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, g, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

app = Flask(__name__)
app.config["DATABASE"] = os.path.join(app.root_path, "phd.db")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")


@app.template_filter("rowtodict")
def rowtodict_filter(row):
    return dict(row)


app.jinja_env.filters["tojson_safe"] = lambda val: json.dumps(dict(val) if isinstance(val, sqlite3.Row) else val)

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
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL,
        country   TEXT,
        deadline  TEXT,
        status    TEXT DEFAULT 'researching',
        fit_score INTEGER DEFAULT 0,
        notes     TEXT,
        created   TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        title     TEXT NOT NULL,
        priority  TEXT DEFAULT 'medium',
        category  TEXT DEFAULT 'writing',
        deadline  TEXT,
        done      INTEGER DEFAULT 0,
        created   TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS open_calls (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT NOT NULL,
        type        TEXT,
        description TEXT,
        deadline    TEXT,
        url         TEXT,
        source      TEXT DEFAULT 'ai',
        created     TEXT DEFAULT (datetime('now'))
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
    """)

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
             "Strong urban housing research group. Look into CCCP lab."),
            ("Harvard GSD", "USA", "2027-01-02", "researching", 72,
             "MDes or DDes track. Check Inaba's studio."),
            ("ETH Zürich", "Sveits", "2026-11-01", "researching", 85,
             "Chair of Architecture and Urban Design. Strong on European postwar housing."),
            ("KTH Stockholm", "Sverige", "2026-10-15", "shortlisted", 90,
             "Closest geographically. Arkitekturskolan has active housing research."),
            ("TU Delft", "Nederland", "2026-12-01", "researching", 80,
             "Heritage & Architecture dept. Good fit for adaptive reuse angle."),
        ]
        db.executemany(
            "INSERT INTO programs (name, country, deadline, status, fit_score, notes) VALUES (?,?,?,?,?,?)",
            programs)

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
    """Robustly extract JSON from Ollama responses that may contain markdown fences."""
    # Strip markdown code fences
    cleaned = re.sub(r'```(?:json)?\s*', '', raw)
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        if expect_list and isinstance(parsed, dict) and any(isinstance(v, list) for v in parsed.values()):
            for v in parsed.values():
                if isinstance(v, list):
                    return v
        return parsed
    except json.JSONDecodeError:
        pass

    # Try to find JSON array or object
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
    """Build a system prompt that includes full profile context."""
    db = sqlite3.connect(app.config["DATABASE"])
    db.row_factory = sqlite3.Row

    angle = db.execute("SELECT value FROM profile WHERE key='research_angle'").fetchone()
    voice = db.execute("SELECT value FROM profile WHERE key='voice_profile'").fetchone()
    programs = db.execute("SELECT name, country, status, fit_score FROM programs").fetchall()
    tasks = db.execute("SELECT title, priority, category, done FROM tasks ORDER BY done, priority").fetchall()

    angle_text = angle["value"] if angle else "Not set"
    voice_text = voice["value"] if voice and voice["value"] else "Not yet defined"

    prog_lines = "\n".join(f"- {p['name']} ({p['country']}) — status: {p['status']}, fit: {p['fit_score']}/100"
                           for p in programs)
    task_lines = "\n".join(f"- {'[x]' if t['done'] else '[ ]'} {t['title']} ({t['priority']}, {t['category']})"
                           for t in tasks)

    db.close()
    return f"""You are the AI assistant for a PhD Command Center. You know the user's full research profile.

Research angle: {angle_text}

Programs:
{prog_lines or 'None added yet.'}

Tasks:
{task_lines or 'None added yet.'}

Voice profile: {voice_text}

Always respond in English. Be precise and actionable."""


# ---------------------------------------------------------------------------
# Web scraping — real open calls from multiple sources
# ---------------------------------------------------------------------------

SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def _get(url, timeout=15):
    """HTTP GET with browser headers. Returns response or None."""
    try:
        r = requests.get(url, headers=SCRAPE_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r
        log.warning("HTTP %d for %s", r.status_code, url)
    except Exception as e:
        log.warning("Request failed for %s: %s", url, e)
    return None


# --- e-flux (architecture announcements, very relevant) ---

def scrape_eflux():
    """Scrape e-flux.com for architecture announcements and calls."""
    results = []
    for section in ["announcements", "architecture"]:
        r = _get(f"https://www.e-flux.com/{section}/")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # e-flux uses article tags or divs with class containing 'item'
        items = soup.select("article, .announcement-item, .content-item, [class*='Item'], [class*='card']")
        if not items:
            items = soup.find_all("a", href=re.compile(r"/(announcements|architecture)/\d+"))
        for item in items[:15]:
            a_tag = item.find("a", href=True) if item.name != "a" else item
            if not a_tag or not a_tag.get("href"):
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = title_el.get_text(strip=True)
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://www.e-flux.com{href}"
            desc_el = item.find("p")
            desc = desc_el.get_text(strip=True)[:200] if desc_el else ""
            date_el = item.find(["time", "span"], class_=re.compile(r"date|time", re.I))
            date_text = date_el.get_text(strip=True) if date_el else ""
            if title and len(title) > 3 and title.lower() != "e-flux":
                results.append({
                    "title": title[:150],
                    "type": "cfp",
                    "description": desc,
                    "deadline": date_text,
                    "url": href,
                    "source": "e-flux",
                })
    return results


# --- Archinect (competitions & opportunities) ---

def scrape_archinect():
    """Scrape Archinect for competitions and opportunities."""
    results = []
    for path in ["/competitions", "/jobs/categories/academic"]:
        r = _get(f"https://archinect.com{path}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select("article, .item, .listing-item, [class*='ListItem'], [class*='card']")
        if not items:
            items = soup.find_all("a", href=re.compile(r"/(competitions|jobs)/"))
        for item in items[:15]:
            a_tag = item.find("a", href=True) if item.name != "a" else item
            if not a_tag or not a_tag.get("href"):
                continue
            title_el = item.find(["h2", "h3", "h4"]) or a_tag
            title = title_el.get_text(strip=True)
            href = a_tag["href"]
            if not href.startswith("http"):
                href = f"https://archinect.com{href}"
            desc_el = item.find("p")
            desc = desc_el.get_text(strip=True)[:200] if desc_el else ""
            date_el = item.find(["time", "span"], class_=re.compile(r"date|deadline", re.I))
            date_text = date_el.get_text(strip=True) if date_el else ""
            if title and len(title) > 3:
                results.append({
                    "title": title[:150],
                    "type": "competition" if "compet" in path else "job",
                    "description": desc,
                    "deadline": date_text,
                    "url": href,
                    "source": "Archinect",
                })
    return results


# --- FindAPhD (PhD positions) ---

def scrape_findaphd():
    """Scrape FindAPhD for architecture/housing PhD positions."""
    results = []
    queries = [
        "architecture+housing", "urban+design+heritage",
        "adaptive+reuse+building", "postwar+housing",
    ]
    for q in queries:
        r = _get(f"https://www.findaphd.com/phds/?Keywords={q}")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        items = soup.select(".resultsRow, .phd-result, [class*='Result'], .card, article")
        for item in items[:10]:
            title_el = item.find(["h3", "h4", "a"])
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            link = ""
            a_tag = item.find("a", href=True)
            if a_tag:
                link = a_tag["href"]
                if not link.startswith("http"):
                    link = f"https://www.findaphd.com{link}"
            desc_el = item.find("p", class_=re.compile(r"desc|snippet|summary", re.I))
            if not desc_el:
                desc_el = item.find("p")
            desc = desc_el.get_text(strip=True)[:200] if desc_el else ""
            deadline_el = item.find(class_=re.compile(r"deadline|date|closes", re.I))
            deadline = deadline_el.get_text(strip=True) if deadline_el else ""
            if title and len(title) > 5:
                results.append({
                    "title": title[:150],
                    "type": "phd",
                    "description": desc,
                    "deadline": deadline,
                    "url": link,
                    "source": "FindAPhD",
                })
        if results:
            break  # Got results, no need to try more queries
    return results


# --- WikiCFP ---

def scrape_wikicfp():
    """Scrape WikiCFP for architecture/urban-related CFPs."""
    results = []
    for term in ["architecture", "urban+design", "heritage", "housing"]:
        r = _get(f"http://www.wikicfp.com/cfp/servlet/tool.search?q={term}&year=f")
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        # WikiCFP uses nested tables; CFP rows have colored backgrounds
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            link_el = cells[0].find("a")
            if not link_el:
                continue
            title = link_el.get_text(strip=True)
            if not title or len(title) < 3:
                continue
            href = link_el.get("href", "")
            if href and not href.startswith("http"):
                href = f"http://www.wikicfp.com{href}"
            # Description is often in the next row or second cell
            desc = cells[1].get_text(strip=True)[:200] if len(cells) > 1 else ""
            deadline = ""
            for cell in cells:
                text = cell.get_text(strip=True)
                if re.search(r"\d{4}-\d{2}-\d{2}|\w+ \d{1,2},? \d{4}", text):
                    deadline = text
                    break
            results.append({
                "title": title[:150],
                "type": "cfp",
                "description": desc,
                "deadline": deadline,
                "url": href,
                "source": "WikiCFP",
            })
        if len(results) >= 10:
            break
    return results


# --- EURAXESS (European research positions) ---

def scrape_euraxess():
    """Scrape EURAXESS for PhD/research positions in architecture."""
    results = []
    r = _get("https://euraxess.ec.europa.eu/jobs/search?keywords=architecture+housing+urban&research_profile=First+Stage+Researcher+%28R1%29")
    if not r:
        return results
    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select(".views-row, article, .node, [class*='job'], .card")
    for item in items[:15]:
        title_el = item.find(["h2", "h3", "h4", "a"])
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = ""
        a_tag = title_el if title_el.name == "a" else item.find("a", href=True)
        if a_tag and a_tag.get("href"):
            link = a_tag["href"]
            if not link.startswith("http"):
                link = f"https://euraxess.ec.europa.eu{link}"
        desc_el = item.find(class_=re.compile(r"field|body|summary|snippet", re.I))
        desc = desc_el.get_text(strip=True)[:200] if desc_el else ""
        deadline_el = item.find(class_=re.compile(r"deadline|date", re.I))
        deadline = deadline_el.get_text(strip=True) if deadline_el else ""
        if title and len(title) > 5:
            results.append({
                "title": title[:150],
                "type": "fellowship",
                "description": desc,
                "deadline": deadline,
                "url": link,
                "source": "EURAXESS",
            })
    return results


# --- jobs.ac.uk (UK academic jobs) ---

def scrape_jobs_ac_uk():
    """Scrape jobs.ac.uk for architecture PhD/research positions."""
    results = []
    r = _get("https://www.jobs.ac.uk/search/?keywords=architecture+housing+phd&activeFacet=hoursTypeFacet")
    if not r:
        return results
    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select(".job-result, .search-result, article, [class*='job'], .card")
    for item in items[:10]:
        title_el = item.find(["h3", "h4", "a"])
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = ""
        a_tag = item.find("a", href=True)
        if a_tag:
            link = a_tag["href"]
            if not link.startswith("http"):
                link = f"https://www.jobs.ac.uk{link}"
        desc_el = item.find("p")
        desc = desc_el.get_text(strip=True)[:200] if desc_el else ""
        deadline_el = item.find(class_=re.compile(r"deadline|closing|date", re.I))
        deadline = deadline_el.get_text(strip=True) if deadline_el else ""
        if title and len(title) > 5:
            results.append({
                "title": title[:150],
                "type": "phd",
                "description": desc,
                "deadline": deadline,
                "url": link,
                "source": "jobs.ac.uk",
            })
    return results


# --- Academic Positions ---

def scrape_academic_positions():
    """Scrape academicpositions.com for architecture research positions."""
    results = []
    r = _get("https://academicpositions.com/jobs?q=architecture+housing+urban")
    if not r:
        return results
    soup = BeautifulSoup(r.text, "html.parser")
    items = soup.select("article, .job-card, [class*='Card'], [class*='result'], .list-group-item")
    for item in items[:10]:
        title_el = item.find(["h2", "h3", "h4", "a"])
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        link = ""
        a_tag = item.find("a", href=True)
        if a_tag:
            link = a_tag["href"]
            if not link.startswith("http"):
                link = f"https://academicpositions.com{link}"
        desc_el = item.find("p")
        desc = desc_el.get_text(strip=True)[:200] if desc_el else ""
        if title and len(title) > 5:
            results.append({
                "title": title[:150],
                "type": "job",
                "description": desc,
                "deadline": "",
                "url": link,
                "source": "AcademicPositions",
            })
    return results


# --- Master scraper ---

def scrape_all_sources():
    """Scrape all sources in parallel-ish fashion, return combined results."""
    all_results = []
    scrapers = [
        ("e-flux", scrape_eflux),
        ("Archinect", scrape_archinect),
        ("FindAPhD", scrape_findaphd),
        ("WikiCFP", scrape_wikicfp),
        ("EURAXESS", scrape_euraxess),
        ("jobs.ac.uk", scrape_jobs_ac_uk),
        ("AcademicPositions", scrape_academic_positions),
    ]
    succeeded = 0
    for name, fn in scrapers:
        try:
            items = fn()
            all_results.extend(items)
            if items:
                succeeded += 1
                log.info("  %s: %d results", name, len(items))
            else:
                log.info("  %s: 0 results", name)
        except Exception as e:
            log.warning("  %s: failed — %s", name, e)
    log.info("Scraped %d total results from %d/%d sources",
             len(all_results), succeeded, len(scrapers))
    return all_results


def filter_by_relevance(calls, research_angle, max_results=15):
    """Use Ollama to filter and rank scraped calls by relevance to research angle."""
    if not calls:
        return []

    # Deduplicate by normalized title
    seen = set()
    unique = []
    for c in calls:
        key = re.sub(r'\s+', ' ', c["title"].lower().strip())
        if key not in seen and len(key) > 3:
            seen.add(key)
            unique.append(c)

    if not unique:
        return []

    # If few results, skip filtering
    if len(unique) <= 5:
        return unique

    # Build a numbered list for Ollama to evaluate
    numbered = "\n".join(
        f"{i+1}. [{c['source']}] {c['title']} — {c.get('description', '')[:80]}"
        for i, c in enumerate(unique[:40])
    )

    prompt = f"""Research angle: "{research_angle}"

The user is a PhD candidate in architecture, focusing on postwar housing, adaptive reuse, and urban transformation.

Below are items scraped from academic websites. Select ALL items relevant to:
- Architecture (broadly)
- Housing, urban design, urban studies
- Heritage, preservation, adaptive reuse
- Postwar built environment
- Architectural theory and history
- Related PhD positions, grants, or fellowships

Be generous — if it's even loosely related to architecture or urban studies, include it.

Return the numbers as JSON: {{"relevant": [1, 2, 5, 7]}}

Items:
{numbered}"""

    raw = ollama_generate(prompt, as_json=True)
    parsed = parse_json_response(raw)

    if parsed and isinstance(parsed, dict) and "relevant" in parsed:
        indices = parsed["relevant"]
        filtered = []
        for idx in indices:
            if isinstance(idx, int) and 1 <= idx <= len(unique):
                filtered.append(unique[idx - 1])
        if filtered:
            return filtered[:max_results]

    # Fallback: return all unique results
    return unique[:max_results]


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

def job_scan_open_calls():
    """Nightly 02:00 — scrape real open calls and filter by relevance."""
    log.info("Running scheduled open-calls scan")
    with app.app_context():
        db = get_db()
        angle = db.execute("SELECT value FROM profile WHERE key='research_angle'").fetchone()
        if not angle:
            return

        scraped = scrape_all_sources()
        if not scraped:
            log.warning("No results scraped from any source")
            return

        calls = filter_by_relevance(scraped, angle["value"])
        for c in calls:
            db.execute(
                "INSERT INTO open_calls (title, type, description, deadline, url, source) VALUES (?,?,?,?,?,?)",
                (c.get("title", ""), c.get("type", ""), c.get("description", ""),
                 c.get("deadline", ""), c.get("url", ""), c.get("source", "scraped")))
        db.commit()
        log.info("Added %d relevant open calls from scraping", len(calls))


def job_generate_drafts():
    """Monday 07:00 — generate 3 inbox drafts."""
    log.info("Running scheduled draft generation")
    with app.app_context():
        db = get_db()
        ctx = build_system_context()
        types = [
            ("substack", "Write a short Substack post (300-400 words) based on recent reflections about the research angle. Thoughtful, personal, academic but accessible."),
            ("linkedin", "Write a LinkedIn update (150-200 words) sharing a research insight or milestone. Professional but warm."),
            ("outreach", "Write an outreach email (200-300 words) to a potential PhD supervisor at one of the listed programs. Specific, shows knowledge of their work, proposes connection."),
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
    return render_template("dashboard.html",
                           angle=angle["value"] if angle else "",
                           programs=programs,
                           tasks=tasks,
                           total_tasks=total_tasks,
                           done_tasks=done_tasks,
                           upcoming_deadlines=upcoming_deadlines,
                           inbox_pending=inbox_pending)


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
    programs = query_db("SELECT * FROM programs ORDER BY deadline")
    return render_template("programs.html", programs=programs)


@app.route("/api/programs", methods=["POST"])
def create_program():
    d = request.json
    db = get_db()
    db.execute("INSERT INTO programs (name, country, deadline, status, fit_score, notes) VALUES (?,?,?,?,?,?)",
               (d["name"], d.get("country", ""), d.get("deadline", ""), d.get("status", "researching"),
                d.get("fit_score", 0), d.get("notes", "")))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/programs/<int:pid>", methods=["PUT"])
def update_program(pid):
    d = request.json
    db = get_db()
    db.execute("""UPDATE programs SET name=?, country=?, deadline=?, status=?, fit_score=?, notes=?
                  WHERE id=?""",
               (d["name"], d.get("country", ""), d.get("deadline", ""), d.get("status", "researching"),
                d.get("fit_score", 0), d.get("notes", ""), pid))
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
    db.execute("UPDATE tasks SET done = NOT done WHERE id=?", (tid,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
def delete_task(tid):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    db.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Routes — Open Calls
# ---------------------------------------------------------------------------

@app.route("/open-calls")
def open_calls_page():
    calls = query_db("SELECT * FROM open_calls ORDER BY created DESC")
    return render_template("open_calls.html", calls=calls)


@app.route("/api/open-calls/generate", methods=["POST"])
def generate_open_calls():
    db = get_db()
    angle = db.execute("SELECT value FROM profile WHERE key='research_angle'").fetchone()
    if not angle:
        return jsonify(error="No research angle set"), 400

    scraped = scrape_all_sources()
    if not scraped:
        return jsonify(error="Could not reach any source. Check your internet connection."), 500

    calls = filter_by_relevance(scraped, angle["value"])
    if not calls:
        # If filtering returns nothing, keep the top scraped results
        calls = scraped[:5]

    for c in calls:
        db.execute(
            "INSERT INTO open_calls (title, type, description, deadline, url, source) VALUES (?,?,?,?,?,?)",
            (c.get("title", ""), c.get("type", ""), c.get("description", ""),
             c.get("deadline", ""), c.get("url", ""), c.get("source", "scraped")))
    db.commit()
    return jsonify(ok=True, count=len(calls))


@app.route("/api/open-calls/<int:cid>", methods=["DELETE"])
def delete_open_call(cid):
    db = get_db()
    db.execute("DELETE FROM open_calls WHERE id=?", (cid,))
    db.commit()
    return jsonify(ok=True)


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
        "substack": "Write a short Substack post (300-400 words) based on reflections about the research angle. Thoughtful, personal, academic but accessible.",
        "linkedin": "Write a LinkedIn update (150-200 words) sharing a research insight or milestone. Professional but warm.",
        "outreach": "Write an outreach email (200-300 words) to a potential PhD supervisor at one of the listed programs. Specific, proposes connection.",
    }
    prompt = type_prompts.get(dtype, type_prompts["substack"]) + voice_instruction
    prompt += '\n\nReturn valid JSON: {"title": "...", "body": "..."}'

    raw = ollama_generate(prompt, system=ctx, as_json=True)
    data = parse_json_response(raw)
    if not data or not isinstance(data, dict):
        data = {"title": f"Draft {dtype}", "body": raw}

    db.execute("INSERT INTO inbox (type, title, body) VALUES (?,?,?)",
               (dtype, data.get("title", ""), data.get("body", "")))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/inbox/<int:iid>/<action>", methods=["POST"])
def inbox_action(iid, action):
    if action not in ("approve", "reject"):
        return jsonify(error="Invalid action"), 400
    db = get_db()
    status = "approved" if action == "approve" else "rejected"
    db.execute("UPDATE inbox SET status=? WHERE id=?", (status, iid))
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

    # Rebuild composite voice profile from all samples
    all_samples = query_db("SELECT analysis FROM voice_samples")
    if len(all_samples) > 1:
        analyses = "\n---\n".join(s["analysis"] for s in all_samples if s["analysis"])
        merge_prompt = f"""Here are voice analyses from multiple writing samples by the same person:

{analyses}

Synthesize these into ONE concise voice profile (4-6 sentences) that captures the writer's consistent style, tone, and patterns. This will be used to guide AI text generation to match this person's voice."""
        merged = ollama_generate(merge_prompt)
    else:
        merged = analysis

    db.execute("INSERT OR REPLACE INTO profile (key, value) VALUES ('voice_profile', ?)", (merged,))
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

scheduler = BackgroundScheduler()
scheduler.add_job(job_scan_open_calls, "cron", hour=2, minute=0, id="nightly_calls")
scheduler.add_job(job_generate_drafts, "cron", day_of_week="mon", hour=7, minute=0, id="weekly_drafts")
scheduler.start()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
