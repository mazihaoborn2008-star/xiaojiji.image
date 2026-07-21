# Uma Web MVP

Anime image generation web platform with Smart Agent, character-aware prompt engineering, and ComfyUI integration.

## Tech Stack

- **Backend:** Python / FastAPI / Uvicorn
- **Frontend:** Vanilla HTML/CSS/JS (SPA)
- **Database:** SQLite (WAL mode)
- **Cache:** Redis (optional)
- **AI Agent:** Ollama (local LLM) + DeepSeek API (optional)
- **Image Generation:** ComfyUI

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and configure environment
cp .env.example .env
# Edit .env with your actual settings

# 3. Run
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Project Structure

```
app/
├── main.py              # FastAPI routes & app entry
├── db.py                # SQLite database layer
├── auth.py              # Authentication (Discord OAuth, email OTP, password)
├── agent.py             # Prompt translation agent (Ollama)
├── config.py            # Settings from .env
├── catalog.py           # Style/workflow catalog
├── schemas.py           # Pydantic models
├── content_policy.py    # Content filter
├── recharge_service.py  # Top-up / payment
├── redis_client.py      # Redis connection
├── data/                # Character tags, prompt library, LoRA registry
├── services/            # Email, image review
├── smart_agent/         # Smart Agent (DeepSeek-based planning)
└── static/              # Frontend SPA (HTML/CSS/JS)
scripts/                 # Utility scripts
tools/                   # Migration tools
```

## Environment Variables

See `.env.example` for all available configuration options.

Key settings:
- `OWNER_USER_ID` - Discord user ID for admin access
- `DEEPSEEK_API_KEY` - DeepSeek API key for Smart Agent (optional)
- `COMFYUI_WORKFLOW_DIR` - Path to ComfyUI workflow JSON files

## License

Private project.
