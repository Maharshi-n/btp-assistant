# BTP Personal AI Assistant

A self-hosted personal AI assistant running on a single laptop, accessible from a phone or desktop browser over LAN. Built as Maharshi's B.Tech Project, it combines a FastAPI web server with a LangGraph multi-agent orchestration system that gives the assistant real access to the filesystem, terminal, Gmail, Google Drive, and Google Calendar — with a safety-first permission model that routes sensitive actions through phone approval.

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash / PowerShell: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env and set SECRET_KEY at minimum

# 4. Run the server
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/` on the laptop or `http://<laptop-lan-ip>:8000/` from your phone.
