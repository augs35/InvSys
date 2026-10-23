import os
import sqlite3
import qrcode
from flask import Flask, render_template, request, redirect, url_for, send_file, flash, g, jsonify
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
app.secret_key = "change-this-secret-key"

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
if not BASE_URL:
    if os.path.isdir("/data"):
        # On Render, BASE_URL MUST be set — otherwise QRs point to a LAN IP
        print("[warn] BASE_URL not set on Render. QRs will be broken. Set the env var!")
        BASE_URL = "https://invsys-hlx3.onrender.com"  # placeholder, must be replaced
    else:
        BASE_URL = "https://invsys-hlx3.onrender.com"

# On Render, a persistent disk is mounted at /data.
# Locally, these fall back to the project folder.
if os.path.isdir("/data"):
    DB_PATH   = "/data/database.db"
    QR_FOLDER = "/data/qrcodes"
else:
    DB_PATH   = "database.db"
    QR_FOLDER = os.path.join("static", "qrcodes")

os.makedirs(QR_FOLDER, exist_ok=True)


# ============================================================
# HELPERS
# ============================================================
# Philippine Standard Time = UTC+8 (no daylight savings, so a fixed offset is fine)
MANILA_TZ = timezone(timedelta(hours=8))


def pretty_time(sqlite_ts):
    """Convert a stored UTC 'YYYY-MM-DD HH:MM:SS' string into 'October 18, 2026 3:45PM' (Manila time)."""
    if not sqlite_ts:
        return ""
    try:
        # Parse as UTC
        dt_utc = datetime.strptime(sqlite_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        # Legacy values that might not have seconds — try a couple more patterns
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt_utc = datetime.strptime(sqlite_ts, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return sqlite_ts

    dt = dt_utc.astimezone(MANILA_TZ)
    month_name = dt.strftime("%B")
    day = dt.day
    year = dt.year
    hour12 = dt.hour % 12 or 12
    minute = f"{dt.minute:02d}"
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{month_name} {day}, {year} {hour12}:{minute}{ampm}"
    """Convert '2026-11-01 22:33:00' -> 'November 1, 2026 10:33PM'."""
    if not sqlite_ts:
        return ""
    try:
        dt = datetime.strptime(sqlite_ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return sqlite_ts

    month_name = dt.strftime("%B")
    day = dt.day
    year = dt.year
    hour12 = dt.hour % 12 or 12
    minute = f"{dt.minute:02d}"
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{month_name} {day}, {year} {hour12}:{minute}{ampm}"


app.jinja_env.filters["pretty_time"] = pretty_time


# ============================================================
# DATABASE
# ============================================================
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            quantity INTEGER DEFAULT 1,
            location TEXT,
            status TEXT DEFAULT 'available',
            folder_id INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            borrower TEXT,
            action TEXT NOT NULL,
            qty INTEGER DEFAULT 1,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Migrations
    item_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "status" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'available'")
        print("[DB] Added column items.status")
    if "folder_id" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN folder_id INTEGER")
        print("[DB] Added column items.folder_id")

    log_cols = [r[1] for r in conn.execute("PRAGMA table_info(logs)").fetchall()]
    if "qty" not in log_cols:
        conn.execute("ALTER TABLE logs ADD COLUMN qty INTEGER DEFAULT 1")
        print("[DB] Added column logs.qty")

    conn.commit()
    conn.close()
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            quantity INTEGER DEFAULT 1,
            location TEXT,
            status TEXT DEFAULT 'available'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            borrower TEXT,
            action TEXT NOT NULL,
            qty INTEGER DEFAULT 1,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
        )
    """)

    item_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "status" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'available'")
        print("[DB] Added column items.status")

    log_cols = [r[1] for r in conn.execute("PRAGMA table_info(logs)").fetchall()]
    if "qty" not in log_cols:
        conn.execute("ALTER TABLE logs ADD COLUMN qty INTEGER DEFAULT 1")
        print("[DB] Added column logs.qty")

    conn.commit()
    conn.close()


def get_outstanding(db, item_id):
    row = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN action='borrow' THEN qty ELSE -qty END), 0) AS out "
        "FROM logs WHERE item_id=?", (item_id,)
    ).fetchone()
    return max(row["out"], 0)


# ============================================================
# QR HELPERS
# ============================================================
def generate_qr(item_id, force=False):
    filename = f"item_{item_id}.png"
    filepath = os.path.join(QR_FOLDER, filename)

    if force and os.path.exists(filepath):
        os.remove(filepath)

    if not os.path.exists(filepath):
        url = f"{BASE_URL}/toggle/{item_id}"
        print(f"[QR] Generating {filename} -> {url}")
        img = qrcode.make(url)
        img.save(filepath)

    return filename


def delete_qr(item_id):
    filepath = os.path.join(QR_FOLDER, f"item_{item_id}.png")
    if os.path.exists(filepath):
        os.remove(filepath)


# ============================================================
# MAIN PAGES
# ============================================================
@app.route("/")
def index():
    db = get_db()
    items = db.execute("""
        SELECT i.*, f.name AS folder_name
        FROM items i LEFT JOIN folders f ON f.id = i.folder_id
        ORDER BY i.id DESC
    """).fetchall()
    folders = db.execute("""
        SELECT f.*, (SELECT COUNT(*) FROM items i WHERE i.folder_id = f.id) AS item_count
        FROM folders f ORDER BY f.name COLLATE NOCASE
    """).fetchall()
    return render_template("index.html", items=items, folders=folders)


@app.route("/add", methods=["GET", "POST"])
def add_item():
    db = get_db()
    folders = db.execute("SELECT * FROM folders ORDER BY name COLLATE NOCASE").fetchall()

    if request.method == "POST":
        name = request.form["name"].strip()
        description = request.form.get("description", "").strip()
        quantity = int(request.form.get("quantity", 1))
        folder_id_raw = request.form.get("folder_id", "").strip()
        folder_id = int(folder_id_raw) if folder_id_raw.isdigit() else None

        cur = db.execute(
            "INSERT INTO items (name, description, quantity, folder_id) VALUES (?, ?, ?, ?)",
            (name, description, quantity, folder_id),
        )
        db.commit()
        item_id = cur.lastrowid

        generate_qr(item_id)
        flash(f"Added '{name}' and generated its QR code!", "success")
        return redirect(url_for("index"))

    return render_template("add.html", folders=folders)
    if request.method == "POST":
        name = request.form["name"].strip()
        description = request.form.get("description", "").strip()
        quantity = int(request.form.get("quantity", 1))

        db = get_db()
        cur = db.execute(
            "INSERT INTO items (name, description, quantity) VALUES (?, ?, ?)",
            (name, description, quantity),
        )
        db.commit()
        item_id = cur.lastrowid

        generate_qr(item_id)
        flash(f"Added '{name}' and generated its QR code!", "success")
        return redirect(url_for("index"))

    return render_template("add.html")


@app.route("/item/<int:item_id>")
def view_item(item_id):
    db = get_db()
    item = db.execute("""
        SELECT i.*, f.name AS folder_name
        FROM items i LEFT JOIN folders f ON f.id = i.folder_id
        WHERE i.id = ?
    """, (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    logs = db.execute(
        "SELECT * FROM logs WHERE item_id=? ORDER BY id DESC", (item_id,)
    ).fetchall()

    outstanding = get_outstanding(db, item_id)
    return render_template("item.html", item=item, logs=logs, outstanding=outstanding)

# ============================================================
# FOLDERS
# ============================================================
@app.route("/folders")
def folders_list():
    db = get_db()
    folders = db.execute("""
        SELECT f.*,
               (SELECT COUNT(*) FROM items i WHERE i.folder_id = f.id) AS item_count,
               (SELECT COALESCE(SUM(quantity),0) FROM items i WHERE i.folder_id = f.id) AS total_qty
        FROM folders f ORDER BY f.name COLLATE NOCASE
    """).fetchall()
    # Items with no folder
    uncategorized = db.execute(
        "SELECT * FROM items WHERE folder_id IS NULL ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return render_template("folders.html", folders=folders, uncategorized=uncategorized)


@app.route("/folders/new", methods=["POST"])
def folder_new():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Folder name can't be empty.", "error")
        return redirect(url_for("folders_list"))
    db = get_db()
    db.execute(
        "INSERT INTO folders (name, created_at) VALUES (?, ?)",
        (name, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    db.commit()
    flash(f"Folder '{name}' created.", "success")
    return redirect(url_for("folders_list"))


@app.route("/folders/<int:folder_id>")
def folder_view(folder_id):
    db = get_db()
    folder = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if not folder:
        flash("Folder not found.", "error")
        return redirect(url_for("folders_list"))
    items = db.execute(
        "SELECT * FROM items WHERE folder_id = ? ORDER BY name COLLATE NOCASE",
        (folder_id,),
    ).fetchall()
    return render_template("folder_view.html", folder=folder, items=items)


@app.route("/folders/<int:folder_id>/rename", methods=["POST"])
def folder_rename(folder_id):
    db = get_db()
    folder = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if not folder:
        flash("Folder not found.", "error")
        return redirect(url_for("folders_list"))
    name = request.form.get("name", "").strip()
    if name:
        db.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
        db.commit()
        flash(f"Folder renamed to '{name}'.", "success")
    return redirect(url_for("folders_list"))


@app.route("/folders/<int:folder_id>/delete", methods=["POST"])
def folder_delete(folder_id):
    db = get_db()
    folder = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if folder:
        # Unlink items but don't delete them
        db.execute("UPDATE items SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
        db.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        db.commit()
        flash(f"Folder '{folder['name']}' deleted. Items were moved to Uncategorized.", "success")
    return redirect(url_for("folders_list"))


@app.route("/items/<int:item_id>/move", methods=["POST"])
def item_move(item_id):
    """Assign (or unassign) an item to a folder."""
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    folder_id_raw = request.form.get("folder_id", "").strip()
    folder_id = int(folder_id_raw) if folder_id_raw.isdigit() else None

    if folder_id:
        folder = db.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if not folder:
            flash("Folder not found.", "error")
            return redirect(request.referrer or url_for("index"))

    db.execute("UPDATE items SET folder_id = ? WHERE id = ?", (folder_id, item_id))
    db.commit()

    flash(f"'{item['name']}' moved.", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/edit/<int:item_id>", methods=["GET", "POST"])
def edit_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    folders = db.execute("SELECT * FROM folders ORDER BY name COLLATE NOCASE").fetchall()

    if request.method == "POST":
        name = request.form["name"].strip()
        description = request.form.get("description", "").strip()
        quantity = int(request.form.get("quantity", 1))
        new_status = "borrowed" if quantity == 0 else "available"
        folder_id_raw = request.form.get("folder_id", "").strip()
        folder_id = int(folder_id_raw) if folder_id_raw.isdigit() else None

        db.execute(
            "UPDATE items SET name=?, description=?, quantity=?, status=?, folder_id=? WHERE id=?",
            (name, description, quantity, new_status, folder_id, item_id),
        )
        db.commit()
        flash(f"Updated '{name}'.", "success")
        return redirect(url_for("view_item", item_id=item_id))

    return render_template("edit.html", item=item, folders=folders)
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form["name"].strip()
        description = request.form.get("description", "").strip()
        quantity = int(request.form.get("quantity", 1))
        new_status = "borrowed" if quantity == 0 else "available"

        db.execute(
            "UPDATE items SET name=?, description=?, quantity=?, status=? WHERE id=?",
            (name, description, quantity, new_status, item_id),
        )
        db.commit()
        flash(f"Updated '{name}'.", "success")
        return redirect(url_for("view_item", item_id=item_id))

    return render_template("edit.html", item=item)


@app.route("/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item:
        db.execute("DELETE FROM logs WHERE item_id = ?", (item_id,))
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        db.commit()
        delete_qr(item_id)
        flash(f"Deleted '{item['name']}'.", "success")
    return redirect(url_for("index"))


# ============================================================
# BORROW / RETURN (web UI)
# ============================================================
@app.route("/borrow/<int:item_id>", methods=["GET", "POST"])
def borrow_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    if item["quantity"] <= 0:
        flash(f"'{item['name']}' has no stock left to borrow.", "error")
        return redirect(url_for("view_item", item_id=item_id))

    if request.method == "POST":
        borrower = request.form.get("borrower", "").strip() or "Unknown"
        try:
            qty = int(request.form.get("qty", 1))
        except (ValueError, TypeError):
            qty = 0

        if qty < 1:
            flash("Quantity must be at least 1.", "error")
            return redirect(url_for("borrow_item", item_id=item_id))

        if qty > item["quantity"]:
            flash(f"Only {item['quantity']} in stock.", "error")
            return redirect(url_for("borrow_item", item_id=item_id))

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        new_qty = item["quantity"] - qty
        new_status = "borrowed" if new_qty == 0 else "available"

        db.execute(
            "INSERT INTO logs (item_id, borrower, action, qty, timestamp) VALUES (?, ?, 'borrow', ?, ?)",
            (item_id, borrower, qty, now),
        )
        db.execute(
            "UPDATE items SET quantity=?, status=? WHERE id=?",
            (new_qty, new_status, item_id),
        )
        db.commit()

        flash(f"{borrower} borrowed {qty} × {item['name']}. {new_qty} left.", "success")
        return redirect(url_for("view_item", item_id=item_id))

    return render_template("borrow.html", item=item)


@app.route("/return/<int:item_id>", methods=["GET", "POST"])
def return_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    outstanding = get_outstanding(db, item_id)
    if outstanding <= 0:
        flash(f"Nothing is currently borrowed for '{item['name']}'.", "error")
        return redirect(url_for("view_item", item_id=item_id))

    if request.method == "POST":
        try:
            qty = int(request.form.get("qty", outstanding))
        except (ValueError, TypeError):
            qty = 0

        if qty < 1:
            flash("Quantity must be at least 1.", "error")
            return redirect(url_for("return_item", item_id=item_id))

        if qty > outstanding:
            flash(f"Only {outstanding} borrowed — can't return more than that.", "error")
            return redirect(url_for("return_item", item_id=item_id))

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        last = db.execute(
            "SELECT borrower FROM logs WHERE item_id=? AND action='borrow' ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        borrower = last["borrower"] if last else "Unknown"

        new_qty = item["quantity"] + qty
        new_status = "borrowed" if new_qty == 0 else "available"

        db.execute(
            "INSERT INTO logs (item_id, borrower, action, qty, timestamp) VALUES (?, ?, 'return', ?, ?)",
            (item_id, borrower, qty, now),
        )
        db.execute(
            "UPDATE items SET quantity=?, status=? WHERE id=?",
            (new_qty, new_status, item_id),
        )
        db.commit()

        flash(f"Returned {qty} × {item['name']}. Stock is now {new_qty}.", "success")
        return redirect(url_for("view_item", item_id=item_id))

    return render_template("return.html", item=item, outstanding=outstanding)


# ============================================================
# QR SCAN FLOW (name prompt -> toggle)
# ============================================================
@app.route("/scan/<int:item_id>", methods=["GET", "POST"])
def scan_confirm(item_id):
    """Name + quantity entry form shown after a phone scans a QR."""
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return render_template("scan_result.html", success=False,
                               title="Item not found",
                               message=f"No item with ID {item_id}.")

    outstanding = get_outstanding(db, item_id)

    # Decide which actions are valid right now
    can_borrow = item["quantity"] > 0
    can_return = outstanding > 0

    if not can_borrow and not can_return:
        return render_template("scan_result.html", success=False,
                               title="Nothing to do",
                               message=f"'{item['name']}' has 0 stock and nothing on loan.")

    # Default to whatever's available
    action = "borrow" if can_borrow else "return"

    if request.method == "POST":
        who = request.form.get("who", "").strip()
        action = request.form.get("action", action)
        try:
            qty = int(request.form.get("qty", 1))
        except (ValueError, TypeError):
            qty = 0

        # Validate
        if not who:
            flash("Please enter your name.", "error")
            return redirect(url_for("scan_confirm", item_id=item_id))
        if qty < 1:
            flash("Quantity must be at least 1.", "error")
            return redirect(url_for("scan_confirm", item_id=item_id))
        if action == "borrow" and qty > item["quantity"]:
            flash(f"Only {item['quantity']} in stock.", "error")
            return redirect(url_for("scan_confirm", item_id=item_id))
        if action == "return" and qty > outstanding:
            flash(f"Only {outstanding} currently on loan.", "error")
            return redirect(url_for("scan_confirm", item_id=item_id))

        return redirect(url_for("toggle_item", item_id=item_id,
                                who=who, qty=qty, action=action))

    return render_template("scan_confirm.html",
                           item=item, action=action,
                           outstanding=outstanding,
                           can_borrow=can_borrow,
                           can_return=can_return)
    """Name-entry form shown after a phone scans a QR."""
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return render_template("scan_result.html", success=False,
                               title="Item not found",
                               message=f"No item with ID {item_id}.")

    outstanding = get_outstanding(db, item_id)

    if item["quantity"] > 0:
        action = "borrow"
    elif outstanding > 0:
        action = "return"
    else:
        return render_template("scan_result.html", success=False,
                               title="Nothing to do",
                               message=f"'{item['name']}' has 0 stock and nothing on loan.")

    if request.method == "POST":
        who = request.form.get("who", "").strip()
        if not who:
            flash("Please enter your name.", "error")
            return redirect(url_for("scan_confirm", item_id=item_id))
        return redirect(url_for("toggle_item", item_id=item_id, who=who))

    return render_template("scan_confirm.html",
                           item=item, action=action, outstanding=outstanding)


@app.route("/toggle/<int:item_id>", methods=["GET", "POST"])
def toggle_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return render_template("scan_result.html", success=False,
                               title="Item not found",
                               message=f"No item with ID {item_id}.")

    who = request.values.get("who", "").strip()
    action = request.values.get("action", "").strip()
    try:
        qty = int(request.values.get("qty", 1))
    except (ValueError, TypeError):
        qty = 1

    # If we don't have what we need, redirect to the form
    if not who or action not in ("borrow", "return"):
        return redirect(url_for("scan_confirm", item_id=item_id))

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    outstanding = get_outstanding(db, item_id)

    # ---- BORROW ----
    if action == "borrow":
        if qty < 1 or qty > item["quantity"]:
            return render_template("scan_result.html", success=False,
                                   title="Not enough stock",
                                   message=f"Only {item['quantity']} in stock.")
        new_qty = item["quantity"] - qty
        new_status = "borrowed" if new_qty == 0 else "available"
        db.execute(
            "INSERT INTO logs (item_id, borrower, action, qty, timestamp) "
            "VALUES (?, ?, 'borrow', ?, ?)",
            (item_id, who, qty, now),
        )
        db.execute("UPDATE items SET quantity=?, status=? WHERE id=?",
                   (new_qty, new_status, item_id))
        db.commit()
        return render_template("scan_result.html",
                               success=True, action="borrow", qty=qty,
                               title="Borrowed", item=item, who=who,
                               when_pretty=pretty_time(now),
                               stock_left=new_qty)

    # ---- RETURN ----
    else:
        if qty < 1 or qty > outstanding:
            return render_template("scan_result.html", success=False,
                                   title="Invalid return",
                                   message=f"Only {outstanding} currently on loan.")
        last = db.execute(
            "SELECT borrower FROM logs WHERE item_id=? AND action='borrow' "
            "ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        borrower = last["borrower"] if last else who
        new_qty = item["quantity"] + qty
        new_status = "borrowed" if new_qty == 0 else "available"
        db.execute(
            "INSERT INTO logs (item_id, borrower, action, qty, timestamp) "
            "VALUES (?, ?, 'return', ?, ?)",
            (item_id, borrower, qty, now),
        )
        db.execute("UPDATE items SET quantity=?, status=? WHERE id=?",
                   (new_qty, new_status, item_id))
        db.commit()
        return render_template("scan_result.html",
                               success=True, action="return", qty=qty,
                               title="Returned", item=item, who=borrower,
                               when_pretty=pretty_time(now),
                               stock_left=new_qty)
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return render_template("scan_result.html", success=False,
                               title="Item not found",
                               message=f"No item with ID {item_id}.")

    who = request.values.get("who", "").strip()
    if not who:
        return redirect(url_for("scan_confirm", item_id=item_id))

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    outstanding = get_outstanding(db, item_id)

    if item["quantity"] > 0:
        new_qty = item["quantity"] - 1
        new_status = "borrowed" if new_qty == 0 else "available"
        db.execute(
            "INSERT INTO logs (item_id, borrower, action, qty, timestamp) VALUES (?, ?, 'borrow', 1, ?)",
            (item_id, who, now),
        )
        db.execute("UPDATE items SET quantity=?, status=? WHERE id=?",
                   (new_qty, new_status, item_id))
        db.commit()
        return render_template("scan_result.html",
                               success=True, action="borrow", qty=1,
                               title="Borrowed", item=item, who=who,
                               when_pretty=pretty_time(now),
                               stock_left=new_qty)

    elif outstanding > 0:
        last = db.execute(
            "SELECT borrower FROM logs WHERE item_id=? AND action='borrow' ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        borrower = last["borrower"] if last else who
        new_qty = item["quantity"] + 1
        new_status = "borrowed" if new_qty == 0 else "available"
        db.execute(
            "INSERT INTO logs (item_id, borrower, action, qty, timestamp) VALUES (?, ?, 'return', 1, ?)",
            (item_id, borrower, now),
        )
        db.execute("UPDATE items SET quantity=?, status=? WHERE id=?",
                   (new_qty, new_status, item_id))
        db.commit()
        return render_template("scan_result.html",
                               success=True, action="return", qty=1,
                               title="Returned", item=item, who=borrower,
                               when_pretty=pretty_time(now),
                               stock_left=new_qty)

    else:
        return render_template("scan_result.html", success=False,
                               title="Nothing to do",
                               message=f"'{item['name']}' has 0 stock and nothing on loan.")


# ============================================================
# DATABASE BACKUP / RESTORE
# ============================================================
@app.route("/download-db")
def download_db():
    """Download a copy of the current SQLite database."""
    if not os.path.exists(DB_PATH):
        flash("No database file found yet.", "error")
        return redirect(url_for("index"))

    # Give it a friendly filename with a timestamp
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"inventory-backup-{stamp}.db"
    return send_file(
        DB_PATH,
        as_attachment=True,
        download_name=filename,
        mimetype="application/x-sqlite3",
    )


@app.route("/upload-db", methods=["POST"])
def upload_db():
    """Restore the database from an uploaded .db file."""
    file = request.files.get("db_file")
    if not file or file.filename == "":
        flash("Please choose a .db file first.", "error")
        return redirect(url_for("index"))

    if not file.filename.lower().endswith(".db"):
        flash("The file must be a .db file.", "error")
        return redirect(url_for("index"))

    # Read into memory, validate it's actually a SQLite file
    # (SQLite files always begin with "SQLite format 3\0")
    data = file.read()
    if not data.startswith(b"SQLite format 3\x00"):
        flash("That file doesn't look like a SQLite database.", "error")
        return redirect(url_for("index"))

    # Back up the current DB before overwriting
    if os.path.exists(DB_PATH):
        backup = DB_PATH + ".bak"
        try:
            os.replace(DB_PATH, backup)
            print(f"[db] Backed up current DB to {backup}")
        except OSError as e:
            flash(f"Couldn't back up the current DB: {e}", "error")
            return redirect(url_for("index"))

    # Write the new DB
    try:
        with open(DB_PATH, "wb") as fh:
            fh.write(data)
    except OSError as e:
        # Restore the backup if writing failed
        if os.path.exists(DB_PATH + ".bak"):
            os.replace(DB_PATH + ".bak", DB_PATH)
        flash(f"Failed to save uploaded database: {e}", "error")
        return redirect(url_for("index"))

    # Sanity check: make sure the new DB has the tables we expect
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1 FROM items LIMIT 1")
        conn.close()
    except sqlite3.Error as e:
        # Roll back to the backup — bad DB
        if os.path.exists(DB_PATH + ".bak"):
            os.replace(DB_PATH + ".bak", DB_PATH)
        flash(f"Uploaded file isn't a valid inventory database: {e}", "error")
        return redirect(url_for("index"))

    # Run migrations in case the uploaded DB is from an older version
    init_db()

    flash("Database restored successfully. Previous DB saved as database.db.bak.", "success")
    return redirect(url_for("index"))

# ============================================================
# QR IMAGE + REGEN
# ============================================================
@app.route("/qr/<int:item_id>.png")
def qr_image(item_id):
    filepath = os.path.join(QR_FOLDER, f"item_{item_id}.png")
    if not os.path.exists(filepath):
        generate_qr(item_id)
    return send_file(filepath, mimetype="image/png")


@app.route("/regen-qrs")
def regen_qrs():
    db = get_db()
    items = db.execute("SELECT id, name FROM items").fetchall()
    for it in items:
        generate_qr(it["id"], force=True)
    flash(f"Regenerated {len(items)} QR codes for {BASE_URL}", "success")
    return redirect(url_for("index"))


# ============================================================
# API + SCANNER PAGE
# ============================================================
@app.route("/api/status")
def api_status():
    db = get_db()
    rows = db.execute("SELECT id, status, quantity FROM items ORDER BY id").fetchall()
    fingerprint = ";".join(f"{r['id']}:{r['status']}:{r['quantity']}" for r in rows)
    last_log = db.execute("SELECT id FROM logs ORDER BY id DESC LIMIT 1").fetchone()
    return jsonify({
        "fingerprint": fingerprint,
        "last_log_id": last_log["id"] if last_log else 0,
    })


@app.route("/scan")
def scan():
    return render_template("scan.html")


# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        ssl_context="adhoc",
    )
    
# Initialize DB when the app is imported (by Gunicorn on Render, or by `python app.py` locally)
init_db()