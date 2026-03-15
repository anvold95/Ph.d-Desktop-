import os
import json
import sqlite3
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, g, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

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

def ollama_generate(prompt, system=None):
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=120)
        r.raise_for_status()
        return r.json().get("response", "")
    except Exception as e:
        log.error("Ollama error: %s", e)
        return f"[Ollama unavailable: {e}]"


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

Respond in the same language the user writes in. Be precise and actionable. If the user writes in Norwegian, respond in Norwegian."""


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------

def job_scan_open_calls():
    """Nightly 02:00 — generate relevant open calls."""
    log.info("Running scheduled open-calls scan")
    with app.app_context():
        db = get_db()
        angle = db.execute("SELECT value FROM profile WHERE key='research_angle'").fetchone()
        if not angle:
            return
        prompt = f"""Based on this research angle:
\"{angle['value']}\"

Generate 3 relevant current opportunities (conferences, calls for papers, grants, or fellowships) for a PhD candidate in architecture/urban studies. For each, provide:
- title
- type (conference / cfp / grant / fellowship)
- short description (1-2 sentences)
- approximate deadline (YYYY-MM-DD)
- a plausible URL or "N/A"

Return ONLY valid JSON: [{{"title":"...","type":"...","description":"...","deadline":"...","url":"..."}}]"""

        raw = ollama_generate(prompt)
        try:
            start = raw.index("[")
            end = raw.rindex("]") + 1
            calls = json.loads(raw[start:end])
        except (ValueError, json.JSONDecodeError):
            log.warning("Could not parse open-calls JSON from Ollama")
            return

        for c in calls:
            db.execute("INSERT INTO open_calls (title, type, description, deadline, url) VALUES (?,?,?,?,?)",
                       (c.get("title", ""), c.get("type", ""), c.get("description", ""),
                        c.get("deadline", ""), c.get("url", "")))
        db.commit()
        log.info("Added %d open calls", len(calls))


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
            prompt = instruction + voice_instruction + "\n\nReturn the result as JSON: {\"title\": \"...\", \"body\": \"...\"}"
            raw = ollama_generate(prompt, system=ctx)
            try:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                data = json.loads(raw[start:end])
            except (ValueError, json.JSONDecodeError):
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

    prompt = f"""Based on this research angle:
\"{angle['value']}\"

Generate 5 relevant current opportunities (conferences, calls for papers, grants, or fellowships) for a PhD candidate in architecture/urban studies. For each, provide:
- title
- type (conference / cfp / grant / fellowship)
- short description (1-2 sentences)
- approximate deadline (YYYY-MM-DD)
- a plausible URL or "N/A"

Return ONLY valid JSON array: [{{"title":"...","type":"...","description":"...","deadline":"...","url":"..."}}]"""

    raw = ollama_generate(prompt)
    try:
        start = raw.index("[")
        end = raw.rindex("]") + 1
        calls = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return jsonify(error="Could not parse AI response", raw=raw), 500

    for c in calls:
        db.execute("INSERT INTO open_calls (title, type, description, deadline, url) VALUES (?,?,?,?,?)",
                   (c.get("title", ""), c.get("type", ""), c.get("description", ""),
                    c.get("deadline", ""), c.get("url", "")))
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
    prompt += '\n\nReturn the result as JSON: {"title": "...", "body": "..."}'

    raw = ollama_generate(prompt, system=ctx)
    try:
        start = raw.index("{")
        end = raw.rindex("}") + 1
        data = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
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
