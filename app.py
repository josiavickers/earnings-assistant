import hashlib
import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, render_template, g

try:
    import pypdf
    def get_page_count(path):
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            return len(reader.pages)
    def get_pdf_metadata(path):
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            info = reader.metadata or {}
            return {
                "title": info.get("/Title", ""),
                "author": info.get("/Author", ""),
                "creator": info.get("/Creator", ""),
                "pages": len(reader.pages),
            }
except ImportError:
    pypdf = None
    def get_page_count(path):
        return None
    def get_pdf_metadata(path):
        return {"title": "", "author": "", "creator": "", "pages": None}

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "pdfs.db")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"pdf"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------- Database ----------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = sqlite3.connect(DB_PATH)
        db.execute("""
            CREATE TABLE IF NOT EXISTS pdf_metadata (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                filename    TEXT NOT NULL,
                file_size   INTEGER NOT NULL,
                sha256      TEXT NOT NULL,
                pages       INTEGER,
                title       TEXT,
                author      TEXT,
                creator     TEXT,
                uploaded_at TEXT NOT NULL
            )
        """)
        # Add sha256 column to existing DBs that predate this change
        try:
            db.execute("ALTER TABLE pdf_metadata ADD COLUMN sha256 TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        db.commit()
        db.close()

# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Only PDF files are accepted"}), 400

    # Read into a temp buffer so we can hash before writing to disk
    file_bytes = file.read()
    file_size = len(file_bytes)
    sha256 = hashlib.sha256(file_bytes).hexdigest()

    # Duplicate check — same hash AND same size
    db = get_db()
    existing = db.execute(
        "SELECT id, filename FROM pdf_metadata WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if existing:
        return jsonify({
            "error": "duplicate",
            "message": f'This file has already been uploaded as "{existing["filename"]}" (entry #{existing["id"]}).',
            "existing_id": existing["id"],
        }), 409

    # Save file
    safe_name = os.path.basename(file.filename)
    save_path = os.path.join(UPLOAD_FOLDER, safe_name)
    if os.path.exists(save_path):
        base, ext = os.path.splitext(safe_name)
        safe_name = f"{base}_{int(datetime.utcnow().timestamp())}{ext}"
        save_path = os.path.join(UPLOAD_FOLDER, safe_name)
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    meta = get_pdf_metadata(save_path)
    uploaded_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    cur = db.execute(
        """INSERT INTO pdf_metadata (filename, file_size, sha256, pages, title, author, creator, uploaded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (safe_name, file_size, sha256, meta["pages"], meta["title"], meta["author"], meta["creator"], uploaded_at),
    )
    db.commit()
    row_id = cur.lastrowid

    return jsonify({
        "id": row_id,
        "filename": safe_name,
        "file_size": file_size,
        "sha256": sha256,
        "pages": meta["pages"],
        "title": meta["title"],
        "author": meta["author"],
        "creator": meta["creator"],
        "uploaded_at": uploaded_at,
    }), 201

@app.route("/pdfs", methods=["GET"])
def list_pdfs():
    db = get_db()
    rows = db.execute("SELECT * FROM pdf_metadata ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/pdfs/<int:pdf_id>", methods=["DELETE"])
def delete_pdf(pdf_id):
    db = get_db()
    row = db.execute("SELECT filename FROM pdf_metadata WHERE id = ?", (pdf_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    path = os.path.join(UPLOAD_FOLDER, row["filename"])
    if os.path.exists(path):
        os.remove(path)
    db.execute("DELETE FROM pdf_metadata WHERE id = ?", (pdf_id,))
    db.commit()
    return jsonify({"deleted": pdf_id})

# ---------- Main ----------

if __name__ == "__main__":
    init_db()
    print("Starting PDF Upload Server at http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
