# Panel config

`apps.json` and `databases.json` in this folder are **gitignored**. You do not need to create them by hand.

- The panel writes **apps** when you deploy from GitHub or a folder, when you switch a discovered Laravel app to the panel template, and when it finds compose projects under `HOST_APPS_DIR`.
- Panel-managed Laravel apps use generated `docker-compose.panel.yml` (not the repo’s Docker files).
- It writes **databases** when a new GitHub MySQL app is created, and when it can read MySQL settings from an app `.env`.

Example files (`apps.example.json`, `databases.example.json`) show the shape only. Do not commit real hostnames, paths, or secrets.
