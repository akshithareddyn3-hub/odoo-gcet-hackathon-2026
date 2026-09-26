# StockSense — Inventory Management System

Flask + SQLite inventory MVP with location-level stock, operation lifecycles, and an auditable stock ledger.

## Run locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. First launch creates the SQLite database and demo records. Set `STOCKSENSE_SECRET` to a persistent random secret and optionally `STOCKSENSE_DB` to select another database path.

Fresh demo login: **demo01** / **StockSense1!**. On an upgraded database, the original demo password remains **stock123**.

The password reset uses a development-only OTP displayed in the browser. No external mail/SMS service is used.

## Existing architecture

- `app.py`: Flask HTML/API routes, SQLite schema migration and seed, operation validation, and transactional inventory updates.
- `templates/index.html`, `templates/auth.html`: authenticated app shell and authentication pages.
- `static/app.js`: API-backed dashboard, tables, detail views, list/Kanban switchers, and forms.
- `static/style.css`: shared visual system and responsive layout.
- Flask is the only Python dependency. Bootstrap, Chart.js, and fonts load from CDNs.

The migration upgrades the previous warehouse-only stock rows by assigning each to a default location, preserving existing quantities and ledger entries.

## Main API routes

Authentication uses session cookies. All `/api` routes require sign-in.

- `POST /login`, `POST /signup`, `POST /logout`, `POST /reset`
- `GET /api/dashboard` — live inventory KPIs, schedule/late/waiting counts, category stock, and recent movements
- `GET|POST /api/products`, `PUT|DELETE /api/products/<id>`
- `GET|POST /api/warehouses`, `PUT|DELETE /api/warehouses/<id>`
- `GET|POST /api/locations`, `PUT|DELETE /api/locations/<id>`
- `GET|POST /api/contacts`
- `GET|POST /api/operations`, `GET /api/operations/<id>`
- `POST /api/operations/<id>/transition` — `ready`, `validate`, or `cancel`
- `POST /api/adjustments` — confirm physical counted quantity
- `GET /api/stock` — search/category/location/status filters; includes on-hand, reserved, and free-to-use
- `GET /api/ledger` — ledger-derived movement history

## Data model

SQLite tables: users, categories, warehouses, locations, contacts, products, stock, operations, operation_items, and stock_ledger. Stock is unique per product/location and includes a reserved quantity for future reservation support. Operation items keep quantity and a cost snapshot. Foreign keys and indexes support operational joins and ledger chronology.

New references use `<warehouse-code>/<IN|OUT|MOVE|ADJ>/<sequential number>`. Receipt and delivery creation records Draft operations without moving stock. Moving an operation to Ready also does not change stock; Validate changes stock and writes ledger entries in one transaction. Delivery shortage checks move the operation to Waiting, and retry is safe. Adjustments set a counted quantity and record the signed difference.

