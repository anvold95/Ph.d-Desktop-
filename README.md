# PhD Command Center

Local Flask-webapp for PhD application management with AI-powered features via Ollama.

## Features

- **Dashboard** — Research angle, task progress, deadlines, quick actions
- **Programs** — CRUD for PhD programs with fit scores and status tracking
- **Tasks** — Priority-based tasks with categories (writing/networking/visibility)
- **Open Calls** — AI-generated CFPs, conferences, grants, and fellowships
- **Inbox** — AI-drafted Substack posts, LinkedIn updates, and outreach emails
- **Voice Training** — Submit writing samples to build a voice profile used in all AI generation
- **AI Chat** — Context-aware chat that knows your research angle, programs, tasks, and voice

## Prerequisites

### Install Ollama

```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh

# Windows — download from https://ollama.com/download
```

### Pull the model

```bash
ollama pull llama3.2
```

Make sure Ollama is running before starting the app:

```bash
ollama serve
```

## Setup & Run

```bash
# Clone the repo
git clone <repo-url> && cd Ph.d-Desktop-

# (Optional) Create a virtual environment
python -m venv venv && source venv/bin/activate

# Install and run
pip install -r requirements.txt && python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## Configuration

Copy `.env.example` to `.env` to customize:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `llama3.2` | Model to use |

## Scheduled Jobs

| Schedule | Job |
|---|---|
| Every night 02:00 | Scan for relevant open calls |
| Every Monday 07:00 | Generate 3 inbox drafts (Substack, LinkedIn, outreach) |

Logs are written to `scheduler.log`.

## Stack

- Flask + SQLite (no external database)
- Ollama (local AI, no API keys needed)
- Vanilla JS + Tailwind CDN (no build step)
- APScheduler for background agents
