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
        
        # https://192.168.1.181:5000
        # https://invsys-hlx3.onrender.com

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER REFERENCES folders(id)
        )
    """)

    item_cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "status" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN status TEXT DEFAULT 'available'")
        print("[DB] Added column items.status")
    if "folder_id" not in item_cols:
        conn.execute("ALTER TABLE items ADD COLUMN folder_id INTEGER REFERENCES folders(id)")
        print("[DB] Added column items.folder_id")

    log_cols = [r[1] for r in conn.execute("PRAGMA table_info(logs)").fetchall()]
    if "qty" not in log_cols:
        conn.execute("ALTER TABLE logs ADD COLUMN qty INTEGER DEFAULT 1")
        print("[DB] Added column logs.qty")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_folder_id ON items(folder_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_parent_id ON folders(parent_id)")
    conn.commit()
    conn.close()


def get_outstanding(db, item_id):
    row = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN action='borrow' THEN qty ELSE -qty END), 0) AS out "
        "FROM logs WHERE item_id=?", (item_id,)
    ).fetchone()
    return max(row["out"], 0)


def folder_index_redirect(folder_id=None):
    if folder_id is None:
        return redirect(url_for("index"))
    return redirect(url_for("index", folder=folder_id))


def resolve_folder_context(db, raw_folder_id):
    if raw_folder_id in (None, ""):
        return None
    try:
        folder_id = int(raw_folder_id)
    except (TypeError, ValueError):
        flash("The folder location is invalid.", "error")
        return None
    if not db.execute(
        "SELECT 1 FROM folders WHERE id=?", (folder_id,)
    ).fetchone():
        flash("That folder location no longer exists.", "error")
        return None
    return folder_id


def folder_name_exists(db, name, parent_id, exclude_id=None):
    query = "SELECT 1 FROM folders WHERE LOWER(name)=LOWER(?) AND parent_id IS ?"
    params = [name, parent_id]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return db.execute(query, params).fetchone() is not None


def folder_is_descendant(db, ancestor_id, folder_id):
    seen = set()
    current = db.execute(
        "SELECT parent_id FROM folders WHERE id=?", (folder_id,)
    ).fetchone()
    while current and current["parent_id"] is not None:
        parent_id = current["parent_id"]
        if parent_id == ancestor_id:
            return True
        if parent_id in seen:
            return True
        seen.add(parent_id)
        current = db.execute(
            "SELECT parent_id FROM folders WHERE id=?", (parent_id,)
        ).fetchone()
    return False

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
    requested_folder = request.args.get("folder")
    folder_id = None
    current_folder = None
    if requested_folder is not None:
        try:
            folder_id = int(requested_folder)
        except ValueError:
            flash("That folder is invalid.", "error")
            return redirect(url_for("index"))
        current_folder = db.execute(
            "SELECT * FROM folders WHERE id=?", (folder_id,)
        ).fetchone()
        if not current_folder:
            flash("That folder no longer exists.", "error")
            return redirect(url_for("index"))

    breadcrumbs = []
    ancestor = current_folder
    seen_ancestors = set()
    while ancestor:
        if ancestor["id"] in seen_ancestors:
            break
        seen_ancestors.add(ancestor["id"])
        breadcrumbs.append(ancestor)
        if ancestor["parent_id"] is None:
            break
        ancestor = db.execute(
            "SELECT * FROM folders WHERE id=?", (ancestor["parent_id"],)
        ).fetchone()
    breadcrumbs.reverse()

    folders = db.execute("""
        SELECT f.*,
               (SELECT COUNT(*) FROM items WHERE folder_id=f.id) AS item_count,
               (SELECT COUNT(*) FROM folders AS child WHERE child.parent_id=f.id) AS folder_count
        FROM folders AS f
        WHERE f.parent_id IS ?
        ORDER BY f.name COLLATE NOCASE, f.id
    """, (folder_id,)).fetchall()
    items = db.execute("""
        SELECT i.*,
               MAX(COALESCE((
                   SELECT SUM(CASE WHEN action='borrow' THEN qty ELSE -qty END)
                   FROM logs WHERE item_id=i.id
               ), 0), 0) AS borrowed
        FROM items i
        WHERE i.folder_id IS ?
        ORDER BY i.id ASC
    """, (folder_id,)).fetchall()
    return render_template(
        "index.html",
        items=items,
        folders=folders,
        current_folder=current_folder,
        breadcrumbs=breadcrumbs,
    )


@app.route("/folders/create", methods=["POST"])
def create_folder():
    db = get_db()
    name = request.form.get("name", "").strip()
    raw_parent_id = request.form.get("parent_id", "")
    try:
        parent_id = int(raw_parent_id) if raw_parent_id else None
    except ValueError:
        flash("The parent folder is invalid.", "error")
        return redirect(url_for("index"))

    if parent_id is not None and not db.execute(
        "SELECT 1 FROM folders WHERE id=?", (parent_id,)
    ).fetchone():
        flash("The parent folder no longer exists.", "error")
        return redirect(url_for("index"))
    if not name:
        flash("Enter a folder name.", "error")
        return folder_index_redirect(parent_id)
    if folder_name_exists(db, name, parent_id):
        flash("A folder with that name already exists here.", "error")
        return folder_index_redirect(parent_id)

    db.execute(
        "INSERT INTO folders (name, parent_id) VALUES (?, ?)",
        (name, parent_id),
    )
    db.commit()
    flash(f"Created folder '{name}'.", "success")
    return folder_index_redirect(parent_id)


@app.route("/folders/rename", methods=["POST"])
def rename_folder():
    db = get_db()
    raw_folder_id = request.form.get("folder_id", "")
    name = request.form.get("name", "").strip()
    try:
        folder_id = int(raw_folder_id)
    except ValueError:
        flash("The folder is invalid.", "error")
        return redirect(url_for("index"))

    folder = db.execute(
        "SELECT * FROM folders WHERE id=?", (folder_id,)
    ).fetchone()
    if not folder:
        flash("That folder no longer exists.", "error")
        return redirect(url_for("index"))
    if not name:
        flash("Enter a folder name.", "error")
        return folder_index_redirect(folder["parent_id"])
    if folder_name_exists(db, name, folder["parent_id"], folder_id):
        flash("A folder with that name already exists here.", "error")
        return folder_index_redirect(folder["parent_id"])

    db.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
    db.commit()
    flash(f"Renamed folder to '{name}'.", "success")
    return folder_index_redirect(folder["parent_id"])


@app.route("/folders/<int:folder_id>/delete", methods=["POST"])
def delete_folder(folder_id):
    db = get_db()
    folder = db.execute(
        "SELECT * FROM folders WHERE id=?", (folder_id,)
    ).fetchone()
    if not folder:
        flash("That folder no longer exists.", "error")
        return redirect(url_for("index"))

    folder_ids = [
        row["id"] for row in db.execute("""
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM folders WHERE id=?
                UNION
                SELECT child.id
                FROM folders AS child
                JOIN subtree ON child.parent_id=subtree.id
            )
            SELECT id FROM subtree
        """, (folder_id,)).fetchall()
    ]
    placeholders = ",".join("?" for _ in folder_ids)
    item_ids = [
        row["id"] for row in db.execute(
            f"SELECT id FROM items WHERE folder_id IN ({placeholders})",
            folder_ids,
        ).fetchall()
    ]
    if item_ids:
        item_placeholders = ",".join("?" for _ in item_ids)
        db.execute(
            f"DELETE FROM logs WHERE item_id IN ({item_placeholders})",
            item_ids,
        )
        db.execute(
            f"DELETE FROM items WHERE id IN ({item_placeholders})",
            item_ids,
        )
    db.execute(
        f"DELETE FROM folders WHERE id IN ({placeholders})",
        folder_ids,
    )
    db.commit()
    for item_id in item_ids:
        delete_qr(item_id)

    flash(f"Deleted folder '{folder['name']}' and its contents.", "success")
    return folder_index_redirect(folder["parent_id"])


@app.route("/items/<int:item_id>/move", methods=["POST"])
def move_item(item_id):
    db = get_db()
    item = db.execute(
        "SELECT id, folder_id FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if not item:
        return jsonify(error="That item no longer exists."), 404

    raw_folder_id = request.form.get("folder_id", "")
    try:
        folder_id = int(raw_folder_id) if raw_folder_id else None
    except ValueError:
        return jsonify(error="The destination folder is invalid."), 400
    if folder_id is not None and not db.execute(
        "SELECT 1 FROM folders WHERE id=?", (folder_id,)
    ).fetchone():
        return jsonify(error="The destination folder no longer exists."), 404
    if item["folder_id"] == folder_id:
        return jsonify(error="That item is already in this location."), 400

    db.execute("UPDATE items SET folder_id=? WHERE id=?", (folder_id, item_id))
    db.commit()
    return jsonify(message="Item moved.")


@app.route("/folders/<int:folder_id>/move", methods=["POST"])
def move_folder(folder_id):
    db = get_db()
    folder = db.execute(
        "SELECT id, name, parent_id FROM folders WHERE id=?", (folder_id,)
    ).fetchone()
    if not folder:
        return jsonify(error="That folder no longer exists."), 404

    raw_parent_id = request.form.get("folder_id", "")
    try:
        parent_id = int(raw_parent_id) if raw_parent_id else None
    except ValueError:
        return jsonify(error="The destination folder is invalid."), 400
    if parent_id is not None and not db.execute(
        "SELECT 1 FROM folders WHERE id=?", (parent_id,)
    ).fetchone():
        return jsonify(error="The destination folder no longer exists."), 404
    if parent_id == folder_id or (
        parent_id is not None and folder_is_descendant(db, folder_id, parent_id)
    ):
        return jsonify(error="A folder cannot be moved inside itself."), 400
    if folder["parent_id"] == parent_id:
        return jsonify(error="That folder is already in this location."), 400
    if folder_name_exists(db, folder["name"], parent_id, folder_id):
        return jsonify(error="A folder with that name already exists there."), 409

    db.execute(
        "UPDATE folders SET parent_id=? WHERE id=?", (parent_id, folder_id)
    )
    db.commit()
    return jsonify(message="Folder moved.")


@app.route("/add", methods=["GET", "POST"])
def add_item():
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
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    logs = db.execute(
        "SELECT * FROM logs WHERE item_id=? ORDER BY id DESC", (item_id,)
    ).fetchall()

    outstanding = get_outstanding(db, item_id)
    folder_id = resolve_folder_context(db, request.args.get("folder"))
    return render_template(
        "item.html",
        item=item,
        logs=logs,
        outstanding=outstanding,
        folder_id=folder_id,
    )


@app.route("/edit/<int:item_id>", methods=["GET", "POST"])
def edit_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        folder_id = resolve_folder_context(db, request.form.get("folder_id"))
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
        return redirect(url_for("view_item", item_id=item_id, folder=folder_id))

    folder_id = resolve_folder_context(db, request.args.get("folder"))
    return render_template("edit.html", item=item, folder_id=folder_id)


@app.route("/delete/<int:item_id>", methods=["POST"])
def delete_item(item_id):
    db = get_db()
    folder_id = resolve_folder_context(db, request.form.get("folder_id"))
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item:
        db.execute("DELETE FROM logs WHERE item_id = ?", (item_id,))
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        db.commit()
        delete_qr(item_id)
        flash(f"Deleted '{item['name']}'.", "success")
    return folder_index_redirect(folder_id)


# ============================================================
# BORROW / RETURN (web UI)
# ============================================================
@app.route("/borrow/<int:item_id>", methods=["GET", "POST"])
def borrow_item(item_id):
    db = get_db()
    folder_id = resolve_folder_context(db, request.values.get("folder"))
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    if item["quantity"] <= 0:
        flash(f"'{item['name']}' has no stock left to borrow.", "error")
        return redirect(url_for("view_item", item_id=item_id, folder=folder_id))

    if request.method == "POST":
        borrower = request.form.get("borrower", "").strip() or "Unknown"
        try:
            qty = int(request.form.get("qty", 1))
        except (ValueError, TypeError):
            qty = 0

        if qty < 1:
            flash("Quantity must be at least 1.", "error")
            return redirect(url_for("borrow_item", item_id=item_id, folder=folder_id))

        if qty > item["quantity"]:
            flash(f"Only {item['quantity']} in stock.", "error")
            return redirect(url_for("borrow_item", item_id=item_id, folder=folder_id))

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
        return redirect(url_for("view_item", item_id=item_id, folder=folder_id))

    return render_template("borrow.html", item=item, folder_id=folder_id)


@app.route("/return/<int:item_id>", methods=["GET", "POST"])
def return_item(item_id):
    db = get_db()
    folder_id = resolve_folder_context(db, request.values.get("folder"))
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        flash("Item not found.", "error")
        return redirect(url_for("index"))

    outstanding = get_outstanding(db, item_id)
    if outstanding <= 0:
        flash(f"Nothing is currently borrowed for '{item['name']}'.", "error")
        return redirect(url_for("view_item", item_id=item_id, folder=folder_id))

    if request.method == "POST":
        try:
            qty = int(request.form.get("qty", outstanding))
        except (ValueError, TypeError):
            qty = 0

        if qty < 1:
            flash("Quantity must be at least 1.", "error")
            return redirect(url_for("return_item", item_id=item_id, folder=folder_id))

        if qty > outstanding:
            flash(f"Only {outstanding} borrowed — can't return more than that.", "error")
            return redirect(url_for("return_item", item_id=item_id, folder=folder_id))

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
        return redirect(url_for("view_item", item_id=item_id, folder=folder_id))

    return render_template(
        "return.html", item=item, outstanding=outstanding, folder_id=folder_id
    )


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