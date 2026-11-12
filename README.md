# Gaming Smart — admin dashboard shell

Flask + SQLite. Photo/document capture (phone camera or file upload), clients, machines, win/loss entries, dashboard, audit trail.

## Run locally
    pip install -r requirements.txt
    ADMIN_PASSWORD=yourpassword SECRET_KEY=random python app.py

## Environment variables
- ADMIN_USER, ADMIN_PASSWORD — login
- SECRET_KEY — session signing
- DATA_DIR — where the SQLite DB and uploaded photos live

## Production
    gunicorn app:app
