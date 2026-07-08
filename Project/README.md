# Dating App Backend Project

This folder is the scheme-A backend layout. It groups the current Project backend
with the independent backend-main modules while preserving the service boundaries
that are already working locally.

## Layout

```text
Project/
├── main.py
├── requirements.txt
├── .env
├── social_demotest/
├── matchmaker_agent/
├── risk_backend/
├── NEW_AI_GEN/
└── docker/
```

## First-time setup

Create a new virtual environment from this folder. Do not copy an old venv from
another path because Windows venv files often contain absolute Python paths.

```powershell
cd C:\Users\daisy\backend-main\Project
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

If `python` is not available in PATH, use the Python executable you normally use
on this machine and run the same `-m venv venv` command.

## Services

Run these services in separate PowerShell windows after the venv is ready:

```powershell
cd C:\Users\daisy\backend-main\Project\risk_backend
..\venv\Scripts\python.exe main.py
```

```powershell
cd C:\Users\daisy\backend-main\Project\matchmaker_agent
..\venv\Scripts\python.exe agent_api.py
```

```powershell
cd C:\Users\daisy\backend-main\Project
.\venv\Scripts\python.exe main.py
```

The expected ports are:

```text
8000 Project/main.py -> social_demotest + NEW_AI_GEN
8001 risk_backend
9001 matchmaker_agent
```

## Notes

- `main_app` from backend-main is intentionally not copied here because the
  newer `social_demotest` version is the source of truth.
- `matchmaker_agent` remains a separate 9001 service in this scheme.
- `risk_backend` remains a separate 8001 service in this scheme.
- Virtual environments are local machine artifacts and should be recreated
  instead of committed.
