# ACADES CBV Dashboard

The dashboard is a Python/FastAPI application with server-rendered pages,
login sessions, Google Sheets access, and a local SQLite store. It is not a
static website, so GitHub Pages cannot host the working dashboard.

## Run locally

Install the packages in `requirements.txt`, then start the app:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. The application uses the existing local
`data/` and `bundles/` directories by default.

## Deploy with Docker

Build the image and run it with a persistent data volume:

```sh
docker build -t acades-cbv-dashboard .
docker run --rm -p 8000:8000 \
  -e CBV_USER='your-dashboard-username' \
  -e CBV_PASS='your-strong-password' \
  -e CBV_SESSION_SECRET='a-long-random-secret' \
  -v cbv-dashboard-data:/var/data \
  acades-cbv-dashboard
```

Then open <http://localhost:8000>. On a hosting platform, deploy this
repository as a Docker service, set `CBV_USER`, `CBV_PASS`, and
`CBV_SESSION_SECRET` as secrets, and attach persistent storage at `/var/data`.
The container listens on `PORT` when the platform provides it, or port `8000`
otherwise. The container exits with an error if any of those three required
credentials are missing; it does not use the app's local-development defaults.

Keep the persistent volume mounted at `/var/data`: it stores the dashboard
SQLite database, cached sheet data, generated session key (if no secret is
configured), and uploaded bundle spreadsheets. Without persistent storage,
these files can be lost when the service is replaced or restarted. Uploaded
bundle spreadsheets are intentionally excluded from the Docker image and
should be uploaded after deployment.

The configured Google Sheet must be readable by the deployed app. Optional
settings include `CBV_SHEET_GID`, `CBV_CACHE_TTL`, and
`CBV_LOW_ENCOUNTERS`; see `app/config.py` for their defaults.
