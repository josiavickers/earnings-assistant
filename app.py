import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime

# Load .env file if present (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from flask import Flask, request, jsonify, render_template, g

# ---------- PDF handling ----------

try:
    import pypdf

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

    def extract_pdf_text(path):
        with open(path, "rb") as f:
            reader = pypdf.PdfReader(f)
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)

except ImportError:
    pypdf = None

    def get_pdf_metadata(path):
        return {"title": "", "author": "", "creator": "", "pages": None}

    def extract_pdf_text(path):
        return ""

# ---------- Pydantic validation ----------

try:
    from pydantic import BaseModel, Field, field_validator, ValidationError
    from typing import Optional

    class EarningsReport(BaseModel):
        model_config = {"extra": "ignore"}   # silently drop unexpected keys from LLM

        company_name:                   Optional[str]   = None
        quarter_end_date:               Optional[str]   = None
        fiscal_quarter:                 Optional[str]   = None
        fiscal_year:                    Optional[int]   = None
        currency:                       Optional[str]   = None
        unit_raw:                       Optional[str]   = None
        revenue_current:                Optional[float] = None
        revenue_previous_quarter:       Optional[float] = None
        revenue_same_quarter_last_year: Optional[float] = None
        pbt_current:                    Optional[float] = None
        pbt_previous_quarter:           Optional[float] = None
        pbt_same_quarter_last_year:     Optional[float] = None
        management_commentary:          Optional[str]   = None
        outlook_summary:                Optional[str]   = None
        confidence_score:               Optional[float] = None

    _pydantic_available = True

except ImportError:
    _pydantic_available = False
    ValidationError = Exception


VALID_QUARTERS = {"Q1", "Q2", "Q3", "Q4"}
VALID_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_analysis(raw: dict) -> list[str]:
    """Run Pydantic type checks and cross-field consistency checks on the
    LLM output dict.  Returns a (possibly empty) list of warning strings.
    The LLM dict is NOT mutated; warnings are purely informational."""

    warnings: list[str] = []

    # ---- 1. Pydantic type / coercion check ----
    if _pydantic_available:
        try:
            EarningsReport(**{k: v for k, v in raw.items() if k != "analysis_error"})
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(x) for x in err["loc"])
                warnings.append(f"Type error — {field}: {err['msg']}")
    else:
        warnings.append("pydantic not installed — type validation skipped")

    r = raw  # shorthand for cross-field checks below

    # ---- 2. fiscal_quarter allowed values ----
    fq = r.get("fiscal_quarter")
    if fq is not None and fq not in VALID_QUARTERS:
        warnings.append(f"fiscal_quarter '{fq}' is not one of Q1/Q2/Q3/Q4")

    # ---- 3. fiscal_year plausibility ----
    fy = r.get("fiscal_year")
    current_year = datetime.utcnow().year
    if fy is not None:
        try:
            fy = int(fy)
            if not (2000 <= fy <= current_year + 1):
                warnings.append(
                    f"fiscal_year {fy} is outside the expected range "
                    f"(2000–{current_year + 1})"
                )
        except (ValueError, TypeError):
            warnings.append(f"fiscal_year '{fy}' cannot be parsed as an integer")

    # ---- 4. quarter_end_date format and year consistency ----
    qed = r.get("quarter_end_date")
    if qed is not None:
        if not DATE_RE.match(str(qed)):
            warnings.append(
                f"quarter_end_date '{qed}' is not in YYYY-MM-DD format"
            )
        elif fy is not None:
            date_year = int(str(qed)[:4])
            if abs(date_year - int(fy)) > 1:
                warnings.append(
                    f"quarter_end_date year ({date_year}) does not match "
                    f"fiscal_year ({fy}) — possible extraction error"
                )

    # ---- 5. currency format (ISO 4217: 3 uppercase letters) ----
    currency = r.get("currency")
    if currency is not None and not VALID_CURRENCY_RE.match(str(currency)):
        warnings.append(
            f"currency '{currency}' is not a valid ISO 4217 code "
            f"(expected 3 uppercase letters, e.g. USD)"
        )

    # ---- 6. Revenue must be non-negative ----
    for field in (
        "revenue_current",
        "revenue_previous_quarter",
        "revenue_same_quarter_last_year",
    ):
        val = r.get(field)
        if val is not None and val < 0:
            warnings.append(
                f"{field} is negative ({val:.2f}M) — revenue cannot be negative"
            )

    # ---- 7. PBT must not exceed revenue (when revenue is positive) ----
    pairs = [
        ("revenue_current",                "pbt_current"),
        ("revenue_previous_quarter",       "pbt_previous_quarter"),
        ("revenue_same_quarter_last_year", "pbt_same_quarter_last_year"),
    ]
    for rev_field, pbt_field in pairs:
        rev = r.get(rev_field)
        pbt = r.get(pbt_field)
        if rev is not None and pbt is not None and rev > 0 and pbt > rev:
            warnings.append(
                f"{pbt_field} ({pbt:.2f}M) exceeds {rev_field} ({rev:.2f}M) "
                f"— profit before tax cannot exceed revenue"
            )

    # ---- 8. confidence_score bounds ----
    score = r.get("confidence_score")
    if score is not None:
        try:
            score = float(score)
            if not (0.0 <= score <= 1.0):
                warnings.append(
                    f"confidence_score {score} is outside the valid range [0, 1]"
                )
            elif score < 0.7:
                warnings.append(
                    f"Low confidence score ({score:.0%}) — results should be "
                    f"reviewed manually"
                )
        except (ValueError, TypeError):
            warnings.append(f"confidence_score '{score}' cannot be parsed as a number")

    # ---- 9. No financial values extracted at all ----
    financial_fields = [
        "revenue_current", "revenue_previous_quarter", "revenue_same_quarter_last_year",
        "pbt_current", "pbt_previous_quarter", "pbt_same_quarter_last_year",
    ]
    if all(r.get(f) is None for f in financial_fields):
        warnings.append(
            "No financial values were extracted — the PDF may not contain "
            "machine-readable financial tables"
        )

    return warnings


# ---------- OpenAI ----------

try:
    from openai import OpenAI as _OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False

EARNINGS_SYSTEM_PROMPT = """You are a financial analyst assistant specialised in parsing quarterly earnings reports.

Extract structured data from the provided report text and return ONLY a valid JSON object with these exact fields:

{
  "company_name":                    "<full legal company name>",
  "quarter_end_date":                "<YYYY-MM-DD or null>",
  "fiscal_quarter":                  "<Q1 | Q2 | Q3 | Q4>",
  "fiscal_year":                     <integer>,
  "currency":                        "<ISO 4217 code e.g. USD, GBP, EUR>",
  "unit_raw":                        "<the unit label as it appears in the report, e.g. '$000s', '£m', 'millions', 'billions'>",
  "revenue_current":                 <revenue this quarter, normalised to millions, or null>,
  "revenue_previous_quarter":        <revenue in the immediately preceding quarter, normalised to millions, or null>,
  "revenue_same_quarter_last_year":  <revenue in the same quarter of the prior fiscal year, normalised to millions, or null>,
  "pbt_current":                     <profit before tax this quarter, normalised to millions, or null>,
  "pbt_previous_quarter":            <profit before tax in the immediately preceding quarter, normalised to millions, or null>,
  "pbt_same_quarter_last_year":      <profit before tax in the same quarter of the prior fiscal year, normalised to millions, or null>,
  "management_commentary":           "<2-3 sentence summary of key management remarks>",
  "outlook_summary":                 "<2-3 sentence summary of forward guidance or outlook>",
  "confidence_score":                <float 0.0-1.0: your overall confidence in the accuracy of the extracted fields>
}

Rules:
- ALL monetary values must be normalised to millions of the reported currency, regardless of how they appear in the document (e.g. if the report states values in thousands, multiply by 0.001; if in billions, multiply by 1000).
- Record the original unit label from the document in unit_raw so the normalisation can be verified.
- If a field cannot be determined from the text, use null. Do NOT fabricate or estimate values.
- Return ONLY the JSON object — no markdown, no extra text."""


def analyse_earnings(pdf_text: str) -> dict:
    """Call GPT-4o-mini to extract structured earnings data from PDF text.

    Returns a dict with the 10 structured fields, or a dict containing
    'analysis_error' if the call could not be completed.
    """
    if not _openai_available:
        return {"analysis_error": "openai package not installed — run: pip install openai"}

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"analysis_error": "OPENAI_API_KEY environment variable is not set"}

    try:
        client = _OpenAI(api_key=api_key)

        # Truncate to ~100 k chars (~25 k tokens) to stay within context limits
        truncated_text = pdf_text[:100_000]

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0,
            messages=[
                {"role": "system", "content": EARNINGS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Parse this quarterly earnings report and return the structured JSON:\n\n"
                        + truncated_text
                    ),
                },
            ],
        )

        result = json.loads(response.choices[0].message.content)
        return result

    except Exception as exc:
        error_msg = str(exc)
        print(f"\n[ERROR] GPT-4o-mini analysis failed: {error_msg}\n")
        return {"analysis_error": error_msg}


# ---------- Growth calculations ----------

# Maps each fiscal quarter to its predecessor (prev_quarter, year_delta)
_PREV_QUARTER: dict[str, tuple[str, int]] = {
    "Q1": ("Q4", -1),
    "Q2": ("Q1",  0),
    "Q3": ("Q2",  0),
    "Q4": ("Q3",  0),
}


def pct_change(current, prior) -> float | None:
    """((current - prior) / |prior|) × 100, rounded to 2 dp.
    Returns None if either value is missing or prior is zero."""
    if current is None or prior is None or prior == 0:
        return None
    return round(((current - prior) / abs(prior)) * 100, 2)


def compute_qoq_yoy(analysis: dict, db) -> dict:
    """Calculate revenue and PBT QoQ / YoY growth rates.

    Lookup priority for comparison values:
      1. DB — a previous upload for the same company / quarter / year
      2. Fallback — comparative figures already extracted from the current report
      3. null — if neither source has the data
    """
    result = {
        "revenue_qoq": None,
        "revenue_yoy": None,
        "pbt_qoq":     None,
        "pbt_yoy":     None,
    }

    if analysis.get("analysis_error"):
        return result

    company     = analysis.get("company_name")
    fq          = analysis.get("fiscal_quarter")
    fy          = analysis.get("fiscal_year")
    rev_current = analysis.get("revenue_current")
    pbt_current = analysis.get("pbt_current")

    # ---- Previous quarter ----
    rev_prev = pbt_prev = None

    if company and fq and fy and fq in _PREV_QUARTER:
        pq, year_delta = _PREV_QUARTER[fq]
        py = fy + year_delta
        row = db.execute(
            """SELECT revenue_current, pbt_current FROM pdf_metadata
               WHERE LOWER(company_name) = LOWER(?)
                 AND fiscal_quarter = ? AND fiscal_year = ?
                 AND analysis_error IS NULL
               ORDER BY uploaded_at DESC LIMIT 1""",
            (company, pq, py),
        ).fetchone()
        if row:
            rev_prev = row["revenue_current"]
            pbt_prev = row["pbt_current"]
            print(f"[QoQ] DB match: {company} {pq} {py}")

    # Fallback to LLM-extracted comparative figures
    if rev_prev is None:
        rev_prev = analysis.get("revenue_previous_quarter")
    if pbt_prev is None:
        pbt_prev = analysis.get("pbt_previous_quarter")

    result["revenue_qoq"] = pct_change(rev_current, rev_prev)
    result["pbt_qoq"]     = pct_change(pbt_current, pbt_prev)

    # ---- Same quarter last year ----
    rev_ly = pbt_ly = None

    if company and fq and fy:
        row = db.execute(
            """SELECT revenue_current, pbt_current FROM pdf_metadata
               WHERE LOWER(company_name) = LOWER(?)
                 AND fiscal_quarter = ? AND fiscal_year = ?
                 AND analysis_error IS NULL
               ORDER BY uploaded_at DESC LIMIT 1""",
            (company, fq, fy - 1),
        ).fetchone()
        if row:
            rev_ly = row["revenue_current"]
            pbt_ly = row["pbt_current"]
            print(f"[YoY] DB match: {company} {fq} {fy - 1}")

    # Fallback to LLM-extracted comparative figures
    if rev_ly is None:
        rev_ly = analysis.get("revenue_same_quarter_last_year")
    if pbt_ly is None:
        pbt_ly = analysis.get("pbt_same_quarter_last_year")

    result["revenue_yoy"] = pct_change(rev_current, rev_ly)
    result["pbt_yoy"]     = pct_change(pbt_current, pbt_ly)

    return result


# ---------- Flask app ----------

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "pdfs.db")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"pdf"}

# Analysis columns added to pdf_metadata; listed here for migration
ANALYSIS_COLUMNS = [
    ("company_name",                   "TEXT"),
    ("quarter_end_date",               "TEXT"),
    ("fiscal_quarter",                 "TEXT"),
    ("fiscal_year",                    "INTEGER"),
    ("currency",                       "TEXT"),
    ("unit_raw",                       "TEXT"),
    ("revenue_current",                "REAL"),
    ("revenue_previous_quarter",       "REAL"),
    ("revenue_same_quarter_last_year", "REAL"),
    ("pbt_current",                    "REAL"),
    ("pbt_previous_quarter",           "REAL"),
    ("pbt_same_quarter_last_year",     "REAL"),
    ("management_commentary",          "TEXT"),
    ("outlook_summary",                "TEXT"),
    ("confidence_score",               "REAL"),
    ("analysis_error",                 "TEXT"),
    ("validation_warnings",            "TEXT"),
    ("revenue_qoq",                    "REAL"),
    ("revenue_yoy",                    "REAL"),
    ("pbt_qoq",                        "REAL"),
    ("pbt_yoy",                        "REAL"),
]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


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
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                filename              TEXT NOT NULL,
                file_size             INTEGER NOT NULL,
                sha256                TEXT NOT NULL DEFAULT '',
                pages                 INTEGER,
                title                 TEXT,
                author                TEXT,
                creator               TEXT,
                uploaded_at           TEXT NOT NULL,
                company_name          TEXT,
                quarter_end_date      TEXT,
                fiscal_quarter        TEXT,
                fiscal_year           INTEGER,
                currency                       TEXT,
                unit_raw                       TEXT,
                revenue_current                REAL,
                revenue_previous_quarter       REAL,
                revenue_same_quarter_last_year REAL,
                pbt_current                    REAL,
                pbt_previous_quarter           REAL,
                pbt_same_quarter_last_year     REAL,
                management_commentary          TEXT,
                outlook_summary                TEXT,
                confidence_score               REAL,
                analysis_error                 TEXT,
                validation_warnings            TEXT,
                revenue_qoq                    REAL,
                revenue_yoy                    REAL,
                pbt_qoq                        REAL,
                pbt_yoy                        REAL
            )
        """)
        db.commit()

        # Migrate existing databases that are missing newer columns
        existing_cols = {row[1] for row in db.execute("PRAGMA table_info(pdf_metadata)")}
        for col_name, col_type in [("sha256", "TEXT NOT NULL DEFAULT ''")] + ANALYSIS_COLUMNS:
            if col_name not in existing_cols:
                try:
                    db.execute(f"ALTER TABLE pdf_metadata ADD COLUMN {col_name} {col_type}")
                    db.commit()
                    print(f"[DB] Added column: {col_name}")
                except sqlite3.OperationalError as e:
                    print(f"[DB] Could not add column {col_name}: {e}")

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

    # Read into memory so we can hash before touching disk
    file_bytes = file.read()
    file_size = len(file_bytes)
    sha256 = hashlib.sha256(file_bytes).hexdigest()

    # Duplicate check
    db = get_db()
    existing = db.execute(
        "SELECT id, filename FROM pdf_metadata WHERE sha256 = ? AND file_size = ?",
        (sha256, file_size),
    ).fetchone()
    if existing:
        return jsonify({
            "error": "duplicate",
            "message": (
                f'This file has already been uploaded as "{existing["filename"]}"'
                f" (entry #{existing['id']})."
            ),
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

    # Basic PDF metadata
    meta = get_pdf_metadata(save_path)
    uploaded_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # GPT-4o-mini earnings analysis
    print(f"\n[INFO] Starting GPT-4o-mini analysis for: {safe_name}")
    pdf_text = extract_pdf_text(save_path)
    analysis = analyse_earnings(pdf_text)

    analysis_error = analysis.get("analysis_error")

    # Validate the LLM output (only when we got a real response, not an API error)
    if analysis_error:
        warnings = []
    else:
        warnings = validate_analysis(analysis)

    validation_warnings_json = json.dumps(warnings) if warnings else None

    # QoQ / YoY growth rates (DB-first, then report fallback)
    growth = compute_qoq_yoy(analysis, db)

    # ---- Debug output (printed after validation so all derived fields are included) ----
    debug_output = {k: v for k, v in analysis.items() if k != "analysis_error"}
    debug_output["analysis_error"]      = analysis_error
    debug_output["validation_warnings"] = warnings if warnings else None
    debug_output["revenue_qoq"]         = growth["revenue_qoq"]
    debug_output["revenue_yoy"]         = growth["revenue_yoy"]
    debug_output["pbt_qoq"]             = growth["pbt_qoq"]
    debug_output["pbt_yoy"]             = growth["pbt_yoy"]
    print("\n" + "=" * 50)
    print("GPT-4o-mini Earnings Analysis")
    print("=" * 50)
    print(json.dumps(debug_output, indent=2, ensure_ascii=False))
    print("=" * 50 + "\n")

    cur = db.execute(
        """INSERT INTO pdf_metadata (
               filename, file_size, sha256, pages, title, author, creator, uploaded_at,
               company_name, quarter_end_date, fiscal_quarter, fiscal_year, currency,
               unit_raw,
               revenue_current, revenue_previous_quarter, revenue_same_quarter_last_year,
               pbt_current, pbt_previous_quarter, pbt_same_quarter_last_year,
               management_commentary, outlook_summary, confidence_score,
               analysis_error, validation_warnings,
               revenue_qoq, revenue_yoy, pbt_qoq, pbt_yoy
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            safe_name, file_size, sha256, meta["pages"],
            meta["title"], meta["author"], meta["creator"], uploaded_at,
            analysis.get("company_name"),
            analysis.get("quarter_end_date"),
            analysis.get("fiscal_quarter"),
            analysis.get("fiscal_year"),
            analysis.get("currency"),
            analysis.get("unit_raw"),
            analysis.get("revenue_current"),
            analysis.get("revenue_previous_quarter"),
            analysis.get("revenue_same_quarter_last_year"),
            analysis.get("pbt_current"),
            analysis.get("pbt_previous_quarter"),
            analysis.get("pbt_same_quarter_last_year"),
            analysis.get("management_commentary"),
            analysis.get("outlook_summary"),
            analysis.get("confidence_score"),
            analysis_error,
            validation_warnings_json,
            growth["revenue_qoq"],
            growth["revenue_yoy"],
            growth["pbt_qoq"],
            growth["pbt_yoy"],
        ),
    )
    db.commit()
    row_id = cur.lastrowid

    return jsonify({
        "id":                             row_id,
        "filename":                       safe_name,
        "file_size":                      file_size,
        "pages":                          meta["pages"],
        "uploaded_at":                    uploaded_at,
        "company_name":                   analysis.get("company_name"),
        "quarter_end_date":               analysis.get("quarter_end_date"),
        "fiscal_quarter":                 analysis.get("fiscal_quarter"),
        "fiscal_year":                    analysis.get("fiscal_year"),
        "currency":                       analysis.get("currency"),
        "unit_raw":                       analysis.get("unit_raw"),
        "revenue_current":                analysis.get("revenue_current"),
        "revenue_previous_quarter":       analysis.get("revenue_previous_quarter"),
        "revenue_same_quarter_last_year": analysis.get("revenue_same_quarter_last_year"),
        "pbt_current":                    analysis.get("pbt_current"),
        "pbt_previous_quarter":           analysis.get("pbt_previous_quarter"),
        "pbt_same_quarter_last_year":     analysis.get("pbt_same_quarter_last_year"),
        "management_commentary":          analysis.get("management_commentary"),
        "outlook_summary":                analysis.get("outlook_summary"),
        "confidence_score":               analysis.get("confidence_score"),
        "analysis_error":                 analysis_error,
        "validation_warnings":            warnings if warnings else None,
        "revenue_qoq":                    growth["revenue_qoq"],
        "revenue_yoy":                    growth["revenue_yoy"],
        "pbt_qoq":                        growth["pbt_qoq"],
        "pbt_yoy":                        growth["pbt_yoy"],
    }), 201


@app.route("/pdfs", methods=["GET"])
def list_pdfs():
    db = get_db()
    rows = db.execute("SELECT * FROM pdf_metadata ORDER BY id DESC").fetchall()
    result = []
    for r in rows:
        row = dict(r)
        # Deserialise validation_warnings from JSON string back to a list
        raw_warnings = row.get("validation_warnings")
        row["validation_warnings"] = json.loads(raw_warnings) if raw_warnings else None
        result.append(row)
    return jsonify(result)


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
    print("Starting Earnings Report Assistant at http://localhost:5000")
    if not os.environ.get("OPENAI_API_KEY"):
        print("[WARNING] OPENAI_API_KEY is not set — GPT-4o-mini analysis will be skipped.")
    app.run(debug=True, host="0.0.0.0", port=5000)
