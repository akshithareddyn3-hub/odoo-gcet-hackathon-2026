import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from secrets import randbelow

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("STOCKSENSE_DB", ROOT / "stocksense.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("STOCKSENSE_SECRET") or os.urandom(32)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,login_id TEXT UNIQUE,name TEXT NOT NULL,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS warehouses(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,code TEXT UNIQUE NOT NULL,address TEXT DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY,warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),name TEXT NOT NULL,code TEXT NOT NULL,address TEXT DEFAULT '',active INTEGER NOT NULL DEFAULT 1,UNIQUE(warehouse_id,code));
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,name TEXT NOT NULL,sku TEXT UNIQUE NOT NULL,category_id INTEGER REFERENCES categories(id),unit TEXT NOT NULL,unit_cost REAL NOT NULL DEFAULT 0,reorder_level REAL NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY,name TEXT NOT NULL,type TEXT NOT NULL CHECK(type IN ('Vendor','Customer','Other')),email TEXT DEFAULT '',phone TEXT DEFAULT '',address TEXT DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),location_id INTEGER REFERENCES locations(id),quantity REAL NOT NULL DEFAULT 0,reserved_quantity REAL NOT NULL DEFAULT 0,UNIQUE(product_id,location_id));
CREATE TABLE IF NOT EXISTS operations(id INTEGER PRIMARY KEY,type TEXT NOT NULL,reference TEXT UNIQUE NOT NULL,status TEXT NOT NULL,source_warehouse_id INTEGER REFERENCES warehouses(id),destination_warehouse_id INTEGER REFERENCES warehouses(id),source_location_id INTEGER REFERENCES locations(id),destination_location_id INTEGER REFERENCES locations(id),contact_id INTEGER REFERENCES contacts(id),schedule_date TEXT,note TEXT DEFAULT '',delivery_address TEXT DEFAULT '',created_by INTEGER REFERENCES users(id),created_at TEXT NOT NULL,completed_at TEXT);
CREATE TABLE IF NOT EXISTS operation_items(id INTEGER PRIMARY KEY,operation_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,product_id INTEGER NOT NULL REFERENCES products(id),quantity REAL NOT NULL CHECK(quantity>0),unit_cost REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock_ledger(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),location_id INTEGER REFERENCES locations(id),operation_id INTEGER REFERENCES operations(id),movement_type TEXT NOT NULL,quantity_change REAL NOT NULL,balance_after REAL NOT NULL,note TEXT DEFAULT '',created_at TEXT NOT NULL,created_by INTEGER REFERENCES users(id));
CREATE INDEX IF NOT EXISTS idx_stock_product ON stock(product_id);
CREATE INDEX IF NOT EXISTS idx_ledger_created ON stock_ledger(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operations_type_status ON operations(type,status);
"""

def stamp():
    return datetime.now().isoformat(timespec="seconds")

def columns(con, table):
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}

def add_column(con, table, name, definition):
    if name not in columns(con, table):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

def default_location(con, warehouse_id):
    row = con.execute("SELECT id FROM locations WHERE warehouse_id=? AND active=1 ORDER BY id LIMIT 1", (warehouse_id,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute("INSERT INTO locations(warehouse_id,name,code) VALUES(?,?,?)", (warehouse_id, "Stock 1", "Stock1"))
    return cur.lastrowid

def next_reference(con, warehouse_id, operation):
    wh = con.execute("SELECT code FROM warehouses WHERE id=?", (warehouse_id,)).fetchone()
    if not wh:
        raise ValueError("Warehouse not found")
    prefix = {"RECEIPT": "IN", "DELIVERY": "OUT", "TRANSFER": "MOVE", "ADJUSTMENT": "ADJ"}[operation]
    base = f"{wh['code']}/{prefix}/"
    rows = con.execute("SELECT reference FROM operations WHERE reference LIKE ?", (base + "%",)).fetchall()
    seq = max([int(r["reference"].rsplit("/", 1)[-1]) for r in rows if r["reference"].rsplit("/", 1)[-1].isdigit()] or [0]) + 1
    return f"{base}{seq:04d}"

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")
    con.executescript(SCHEMA)
    # Upgrade the previous MVP schema in place; existing quantities are mapped to each warehouse's default location.
    add_column(con, "users", "login_id", "TEXT")
    add_column(con, "products", "unit_cost", "REAL NOT NULL DEFAULT 0")
    for name, definition in [
        ("source_location_id", "INTEGER REFERENCES locations(id)"),
        ("destination_location_id", "INTEGER REFERENCES locations(id)"),
        ("contact_id", "INTEGER REFERENCES contacts(id)"),
        ("schedule_date", "TEXT"),
        ("delivery_address", "TEXT DEFAULT ''"),
        ("completed_at", "TEXT"),
    ]:
        add_column(con, "operations", name, definition)
    add_column(con, "operation_items", "unit_cost", "REAL NOT NULL DEFAULT 0")
    add_column(con, "stock_ledger", "location_id", "INTEGER REFERENCES locations(id)")
    add_column(con, "stock", "reserved_quantity", "REAL NOT NULL DEFAULT 0")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_login_id ON users(login_id) WHERE login_id IS NOT NULL")
    # Existing stock has UNIQUE(product_id, warehouse_id); rebuild it to permit multiple bin locations.
    if "location_id" not in columns(con, "stock"):
        pass
    # The column was just added for old databases; inspect indexes to detect the old unique constraint.
    old_unique = False
    for ix in con.execute("PRAGMA index_list(stock)").fetchall():
        if ix["unique"] and [r["name"] for r in con.execute(f"PRAGMA index_info({ix['name']})")] == ["product_id", "warehouse_id"]:
            old_unique = True
    if old_unique:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("ALTER TABLE stock RENAME TO stock_old")
        con.execute("CREATE TABLE stock_new(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id),warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),location_id INTEGER REFERENCES locations(id),quantity REAL NOT NULL DEFAULT 0,reserved_quantity REAL NOT NULL DEFAULT 0,UNIQUE(product_id,location_id))")
        for row in con.execute("SELECT * FROM stock_old").fetchall():
            loc = default_location(con, row["warehouse_id"])
            con.execute("INSERT INTO stock_new(id,product_id,warehouse_id,location_id,quantity,reserved_quantity) VALUES(?,?,?,?,?,?)", (row["id"], row["product_id"], row["warehouse_id"], loc, row["quantity"], row["reserved_quantity"] or 0))
        con.execute("DROP TABLE stock_old")
        con.execute("ALTER TABLE stock_new RENAME TO stock")
    for wh in con.execute("SELECT id FROM warehouses").fetchall():
        default_location(con, wh["id"])
    con.execute("UPDATE stock SET location_id=(SELECT id FROM locations WHERE locations.warehouse_id=stock.warehouse_id ORDER BY id LIMIT 1) WHERE location_id IS NULL")
    con.execute("UPDATE stock_ledger SET location_id=(SELECT id FROM locations WHERE locations.warehouse_id=stock_ledger.warehouse_id ORDER BY id LIMIT 1) WHERE location_id IS NULL")
    con.execute("UPDATE operations SET source_location_id=(SELECT id FROM locations WHERE locations.warehouse_id=operations.source_warehouse_id ORDER BY id LIMIT 1) WHERE source_location_id IS NULL AND source_warehouse_id IS NOT NULL")
    con.execute("UPDATE operations SET destination_location_id=(SELECT id FROM locations WHERE locations.warehouse_id=operations.destination_warehouse_id ORDER BY id LIMIT 1) WHERE destination_location_id IS NULL AND destination_warehouse_id IS NOT NULL")
    con.execute("CREATE INDEX IF NOT EXISTS idx_stock_location ON stock(location_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_operations_schedule ON operations(schedule_date)")
    for row in con.execute("SELECT id,email FROM users WHERE login_id IS NULL OR login_id=''").fetchall():
        stem = re.sub(r"[^A-Za-z0-9]", "", row["email"].split("@")[0])[:8] or "user"
        candidate = (stem + "01")[:12]
        n = 1
        while con.execute("SELECT 1 FROM users WHERE login_id=? AND id<>?", (candidate, row["id"])).fetchone():
            suffix = f"{n:02d}"
            candidate = (stem[:12-len(suffix)] + suffix)
            n += 1
        con.execute("UPDATE users SET login_id=? WHERE id=?", (candidate, row["id"]))
    con.executemany("INSERT OR IGNORE INTO categories(name) VALUES(?)", [(x,) for x in ["Raw Materials","Finished Goods","Safety Equipment","Electrical","Machinery"]])
    con.executemany("INSERT OR IGNORE INTO warehouses(name,code,address) VALUES(?,?,?)", [("Main Warehouse","WH","Hyderabad"),("Production Floor","PROD","Building A"),("Dispatch Area","SHIP","Loading dock"),("Secondary Warehouse","WH2","East site")])
    warehouses = {r["name"]: r["id"] for r in con.execute("SELECT * FROM warehouses")}
    for whid in warehouses.values():
        default_location(con, whid)
    if not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        con.execute("INSERT INTO users(login_id,name,email,password_hash,created_at) VALUES(?,?,?,?,?)", ("demo01","Alex Morgan","demo@stocksense.local",generate_password_hash("StockSense1!"),stamp()))
    else:
        con.execute("UPDATE users SET login_id='demo01' WHERE email='demo@stocksense.local' AND login_id='demo01'") # preserve the demo login on upgraded installs
    if not con.execute("SELECT 1 FROM contacts LIMIT 1").fetchone():
        con.executemany("INSERT INTO contacts(name,type,email,phone,address) VALUES(?,?,?,?,?)", [
            ("Azure Interior","Vendor","orders@azure.example","+91 40 5550 0101","Hyderabad"),
            ("Northstar Supply","Vendor","sales@northstar.example","+91 40 5550 0102","Secunderabad"),
            ("Retail Customer","Customer","buyer@example.local","+91 40 5550 0110","Hyderabad"),
        ])
    if not con.execute("SELECT 1 FROM products LIMIT 1").fetchone():
        cats = {r["name"]:r["id"] for r in con.execute("SELECT * FROM categories")}
        ps = [("Steel Rods","RM-001","Raw Materials","kg",100,52.0),("Office Chairs","FG-014","Finished Goods","pcs",20,84.5),("Cement Bags","RM-023","Raw Materials","bags",80,6.8),("Safety Helmets","SA-008","Safety Equipment","pcs",25,12.0),("Copper Wire","EL-031","Electrical","m",150,1.6),("Welding Machines","MA-002","Machinery","pcs",5,950.0),("Steel Sheets","RM-011","Raw Materials","sheets",40,38.0)]
        for n,sku,cat,unit,reorder,cost in ps:
            con.execute("INSERT INTO products(name,sku,category_id,unit,unit_cost,reorder_level,created_at) VALUES(?,?,?,?,?,?,?)",(n,sku,cats[cat],unit,cost,reorder,stamp()))
        locs = {r["warehouse_id"]:r["id"] for r in con.execute("SELECT * FROM locations GROUP BY warehouse_id")}
        stock_levels = [[240,0,0,30],[6,8,0,0],[150,40,20,0],[0,0,0,0],[90,50,0,0],[3,0,0,0],[52,0,14,0]]
        for pi,values in enumerate(stock_levels,1):
            for whpos,qty in enumerate(values,1):
                whid = list(warehouses.values())[whpos-1]
                locid = locs[whid]
                con.execute("INSERT INTO stock(product_id,warehouse_id,location_id,quantity) VALUES(?,?,?,?)",(pi,whid,locid,qty))
                if qty:
                    con.execute("INSERT INTO stock_ledger(product_id,warehouse_id,location_id,movement_type,quantity_change,balance_after,note,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,1)",(pi,whid,locid,"OPENING",qty,qty,"Opening balance",stamp()))
    if (not con.execute("SELECT 1 FROM operations LIMIT 1").fetchone()
        and {"Main Warehouse","Production Floor"}.issubset(warehouses)
        and {"RM-001","FG-014"}.issubset({r["sku"] for r in con.execute("SELECT sku FROM products")})
        and con.execute("SELECT 1 FROM contacts WHERE type='Vendor'").fetchone()
        and con.execute("SELECT 1 FROM contacts WHERE type='Customer'").fetchone()):
        contact = con.execute("SELECT id FROM contacts WHERE type='Vendor' ORDER BY id LIMIT 1").fetchone()["id"]
        customer = con.execute("SELECT id FROM contacts WHERE type='Customer' ORDER BY id LIMIT 1").fetchone()["id"]
        product_ids = {r["sku"]:r["id"] for r in con.execute("SELECT id,sku FROM products")}
        main_wh = warehouses["Main Warehouse"]
        prod_wh = warehouses["Production Floor"]
        main_loc = default_location(con, main_wh)
        prod_loc = default_location(con, prod_wh)
        demo_ops = [
            ("RECEIPT","READY",main_wh,None,main_loc,None,contact,date.today()+timedelta(days=2),"Inbound steel delivery",[("RM-001",24)]),
            ("DELIVERY","WAITING",prod_wh,None,prod_loc,None,customer,date.today()-timedelta(days=1),"Customer order awaiting stock",[("FG-014",200)]),
            ("DELIVERY","READY",main_wh,None,main_loc,None,customer,date.today()+timedelta(days=1),"Scheduled dispatch",[("FG-014",2)]),
            ("RECEIPT","DRAFT",main_wh,None,main_loc,None,contact,date.today()-timedelta(days=2),"Draft cement receipt",[("RM-023",10)]),
        ]
        for typ,status,whsrc,whdst,locsrc,locdst,cid,sch,note,lines in demo_ops:
            ref = next_reference(con, whdst or whsrc, typ)
            oid = con.execute("INSERT INTO operations(type,reference,status,source_warehouse_id,destination_warehouse_id,source_location_id,destination_location_id,contact_id,schedule_date,note,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",(typ,ref,status,whsrc,whdst,locsrc,locdst,cid,sch.isoformat(),note,stamp())).lastrowid
            for sku,qty in lines:
                con.execute("INSERT INTO operation_items(operation_id,product_id,quantity,unit_cost) VALUES(?,?,?,?)",(oid,product_ids[sku],qty,0))
    # Dedicated, reproducible hackathon accounts and records. Marker notes make each
    # record discoverable on later starts without resetting completed demo workflows.
    demo_accounts = [
        ("demo_receiving", "Receiving Manager", "demo.receiving@stocksense.local", "ReceivingDemo1!"),
        ("demo_warehouse", "Warehouse Operations", "demo.warehouse@stocksense.local", "WarehouseDemo1!"),
    ]
    demo_user_ids = {}
    for login_id, name, email, password in demo_accounts:
        if not valid_password(password):
            raise ValueError(f"Invalid demo password configuration for {login_id}")
        matches = con.execute("SELECT * FROM users WHERE login_id=? OR email=?", (login_id, email)).fetchall()
        exact = next((u for u in matches if u["login_id"] == login_id and u["email"].lower() == email.lower()), None)
        if exact:
            demo_user_ids[login_id] = exact["id"]
        elif matches:
            raise ValueError(f"Demo account identity conflicts with existing user data: {login_id}")
        else:
            cur = con.execute("INSERT INTO users(login_id,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",
                              (login_id, name, email, generate_password_hash(password), stamp()))
            demo_user_ids[login_id] = cur.lastrowid

    if "Main Warehouse" in warehouses:
        main_wh = warehouses["Main Warehouse"]
        main_loc = default_location(con, main_wh)
        contacts_by_name = {r["name"]: r["id"] for r in con.execute("SELECT id,name FROM contacts WHERE active=1")}
        products_by_sku = {r["sku"]: dict(r) for r in con.execute("SELECT id,sku,unit_cost FROM products WHERE active=1")}

        def create_demo_operation(login_id, typ, status, note, contact_name, sku, quantity, *, receipt=False):
            uid = demo_user_ids[login_id]
            if con.execute("SELECT 1 FROM operations WHERE created_by=? AND note=? LIMIT 1", (uid, note)).fetchone():
                return
            if contact_name not in contacts_by_name or sku not in products_by_sku:
                raise ValueError(f"Required existing demo contact/product is missing for {login_id}")
            reference = next_reference(con, main_wh, typ)
            source_wh = None if receipt else main_wh
            destination_wh = main_wh if receipt else None
            source_loc = None if receipt else main_loc
            destination_loc = main_loc if receipt else None
            address = "Demo customer dock" if typ == "DELIVERY" else ""
            oid = con.execute("""INSERT INTO operations(type,reference,status,source_warehouse_id,destination_warehouse_id,
                source_location_id,destination_location_id,contact_id,schedule_date,note,delivery_address,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (typ, reference, status, source_wh, destination_wh, source_loc, destination_loc,
                 contacts_by_name[contact_name], date.today().isoformat(), note, address, uid, stamp())).lastrowid
            product = products_by_sku[sku]
            con.execute("INSERT INTO operation_items(operation_id,product_id,quantity,unit_cost) VALUES(?,?,?,?)",
                        (oid, product["id"], quantity, product["unit_cost"]))

        receiving_vendor = "Northstar Supply" if "Northstar Supply" in contacts_by_name else "Azure Interior"
        customer = "Retail Customer"
        helmet = products_by_sku.get("SA-008")
        helmet_stock = con.execute("SELECT quantity-reserved_quantity FROM stock WHERE product_id=? AND location_id=?",
                                   (helmet["id"], main_loc)).fetchone() if helmet else None
        initial_helmets = float(helmet_stock[0]) if helmet_stock else 0
        create_demo_operation("demo_receiving", "RECEIPT", "DRAFT",
                              "DEMO_ACCOUNT demo_receiving | Draft safety helmet replenishment",
                              receiving_vendor, "SA-008", 10, receipt=True)
        create_demo_operation("demo_receiving", "DELIVERY", "WAITING",
                              "DEMO_ACCOUNT demo_receiving | Waiting safety helmet order",
                              customer, "SA-008", max(8, int(initial_helmets + 3)))

        chair_stock = con.execute("SELECT quantity-reserved_quantity FROM stock WHERE product_id=? AND location_id=?",
                                  (products_by_sku["FG-014"]["id"], main_loc)).fetchone()
        if not chair_stock or float(chair_stock[0]) < 2:
            raise ValueError("Main Warehouse needs at least 2 Office Chairs for the warehouse demo delivery")
        create_demo_operation("demo_warehouse", "RECEIPT", "DRAFT",
                              "DEMO_ACCOUNT demo_warehouse | Draft steel rod receipt",
                              receiving_vendor, "RM-001", 12, receipt=True)
        create_demo_operation("demo_warehouse", "DELIVERY", "READY",
                              "DEMO_ACCOUNT demo_warehouse | Ready office chair dispatch",
                              customer, "FG-014", 2)
    con.commit()
    con.close()

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    con = g.pop("db", None)
    if con:
        con.close()

def require_login(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get("uid"):
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

def payload():
    return request.get_json(silent=True) or request.form

def fail(message, status=400):
    return jsonify(error=message), status

def valid_password(password):
    return len(password) >= 8 and re.search(r"[a-z]", password) and re.search(r"[A-Z]", password) and re.search(r"[^A-Za-z0-9]", password)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        d = payload()
        user = db().execute("SELECT * FROM users WHERE login_id=?", (str(d.get("login_id","")).strip(),)).fetchone()
        if user and check_password_hash(user["password_hash"], str(d.get("password",""))):
            session.clear()
            session["uid"], session["name"] = user["id"], user["name"]
            return redirect("/")
        return render_template("auth.html", mode="login", error="Invalid Login Id or Password")
    return render_template("auth.html", mode="login")

@app.route("/signup", methods=["GET","POST"])
def signup():
    if request.method == "POST":
        d = payload()
        login_id = str(d.get("login_id","")).strip()
        name = str(d.get("name",login_id)).strip()
        email = str(d.get("email","")).strip().lower()
        password = str(d.get("password",""))
        confirm = str(d.get("confirm_password",""))
        if not re.fullmatch(r"[A-Za-z0-9]{6,12}", login_id):
            return render_template("auth.html", mode="signup", error="Login ID must be 6–12 letters or numbers")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return render_template("auth.html", mode="signup", error="Enter a valid email address")
        if not valid_password(password):
            return render_template("auth.html", mode="signup", error="Password needs 8+ characters, lowercase, uppercase, and a special character")
        if password != confirm:
            return render_template("auth.html", mode="signup", error="Passwords do not match")
        try:
            cur = db().execute("INSERT INTO users(login_id,name,email,password_hash,created_at) VALUES(?,?,?,?,?)",(login_id,name,email,generate_password_hash(password),stamp()))
            db().commit()
        except sqlite3.IntegrityError:
            return render_template("auth.html", mode="signup", error="Login ID or email already exists")
        session["uid"], session["name"] = cur.lastrowid, name
        return redirect("/")
    return render_template("auth.html", mode="signup")

@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/reset", methods=["GET","POST"])
def reset():
    if request.method == "POST":
        d = payload()
        login_id = str(d.get("login_id","")).strip()
        if d.get("issue_otp"):
            user = db().execute("SELECT email FROM users WHERE login_id=?", (login_id,)).fetchone()
            if not user:
                return render_template("auth.html", mode="reset", error="No account found for that Login ID")
            otp = f"{randbelow(1000000):06d}"
            session.update(reset_login_id=login_id, reset_otp=otp, reset_until=(datetime.now()+timedelta(minutes=10)).timestamp())
            return render_template("auth.html", mode="reset", login_id=login_id, otp_notice=f"Development only — OTP: {otp} (expires in 10 minutes)")
        password = str(d.get("password",""))
        if session.get("reset_login_id") != login_id or session.get("reset_otp") != str(d.get("otp","")) or datetime.now().timestamp() > session.get("reset_until",0):
            return render_template("auth.html", mode="reset", login_id=login_id, error="OTP is invalid or expired")
        if not valid_password(password):
            return render_template("auth.html", mode="reset", login_id=login_id, error="Password does not meet the password requirements")
        if password != str(d.get("confirm_password","")):
            return render_template("auth.html", mode="reset", login_id=login_id, error="Passwords do not match")
        user = db().execute("SELECT email FROM users WHERE login_id=?", (login_id,)).fetchone()
        db().execute("UPDATE users SET password_hash=? WHERE login_id=?",(generate_password_hash(password),login_id))
        db().commit()
        session.pop("reset_otp",None)
        return render_template("auth.html", mode="login", message="Password updated. Please sign in.")
    return render_template("auth.html", mode="reset")

@app.get("/")
@require_login
def index():
    return render_template("index.html", user_name=session.get("name","User"))

@app.get("/api/me")
@require_login
def me():
    return jsonify(dict(db().execute("SELECT id,login_id,name,email,created_at FROM users WHERE id=?",(session["uid"],)).fetchone()))

@app.put("/api/me")
@require_login
def update_me():
    name = str(payload().get("name","")).strip()
    if len(name) < 2:
        return fail("Name must contain at least 2 characters")
    db().execute("UPDATE users SET name=? WHERE id=?",(name,session["uid"]))
    db().commit()
    session["name"] = name
    return jsonify(ok=True,name=name)

@app.get("/api/dashboard")
@require_login
def dashboard():
    con = db()
    totals = con.execute("SELECT p.id,p.name,p.sku,p.reorder_level,COALESCE(SUM(s.quantity),0) on_hand FROM products p LEFT JOIN stock s ON s.product_id=p.id WHERE p.active=1 GROUP BY p.id").fetchall()
    receipt_base = "type='RECEIPT' AND status NOT IN ('DONE','CANCELED')"
    delivery_base = "type='DELIVERY' AND status NOT IN ('DONE','CANCELED')"
    today = date.today().isoformat()
    receipts = con.execute(f"SELECT SUM(CASE WHEN schedule_date>? THEN 1 ELSE 0 END) operations,SUM(CASE WHEN schedule_date<? THEN 1 ELSE 0 END) late,COUNT(*) to_receive FROM operations WHERE {receipt_base}",(today,today)).fetchone()
    deliveries = con.execute(f"SELECT SUM(CASE WHEN schedule_date>? THEN 1 ELSE 0 END) operations,SUM(CASE WHEN schedule_date<? THEN 1 ELSE 0 END) late,SUM(CASE WHEN status='WAITING' THEN 1 ELSE 0 END) waiting,COUNT(*) to_deliver FROM operations WHERE {delivery_base}",(today,today)).fetchone()
    receipts = {k:(v or 0) for k,v in dict(receipts).items()}
    deliveries = {k:(v or 0) for k,v in dict(deliveries).items()}
    cats = [dict(r) for r in con.execute("SELECT c.name,COALESCE(SUM(s.quantity),0) quantity FROM categories c LEFT JOIN products p ON p.category_id=c.id LEFT JOIN stock s ON s.product_id=p.id GROUP BY c.id ORDER BY quantity DESC")]
    recent = [dict(r) for r in con.execute("SELECT l.*,p.name product_name,p.sku,w.name warehouse_name,loc.name location_name,o.reference,co.name contact_name FROM stock_ledger l JOIN products p ON p.id=l.product_id JOIN warehouses w ON w.id=l.warehouse_id LEFT JOIN locations loc ON loc.id=l.location_id LEFT JOIN operations o ON o.id=l.operation_id LEFT JOIN contacts co ON co.id=o.contact_id ORDER BY l.id DESC LIMIT 8")]
    return jsonify(kpis={
        "products":len(totals),"low_stock":sum(0<p["on_hand"]<=p["reorder_level"] for p in totals),
        "out_of_stock":sum(p["on_hand"]<=0 for p in totals),"units":sum(p["on_hand"] for p in totals),
        "receipts":dict(receipts),"deliveries":dict(deliveries),
    },categories=cats,recent=recent,low_products=[dict(p) for p in totals if p["on_hand"]<=p["reorder_level"]][:6])

@app.get("/api/categories")
@require_login
def categories():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM categories ORDER BY name")])

@app.post("/api/categories")
@require_login
def add_category():
    name = str(payload().get("name","")).strip()
    if len(name)<2:
        return fail("Category name is required")
    try:
        cur=db().execute("INSERT INTO categories(name) VALUES(?)",(name,)); db().commit()
        return jsonify(id=cur.lastrowid,name=name),201
    except sqlite3.IntegrityError:
        return fail("Category already exists",409)

@app.get("/api/warehouses")
@require_login
def warehouses():
    rows=db().execute("SELECT w.*,COUNT(DISTINCT l.id) location_count,COALESCE(SUM(s.quantity),0) units FROM warehouses w LEFT JOIN locations l ON l.warehouse_id=w.id AND l.active=1 LEFT JOIN stock s ON s.location_id=l.id WHERE w.active=1 GROUP BY w.id ORDER BY w.name").fetchall()
    return jsonify([dict(x) for x in rows])

@app.post("/api/warehouses")
@require_login
def add_warehouse():
    d=payload(); name=str(d.get("name","")).strip(); code=str(d.get("code","")).strip().upper(); address=str(d.get("address","")).strip()
    if len(name)<2 or not re.fullmatch(r"[A-Z0-9]{2,8}",code):
        return fail("Warehouse name and a 2–8 character short code are required")
    try:
        cur=db().execute("INSERT INTO warehouses(name,code,address) VALUES(?,?,?)",(name,code,address))
        db().execute("INSERT INTO locations(warehouse_id,name,code) VALUES(?,?,?)",(cur.lastrowid,"Stock 1","Stock1"))
        db().commit()
        return jsonify(id=cur.lastrowid),201
    except sqlite3.IntegrityError:
        return fail("Warehouse name or code already exists",409)

@app.put("/api/warehouses/<int:wid>")
@require_login
def edit_warehouse(wid):
    d=payload()
    code=str(d.get("code","")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,8}",code):
        return fail("Warehouse short code must be 2–8 letters or numbers")
    try:
        cur=db().execute("UPDATE warehouses SET name=?,code=?,address=? WHERE id=?",(str(d["name"]).strip(),code,str(d.get("address","")).strip(),wid))
        db().commit()
    except (KeyError,sqlite3.IntegrityError):
        return fail("Invalid or duplicate warehouse name/code",409)
    if not cur.rowcount:
        return fail("Warehouse not found",404)
    return jsonify(ok=True)

@app.delete("/api/warehouses/<int:wid>")
@require_login
def archive_warehouse(wid):
    if not db().execute("SELECT 1 FROM warehouses WHERE id=? AND active=1",(wid,)).fetchone():
        return fail("Warehouse not found",404)
    if db().execute("SELECT 1 FROM operations WHERE status NOT IN ('DONE','CANCELED') AND (source_warehouse_id=? OR destination_warehouse_id=?) LIMIT 1",(wid,wid)).fetchone():
        return fail("Complete or cancel open operations before archiving this warehouse",409)
    if db().execute("SELECT 1 FROM stock WHERE warehouse_id=? AND quantity<>0 LIMIT 1",(wid,)).fetchone():
        return fail("Move remaining stock before archiving this warehouse",409)
    db().execute("UPDATE warehouses SET active=0 WHERE id=?",(wid,)); db().execute("UPDATE locations SET active=0 WHERE warehouse_id=?",(wid,)); db().commit()
    return jsonify(ok=True)

@app.get("/api/locations")
@require_login
def locations():
    wh=request.args.get("warehouse_id")
    sql="SELECT l.*,w.name warehouse_name,w.code warehouse_code,COALESCE(SUM(s.quantity),0) units,COUNT(DISTINCT s.product_id) product_count FROM locations l JOIN warehouses w ON w.id=l.warehouse_id LEFT JOIN stock s ON s.location_id=l.id WHERE l.active=1 AND w.active=1"
    args=[]
    if wh:
        sql+=" AND l.warehouse_id=?"; args.append(wh)
    return jsonify([dict(r) for r in db().execute(sql+" GROUP BY l.id ORDER BY w.name,l.name",args)])

@app.post("/api/locations")
@require_login
def add_location():
    d=payload()
    try:
        wh=int(d["warehouse_id"]); name=str(d["name"]).strip(); code=str(d["code"]).strip()
    except (ValueError,TypeError,KeyError):
        return fail("Warehouse, location name, and code are required")
    if len(name)<2 or not re.fullmatch(r"[A-Za-z0-9_-]{2,12}",code):
        return fail("Location name and a 2–12 character short code are required")
    if not db().execute("SELECT id FROM warehouses WHERE id=? AND active=1",(wh,)).fetchone():
        return fail("Warehouse not found",404)
    try:
        cur=db().execute("INSERT INTO locations(warehouse_id,name,code,address) VALUES(?,?,?,?)",(wh,name,code,str(d.get("address","")).strip()))
        db().commit(); return jsonify(id=cur.lastrowid),201
    except sqlite3.IntegrityError:
        return fail("Warehouse is invalid or location code already exists",409)

@app.put("/api/locations/<int:lid>")
@require_login
def edit_location(lid):
    d=payload()
    try:
        wh=int(d["warehouse_id"]); name=str(d["name"]).strip(); code=str(d["code"]).strip()
    except (KeyError,ValueError):
        return fail("Invalid location or duplicate location code",409)
    if len(name)<2 or not re.fullmatch(r"[A-Za-z0-9_-]{2,12}",code):
        return fail("Location name and short code are required")
    con=db()
    try:
        con.execute("BEGIN IMMEDIATE")
        old=con.execute("SELECT warehouse_id FROM locations WHERE id=?",(lid,)).fetchone()
        if not old:
            con.rollback(); return fail("Location not found",404)
        if not con.execute("SELECT id FROM warehouses WHERE id=? AND active=1",(wh,)).fetchone():
            con.rollback(); return fail("Warehouse not found",404)
        if old["warehouse_id"]!=wh and con.execute("SELECT 1 FROM operations WHERE status NOT IN ('DONE','CANCELED') AND (source_location_id=? OR destination_location_id=?) LIMIT 1",(lid,lid)).fetchone():
            con.rollback(); return fail("Complete or cancel open operations before moving this location",409)
        if old["warehouse_id"]!=wh and con.execute("SELECT 1 FROM stock WHERE location_id=? AND quantity<>0 LIMIT 1",(lid,)).fetchone():
            con.rollback(); return fail("Move remaining stock before changing this location's warehouse",409)
        cur=con.execute("UPDATE locations SET warehouse_id=?,name=?,code=?,address=? WHERE id=?",(wh,name,code,str(d.get("address","")).strip(),lid))
        con.execute("UPDATE stock SET warehouse_id=? WHERE location_id=?",(wh,lid))
        con.commit()
    except sqlite3.IntegrityError:
        con.rollback(); return fail("Invalid warehouse or duplicate location code",409)
    if not cur.rowcount:
        return fail("Location not found",404)
    return jsonify(ok=True)

@app.delete("/api/locations/<int:lid>")
@require_login
def archive_location(lid):
    if not db().execute("SELECT id FROM locations WHERE id=? AND active=1",(lid,)).fetchone():
        return fail("Location not found",404)
    if db().execute("SELECT 1 FROM stock WHERE location_id=? AND quantity<>0 LIMIT 1",(lid,)).fetchone():
        return fail("Move remaining stock before archiving this location",409)
    if db().execute("SELECT 1 FROM operations WHERE status NOT IN ('DONE','CANCELED') AND (source_location_id=? OR destination_location_id=?) LIMIT 1",(lid,lid)).fetchone():
        return fail("Complete or cancel open operations before archiving this location",409)
    db().execute("UPDATE locations SET active=0 WHERE id=?",(lid,)); db().commit()
    return jsonify(ok=True)

@app.get("/api/contacts")
@require_login
def contacts():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM contacts WHERE active=1 ORDER BY name")])

@app.post("/api/contacts")
@require_login
def add_contact():
    d=payload(); name=str(d.get("name","")).strip(); kind=str(d.get("type","Other")).title()
    if len(name)<2 or kind not in ("Vendor","Customer","Other"):
        return fail("Contact name and a valid type are required")
    cur=db().execute("INSERT INTO contacts(name,type,email,phone,address) VALUES(?,?,?,?,?)",(name,kind,str(d.get("email","")).strip(),str(d.get("phone","")).strip(),str(d.get("address","")).strip()))
    db().commit()
    return jsonify(id=cur.lastrowid),201

@app.get("/api/products")
@require_login
def products():
    q=request.args.get("q",""); category=request.args.get("category","")
    sql="SELECT p.*,c.name category,COALESCE(SUM(s.quantity),0) total_stock FROM products p LEFT JOIN categories c ON c.id=p.category_id LEFT JOIN stock s ON s.product_id=p.id WHERE p.active=1 AND (p.name LIKE ? OR p.sku LIKE ?)"
    args=[f"%{q}%",f"%{q}%"]
    if category:
        sql+=" AND p.category_id=?"; args.append(category)
    result=[dict(r) for r in db().execute(sql+" GROUP BY p.id ORDER BY p.name",args)]
    return jsonify(result)

@app.post("/api/products")
@require_login
def add_product():
    d=payload()
    try:
        name=str(d["name"]).strip(); sku=str(d["sku"]).strip().upper(); cat=int(d["category_id"])
        unit=str(d.get("unit","pcs")).strip(); cost=float(d.get("unit_cost",0)); reorder=float(d.get("reorder_level",0))
    except (ValueError,TypeError,KeyError):
        return fail("Enter all required product fields")
    if len(name)<2 or not sku or not unit or cost<0 or reorder<0:
        return fail("Product fields must be valid and costs/levels non-negative")
    try:
        cur=db().execute("INSERT INTO products(name,sku,category_id,unit,unit_cost,reorder_level,created_at) VALUES(?,?,?,?,?,?,?)",(name,sku,cat,unit,cost,reorder,stamp()))
        db().commit(); return jsonify(id=cur.lastrowid),201
    except sqlite3.IntegrityError:
        return fail("SKU already exists or category is invalid",409)

@app.put("/api/products/<int:pid>")
@require_login
def edit_product(pid):
    d=payload()
    try:
        name=str(d["name"]).strip(); sku=str(d["sku"]).strip().upper(); cat=int(d["category_id"])
        unit=str(d["unit"]).strip(); cost=float(d.get("unit_cost",0)); reorder=float(d["reorder_level"])
    except (ValueError,TypeError,KeyError):
        return fail("Invalid product fields")
    if len(name)<2 or not sku or not unit or cost<0 or reorder<0:
        return fail("Product fields must be valid and costs/levels non-negative")
    try:
        cur=db().execute("UPDATE products SET name=?,sku=?,category_id=?,unit=?,unit_cost=?,reorder_level=? WHERE id=?",(name,sku,cat,unit,cost,reorder,pid))
        db().commit()
    except sqlite3.IntegrityError:
        return fail("SKU already exists or category is invalid",409)
    if not cur.rowcount:
        return fail("Product not found",404)
    return jsonify(ok=True)

@app.delete("/api/products/<int:pid>")
@require_login
def delete_product(pid):
    if not db().execute("SELECT id FROM products WHERE id=? AND active=1",(pid,)).fetchone():
        return fail("Product not found",404)
    if db().execute("SELECT 1 FROM operation_items i JOIN operations o ON o.id=i.operation_id WHERE i.product_id=? AND o.status NOT IN ('DONE','CANCELED') LIMIT 1",(pid,)).fetchone():
        return fail("Complete or cancel open operations before archiving this product",409)
    if db().execute("SELECT COALESCE(SUM(quantity),0) FROM stock WHERE product_id=?",(pid,)).fetchone()[0] != 0:
        return fail("Products with stock cannot be archived",409)
    db().execute("UPDATE products SET active=0 WHERE id=?",(pid,)); db().commit()
    return jsonify(ok=True)

@app.get("/api/operations")
@require_login
def operations():
    typ=request.args.get("type")
    q=request.args.get("q","")
    sql="""SELECT o.*,sw.name source_name,sw.code source_code,dw.name destination_name,dw.code destination_code,
        sl.name source_location_name,dl.name destination_location_name,co.name contact_name,co.type contact_type,
        (SELECT COUNT(*) FROM operation_items i WHERE i.operation_id=o.id) item_count
        FROM operations o LEFT JOIN warehouses sw ON sw.id=o.source_warehouse_id
        LEFT JOIN warehouses dw ON dw.id=o.destination_warehouse_id
        LEFT JOIN locations sl ON sl.id=o.source_location_id LEFT JOIN locations dl ON dl.id=o.destination_location_id
        LEFT JOIN contacts co ON co.id=o.contact_id WHERE (o.reference LIKE ? OR COALESCE(co.name,'') LIKE ?)"""
    args=[f"%{q}%",f"%{q}%"]
    if typ:
        sql+=" AND o.type=?"; args.append(typ.upper())
    return jsonify([dict(r) for r in db().execute(sql+" ORDER BY o.id DESC LIMIT 300",args)])

@app.get("/api/operations/<int:oid>")
@require_login
def operation_detail(oid):
    row=db().execute("""SELECT o.*,sw.name source_name,dw.name destination_name,sl.name source_location_name,
        dl.name destination_location_name,co.name contact_name,u.name responsible
        FROM operations o LEFT JOIN warehouses sw ON sw.id=o.source_warehouse_id LEFT JOIN warehouses dw ON dw.id=o.destination_warehouse_id
        LEFT JOIN locations sl ON sl.id=o.source_location_id LEFT JOIN locations dl ON dl.id=o.destination_location_id
        LEFT JOIN contacts co ON co.id=o.contact_id LEFT JOIN users u ON u.id=o.created_by WHERE o.id=?""",(oid,)).fetchone()
    if not row:
        return fail("Operation not found",404)
    result=dict(row)
    result["items"]=[dict(i) for i in db().execute("SELECT i.*,p.name product_name,p.sku,p.unit,p.unit_cost current_unit_cost FROM operation_items i JOIN products p ON p.id=i.product_id WHERE i.operation_id=?",(oid,))]
    return jsonify(result)

def stock_at(con, product_id, location_id):
    row=con.execute("SELECT quantity,reserved_quantity FROM stock WHERE product_id=? AND location_id=?",(product_id,location_id)).fetchone()
    return (row["quantity"],row["reserved_quantity"]) if row else (0,0)

def apply_stock(con, product_id, warehouse_id, location_id, delta, operation_id, movement_type, note, user_id):
    on_hand,reserved=stock_at(con,product_id,location_id)
    if on_hand+delta < 0:
        raise ValueError("Insufficient stock for one or more products.")
    after=on_hand+delta
    con.execute("""INSERT INTO stock(product_id,warehouse_id,location_id,quantity,reserved_quantity) VALUES(?,?,?,?,?)
        ON CONFLICT(product_id,location_id) DO UPDATE SET quantity=excluded.quantity""",(product_id,warehouse_id,location_id,after,reserved))
    con.execute("""INSERT INTO stock_ledger(product_id,warehouse_id,location_id,operation_id,movement_type,quantity_change,balance_after,note,created_at,created_by)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",(product_id,warehouse_id,location_id,operation_id,movement_type,delta,after,note,stamp(),user_id))
    return after

@app.post("/api/operations")
@require_login
def create_operation():
    d=payload(); typ=str(d.get("type","")).upper()
    if typ not in ("RECEIPT","DELIVERY","TRANSFER"):
        return fail("Choose RECEIPT, DELIVERY, or TRANSFER")
    try:
        source=int(d["source_location_id"]) if d.get("source_location_id") else None
        destination=int(d["destination_location_id"]) if d.get("destination_location_id") else None
        contact=int(d["contact_id"]) if d.get("contact_id") else None
        lines=[(int(x["product_id"]),float(x["quantity"])) for x in d.get("items",[])]
    except (TypeError,ValueError,KeyError):
        return fail("Choose locations, products, and valid quantities")
    if not lines or any(q<=0 for _,q in lines):
        return fail("Add at least one product with a positive quantity")
    if typ=="RECEIPT" and not destination:
        return fail("Choose a destination location")
    if typ=="DELIVERY" and not source:
        return fail("Choose a source location")
    if typ=="TRANSFER" and (not source or not destination or source==destination):
        return fail("Choose different source and destination locations")
    con=db()
    try:
        con.execute("BEGIN IMMEDIATE")
        locations={}
        for lid in {x for x in (source,destination) if x}:
            row=con.execute("SELECT l.id,l.warehouse_id FROM locations l JOIN warehouses w ON w.id=l.warehouse_id WHERE l.id=? AND l.active=1 AND w.active=1",(lid,)).fetchone()
            if not row:
                raise ValueError("Location not found")
            locations[lid]=row
        if contact and not con.execute("SELECT id FROM contacts WHERE id=? AND active=1",(contact,)).fetchone():
            raise ValueError("Contact not found")
        header_wh=locations.get(destination or source)
        reference=next_reference(con,header_wh["warehouse_id"],typ)
        status="DRAFT"
        if typ=="TRANSFER":
            status="DRAFT"
        oid=con.execute("""INSERT INTO operations(type,reference,status,source_warehouse_id,destination_warehouse_id,source_location_id,destination_location_id,contact_id,schedule_date,note,delivery_address,created_by,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(typ,reference,status,locations[source]["warehouse_id"] if source else None,locations[destination]["warehouse_id"] if destination else None,source,destination,contact,str(d.get("schedule_date") or date.today().isoformat()),str(d.get("note","")).strip(),str(d.get("delivery_address","")).strip(),session["uid"],stamp())).lastrowid
        for pid,quantity in lines:
            product=con.execute("SELECT id,unit_cost FROM products WHERE id=? AND active=1",(pid,)).fetchone()
            if not product:
                raise ValueError("Product not found")
            con.execute("INSERT INTO operation_items(operation_id,product_id,quantity,unit_cost) VALUES(?,?,?,?)",(oid,pid,quantity,product["unit_cost"]))
        con.commit()
        return jsonify(id=oid,reference=reference,status=status),201
    except ValueError as exc:
        con.rollback(); return fail(str(exc),400)
    except sqlite3.Error:
        con.rollback(); app.logger.exception("Could not create operation"); return fail("Could not create operation",500)

@app.post("/api/operations/<int:oid>/transition")
@require_login
def transition_operation(oid):
    action=str(payload().get("action","")).lower()
    con=db()
    try:
        con.execute("BEGIN IMMEDIATE")
        op=con.execute("SELECT * FROM operations WHERE id=?",(oid,)).fetchone()
        if not op:
            raise LookupError("Operation not found")
        status=op["status"]
        if action=="cancel":
            if status in ("DONE","CANCELED"):
                raise ValueError("Completed or canceled operations cannot be canceled")
            con.execute("UPDATE operations SET status='CANCELED' WHERE id=?",(oid,))
            con.commit(); return jsonify(status="CANCELED")
        if action=="ready":
            if status not in ("DRAFT","WAITING"):
                raise ValueError("Only Draft or Waiting operations can move to Ready")
            if op["type"] in ("DELIVERY","TRANSFER"):
                shortages=[]
                required={}
                for item in con.execute("SELECT product_id,quantity FROM operation_items WHERE operation_id=?",(oid,)):
                    required[item["product_id"]]=required.get(item["product_id"],0)+item["quantity"]
                for pid,quantity in required.items():
                    available=stock_at(con,pid,op["source_location_id"])[0]
                    if available < quantity:
                        shortages.append({"product_id":pid,"available":available,"required":quantity})
                if shortages:
                    con.execute("UPDATE operations SET status='WAITING' WHERE id=?",(oid,))
                    con.commit()
                    return jsonify(status="WAITING",waiting=True,error="Insufficient stock for one or more products.",shortages=shortages)
            con.execute("UPDATE operations SET status='READY' WHERE id=?",(oid,))
            con.commit(); return jsonify(status="READY")
        if action=="validate":
            if status!="READY":
                raise ValueError("Only Ready operations can be validated")
            items=con.execute("SELECT product_id,quantity FROM operation_items WHERE operation_id=?",(oid,)).fetchall()
            for item in items:
                pid,q=item["product_id"],item["quantity"]
                if op["type"]=="RECEIPT":
                    apply_stock(con,pid,op["destination_warehouse_id"],op["destination_location_id"],q,oid,"RECEIPT",op["note"],session["uid"])
                elif op["type"]=="DELIVERY":
                    apply_stock(con,pid,op["source_warehouse_id"],op["source_location_id"],-q,oid,"DELIVERY",op["note"],session["uid"])
                elif op["type"]=="TRANSFER":
                    apply_stock(con,pid,op["source_warehouse_id"],op["source_location_id"],-q,oid,"TRANSFER",op["note"],session["uid"])
                    apply_stock(con,pid,op["destination_warehouse_id"],op["destination_location_id"],q,oid,"TRANSFER",op["note"],session["uid"])
            con.execute("UPDATE operations SET status='DONE',completed_at=? WHERE id=?",(stamp(),oid))
            con.commit(); return jsonify(status="DONE")
        raise ValueError("Unsupported operation action")
    except LookupError as exc:
        con.rollback(); return fail(str(exc),404)
    except ValueError as exc:
        con.rollback()
        if "Insufficient stock" in str(exc):
            con.execute("UPDATE operations SET status='WAITING' WHERE id=? AND status='READY'",(oid,))
            con.commit()
            return jsonify(status="WAITING",error="Insufficient stock for one or more products.",waiting=True),200
        return fail(str(exc),409)
    except sqlite3.Error:
        con.rollback(); app.logger.exception("Could not transition operation"); return fail("Could not update operation",500)

@app.post("/api/adjustments")
@require_login
def create_adjustment():
    d=payload()
    try:
        pid=int(d["product_id"]); lid=int(d["location_id"]); counted=float(d["counted_quantity"])
    except (ValueError,TypeError,KeyError):
        return fail("Product, location, and counted quantity are required")
    if counted<0 or not str(d.get("reason","")).strip():
        return fail("Counted quantity must be non-negative and a reason is required")
    con=db()
    try:
        con.execute("BEGIN IMMEDIATE")
        loc=con.execute("SELECT id,warehouse_id FROM locations WHERE id=? AND active=1",(lid,)).fetchone()
        if not loc:
            raise ValueError("Location not found")
        product=con.execute("SELECT id FROM products WHERE id=? AND active=1",(pid,)).fetchone()
        if not product:
            raise ValueError("Product not found")
        current=stock_at(con,pid,lid)[0]; diff=counted-current
        if diff==0:
            raise ValueError("Counted quantity matches the recorded stock; no adjustment is needed")
        ref=next_reference(con,loc["warehouse_id"],"ADJUSTMENT")
        oid=con.execute("INSERT INTO operations(type,reference,status,source_warehouse_id,source_location_id,note,created_by,created_at,completed_at) VALUES('ADJUSTMENT',?,'DONE',?,?,?,?,?,?)",(ref,loc["warehouse_id"],lid,str(d["reason"]).strip(),session["uid"],stamp(),stamp())).lastrowid
        con.execute("INSERT INTO operation_items(operation_id,product_id,quantity) VALUES(?,?,?)",(oid,pid,abs(diff)))
        after=current+diff
        con.execute("INSERT INTO stock(product_id,warehouse_id,location_id,quantity) VALUES(?,?,?,?) ON CONFLICT(product_id,location_id) DO UPDATE SET quantity=excluded.quantity",(pid,loc["warehouse_id"],lid,after))
        con.execute("INSERT INTO stock_ledger(product_id,warehouse_id,location_id,operation_id,movement_type,quantity_change,balance_after,note,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",(pid,loc["warehouse_id"],lid,oid,"ADJUSTMENT",diff,after,str(d["reason"]).strip(),stamp(),session["uid"]))
        con.commit(); return jsonify(id=oid,reference=ref,difference=diff,status="DONE"),201
    except ValueError as exc:
        con.rollback(); return fail(str(exc),400)
    except sqlite3.Error:
        con.rollback(); app.logger.exception("Adjustment failed"); return fail("Could not complete adjustment",500)

@app.get("/api/stock")
@require_login
def stock_list():
    q=request.args.get("q",""); category=request.args.get("category",""); location=request.args.get("location",""); status=request.args.get("status","")
    sql="""SELECT p.id product_id,p.name product_name,p.sku,p.unit,p.unit_cost,p.reorder_level,c.name category,
        w.id warehouse_id,w.name warehouse_name,loc.id location_id,loc.name location_name,
        COALESCE(s.quantity,0) on_hand,COALESCE(s.reserved_quantity,0) reserved,
        COALESCE(s.quantity,0)-COALESCE(s.reserved_quantity,0) free_to_use
        FROM products p JOIN categories c ON c.id=p.category_id
        CROSS JOIN locations loc JOIN warehouses w ON w.id=loc.warehouse_id AND w.active=1 AND loc.active=1
        LEFT JOIN stock s ON s.product_id=p.id AND s.location_id=loc.id WHERE p.active=1 AND (p.name LIKE ? OR p.sku LIKE ?)"""
    args=[f"%{q}%",f"%{q}%"]
    if category:
        sql+=" AND p.category_id=?"; args.append(category)
    if location:
        sql+=" AND loc.id=?"; args.append(location)
    if status=="low":
        sql+=" AND COALESCE(s.quantity,0)>0 AND COALESCE(s.quantity,0)<=p.reorder_level"
    elif status=="out":
        sql+=" AND COALESCE(s.quantity,0)<=0"
    elif status=="available":
        sql+=" AND COALESCE(s.quantity,0)>p.reorder_level"
    rows=[dict(r) for r in db().execute(sql+" ORDER BY p.name,w.name,loc.name",args)]
    return jsonify(rows)

@app.get("/api/ledger")
@require_login
def ledger():
    q=request.args.get("q",""); warehouse=request.args.get("warehouse","")
    sql="""SELECT l.*,p.name product_name,p.sku,w.name warehouse_name,w.code warehouse_code,loc.name location_name,
        o.reference,o.type operation_type,o.status,o.schedule_date,co.name contact_name,u.name responsible,
        CASE WHEN o.type='RECEIPT' THEN COALESCE(co.name,'Vendor')||' → '||w.name||' / '||COALESCE(loc.name,'')
             WHEN o.type='DELIVERY' THEN w.name||' / '||COALESCE(loc.name,'')||' → '||COALESCE(o.delivery_address,co.name,'Customer')
             WHEN o.type='TRANSFER' THEN COALESCE((SELECT name FROM warehouses WHERE id=o.source_warehouse_id),'')||' / '||COALESCE((SELECT name FROM locations WHERE id=o.source_location_id),'')||' → '||COALESCE((SELECT name FROM warehouses WHERE id=o.destination_warehouse_id),'')||' / '||COALESCE((SELECT name FROM locations WHERE id=o.destination_location_id),'')
             ELSE w.name||' / '||COALESCE(loc.name,'') END route
        FROM stock_ledger l JOIN products p ON p.id=l.product_id JOIN warehouses w ON w.id=l.warehouse_id
        LEFT JOIN locations loc ON loc.id=l.location_id LEFT JOIN operations o ON o.id=l.operation_id
        LEFT JOIN contacts co ON co.id=o.contact_id LEFT JOIN users u ON u.id=l.created_by
        WHERE (COALESCE(o.reference,'') LIKE ? OR COALESCE(co.name,'') LIKE ? OR p.name LIKE ? OR p.sku LIKE ?)"""
    args=[f"%{q}%"]*4
    if warehouse:
        sql+=" AND l.warehouse_id=?"; args.append(warehouse)
    return jsonify([dict(r) for r in db().execute(sql+" ORDER BY l.id DESC LIMIT 500",args)])

if __name__=="__main__":
    init_db()
    app.run(host="127.0.0.1",port=int(os.environ.get("PORT",5000)),debug=os.environ.get("FLASK_DEBUG")=="1")
