import os
import re
import time
import json
import logging
from datetime import datetime
from io import BytesIO
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, Response, jsonify, send_file)
import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
import hmac
import hashlib

from glucorisk_app import GlucoRiskApp, FIELD_HINTS, ACTIVITY_LABELS
from audit import init_audit_table, log_audit, audit_route, AuditAction
from encryption import encrypt_field, decrypt_field

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("glucorisk")

# ── App Setup ─────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "glucorisk-static-fallback-secret-9942a")

# Session hardening
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
    PERMANENT_SESSION_LIFETIME=3600,  # 1 hour
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,  # 1MB max upload
)

# ── CSRF Protection ──────────────────────────────────────────
from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect(app)
# Exempt SSE and JSON API endpoints from CSRF (they use session auth)
csrf_exempt_views = []

# ── Rate Limiting ─────────────────────────────────────────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

DB_PATH = "glucorisk.db"
app_logic = GlucoRiskApp()

# ── Database ──────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'patient',
        failed_attempts INTEGER DEFAULT 0,
        locked_until TEXT DEFAULT NULL
    )
    ''')
    c.execute('''
    CREATE TABLE IF NOT EXISTS entries (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        timestamp TEXT NOT NULL,
        glucose REAL,
        heart_rate REAL,
        gsr REAL,
        spo2 REAL,
        stress REAL,
        age REAL,
        bmi REAL,
        activity REAL,
        risk TEXT,
        score INTEGER,
        source TEXT DEFAULT 'form',
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    ''')
    c.execute('''
    CREATE TABLE IF NOT EXISTS profiles (
        user_id INTEGER PRIMARY KEY,
        display_name TEXT DEFAULT '',
        age REAL DEFAULT 25,
        bmi REAL DEFAULT 22,
        emergency_contact TEXT DEFAULT '',
        blood_type TEXT DEFAULT '',
        allergies TEXT DEFAULT '',
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    ''')
    c.execute('''
    CREATE TABLE IF NOT EXISTS consents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        consent_type TEXT NOT NULL,
        granted_at TEXT,
        revoked_at TEXT DEFAULT NULL,
        ip_address TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    ''')
    # Schema migration for older DBs
    for col, default in [("source", "'form'"), ("role", "'patient'"),
                         ("failed_attempts", "0"), ("locked_until", "NULL")]:
        try:
            table = "entries" if col == "source" else "users"
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    init_audit_table(conn)
    conn.commit()

def setup():
    init_db()
    logger.info("Database initialized with clinical tables")

# ── Auth + RBAC + Password Policy ─────────────────────────────
USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,30}$')
PASSWORD_RE = re.compile(r'^(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=]).{8,}$')
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    """Decorator to restrict route to specific roles (admin, doctor, patient)."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            user_role = session.get("role", "patient")
            if user_role not in roles:
                flash("Access denied: insufficient permissions", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapper
    return decorator

def jwt_required(f):
    """Decorator for edge device API auth via Bearer token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Allow session auth (browser) OR JWT (edge device)
        if "user_id" in session:
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            payload = verify_jwt(token)
            if payload:
                g.jwt_user = payload
                return f(*args, **kwargs)
        return jsonify({"error": "Authentication required"}), 401
    return decorated

def generate_jwt(user_id, username, role="patient"):
    """Simple HMAC-SHA256 JWT for edge devices."""
    import base64
    header = base64.urlsafe_b64encode(json.dumps({"alg":"HS256","typ":"JWT"}).encode()).decode().rstrip("=")
    exp = int(time.time()) + 86400  # 24h
    payload_data = {"user_id": user_id, "username": username, "role": role, "exp": exp}
    payload = base64.urlsafe_b64encode(json.dumps(payload_data).encode()).decode().rstrip("=")
    secret = app.secret_key
    sig = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{header}.{payload}.{sig}"

def verify_jwt(token):
    """Verify HMAC-SHA256 JWT."""
    import base64
    try:
        parts = token.split(".")
        if len(parts) != 3: return None
        header, payload, sig = parts
        secret = app.secret_key
        expected = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return None
        pad = lambda s: s + "=" * (-len(s) % 4)
        data = json.loads(base64.urlsafe_b64decode(pad(payload)))
        if data.get("exp", 0) < time.time(): return None
        return data
    except Exception:
        return None

def query_user(username):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    return c.fetchone()

def is_account_locked(user):
    """Check if account is locked due to failed attempts."""
    locked = user["locked_until"] if "locked_until" in user.keys() else None
    if locked:
        if datetime.fromisoformat(locked) > datetime.now():
            return True
    return False

# ── Routes ────────────────────────────────────────────────────
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        if not username or not password:
            flash("Username and password required", "warning")
            return redirect(url_for("register"))
        
        if not USERNAME_RE.match(username):
            flash("Username must be 3-30 characters, alphanumeric and underscores only", "warning")
            return redirect(url_for("register"))
        
        if not PASSWORD_RE.match(password):
            flash("Password must be 8+ chars with 1 uppercase, 1 digit, and 1 special character (!@#$%^&*)", "warning")
            return redirect(url_for("register"))
        
        if query_user(username):
            flash("Username already taken", "warning")
            return redirect(url_for("register"))
        
        hashed = generate_password_hash(password)
        conn = get_db()
        c = conn.cursor()
        
        # Check if first user → auto-promote to admin
        c.execute("SELECT COUNT(*) as cnt FROM users")
        is_first = c.fetchone()["cnt"] == 0
        role = "admin" if is_first else "patient"
        
        c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                  (username, hashed, role))
        conn.commit()
        log_audit(AuditAction.REGISTER, "users", f"New {role}: {username}")
        logger.info(f"New user registered: {username} (role={role})")
        flash(f"Registration successful{' (admin)' if is_first else ''}. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query_user(username)
        
        if user and is_account_locked(user):
            log_audit(AuditAction.LOGIN_FAILED, "auth", f"Account locked: {username}", severity="WARNING")
            flash(f"Account locked. Try again after {LOCKOUT_MINUTES} minutes.", "danger")
            return redirect(url_for("login"))
        
        if user and check_password_hash(user["password"], password):
            # Reset failed attempts on success
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (user["id"],))
            conn.commit()
            
            session.permanent = True
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"] if "role" in user.keys() else "patient"
            log_audit(AuditAction.LOGIN_SUCCESS, "auth", f"Role: {session['role']}")
            logger.info(f"User logged in: {username} (role={session['role']})")
            return redirect(url_for("dashboard"))
        else:
            # Increment failed attempts
            if user:
                conn = get_db()
                c = conn.cursor()
                attempts = (user["failed_attempts"] or 0) + 1
                locked = None
                if attempts >= MAX_FAILED_ATTEMPTS:
                    from datetime import timedelta
                    locked = (datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
                    log_audit(AuditAction.ACCOUNT_LOCKED, "auth", f"Locked: {username}", severity="CRITICAL")
                c.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                          (attempts, locked, user["id"]))
                conn.commit()
            log_audit(AuditAction.LOGIN_FAILED, "auth", f"Failed: {username}", severity="WARNING")
            logger.warning(f"Failed login attempt for: {username}")
            flash("Invalid credentials", "danger")
            return redirect(url_for("login"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    username = session.get("username", "unknown")
    session.clear()
    logger.info(f"User logged out: {username}")
    flash("You have been logged out", "info")
    return redirect(url_for("login"))

# ── SSE Stream ────────────────────────────────────────────────
@app.route("/stream")
@login_required
@csrf.exempt
def stream():
    """Server-Sent Events stream for real-time telemetry."""
    session_id = session.get("user_id")
    username = session.get("username", "Safal Gupta")
    logger.info(f"SSE stream opened for user {username} (session {session_id})")
    
    return Response(
        app_logic.yield_live_data(session_id, username),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )

# ── API Endpoints ─────────────────────────────────────────────
@app.route("/api/status")
@login_required
@csrf.exempt
def api_status():
    """Health check endpoint for hardware detection status."""
    hw_active = (time.time() - getattr(app_logic, 'last_hardware_time', 0)) < 5
    session_id = session.get("user_id")
    session_exists = session_id in app_logic.sessions
    return jsonify({
        "hardware_connected": hw_active,
        "serial_port": app_logic.ser.port if app_logic.ser and app_logic.ser.is_open else None,
        "session_active": session_exists,
        "model_loaded": app_logic.model is not None,
        "server_uptime": time.time()
    })

@app.route("/api/history")
@login_required
@csrf.exempt
def api_history():
    """Fetch telemetry history for the current user."""
    conn = get_db()
    c = conn.cursor()
    limit = min(int(request.args.get("limit", 100)), 500)
    c.execute(
        "SELECT timestamp, glucose, heart_rate, gsr, spo2, risk, score, source "
        "FROM entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (session["user_id"], limit)
    )
    rows = c.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/export_pdf")
@login_required
@csrf.exempt
def export_pdf():
    """Generate a clinical PDF report for the current patient."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT timestamp, glucose, heart_rate, spo2, gsr, risk, score "
        "FROM entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT 50",
        (session["user_id"],)
    )
    rows = c.fetchall()
    username = session.get("username", "Unknown")
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=18, textColor=colors.HexColor("#0f172a"))
    subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'], fontSize=10, textColor=colors.grey)
    
    elements = []
    elements.append(Paragraph("GlucoRisk Clinical Report", title_style))
    elements.append(Paragraph(f"Patient: {username} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", subtitle_style))
    elements.append(Spacer(1, 10*mm))
    
    if rows:
        table_data = [["Timestamp", "Glucose", "HR", "SpO2", "GSR", "Risk", "Score"]]
        for r in rows:
            table_data.append([
                r["timestamp"][:19] if r["timestamp"] else "",
                f"{r['glucose']:.1f}" if r['glucose'] else "—",
                f"{r['heart_rate']:.0f}" if r['heart_rate'] else "—",
                f"{r['spo2']:.1f}" if r['spo2'] else "—",
                f"{r['gsr']:.0f}" if r['gsr'] else "—",
                r["risk"] or "—",
                str(r["score"]) if r["score"] else "—"
            ])
        
        t = Table(table_data, repeatRows=1)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#1e293b")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('FONTSIZE', (0, 1), (-1, -1), 7),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ]))
        elements.append(t)
    else:
        elements.append(Paragraph("No telemetry entries found.", styles['Normal']))
    
    doc.build(elements)
    buffer.seek(0)
    logger.info(f"PDF report generated for user {username}")
    return send_file(buffer, mimetype='application/pdf',
                     download_name=f"glucorisk_report_{username}_{datetime.now().strftime('%Y%m%d')}.pdf",
                     as_attachment=True)

# ── Treatment & Override ──────────────────────────────────────
@app.route("/administer_treatment", methods=["POST"])
@login_required
@csrf.exempt
def administer_treatment():
    data = request.get_json()
    if not data or "treatment" not in data:
        return jsonify({"status": "error", "message": "Invalid request"}), 400
    
    treatment = data["treatment"]
    session_id = session.get("user_id")
    if session_id in app_logic.sessions:
        app_logic.sessions[session_id]["intervention_queue"].append(treatment)
        logger.info(f"Treatment administered: {treatment} for session {session_id}")
        return jsonify({"status": "success", "message": f"Administered {treatment}"})
    return jsonify({"status": "error", "message": "Simulation not ready"}), 500

@app.route("/inject_telemetry", methods=["POST"])
@login_required
@csrf.exempt
def inject_telemetry():
    """Manual override of live telemetry data."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Invalid request"}), 400
        
    session_id = session.get("user_id")
    if session_id in app_logic.sessions:
        try:
            sd = app_logic.sessions[session_id]
            sd["inputs"]["glucose"] = float(data.get("glucose", 100))
            sd["inputs"]["heart_rate"] = float(data.get("heart_rate", 80))
            sd["inputs"]["spo2"] = float(data.get("spo2", 98))
            sd["inputs"]["gsr"] = float(data.get("gsr", 200))
            
            mode = data.get("mode", "manual")
            sd["simulation_mode"] = mode
            logger.info(f"Telemetry injected: mode={mode}, glucose={sd['inputs']['glucose']}")
            return jsonify({"status": "success", "message": f"Telemetry overridden, mode set to {mode}"})
        except ValueError:
            return jsonify({"status": "error", "message": "Invalid telemetry values"}), 400
    return jsonify({"status": "error", "message": "Simulation not running"}), 500

# ── Dashboard ─────────────────────────────────────────────────
@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    if request.method == "POST":
        data = {}
        for key in FIELD_HINTS.keys():
            raw = request.form.get(key)
            try:
                data[key] = float(raw)
            except (TypeError, ValueError):
                flash(f"Invalid numeric input provided for {key}.", "danger")
                return redirect(url_for("dashboard"))
        
        result = app_logic.local_inference(data)
        
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO entries (user_id, timestamp, glucose, heart_rate, gsr, spo2, stress, age, bmi, activity, risk, score, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session["user_id"],
                datetime.now().isoformat(),
                data.get("glucose"), data.get("heart_rate"), data.get("gsr"),
                data.get("spo2"), data.get("stress"), data.get("age"),
                data.get("bmi"), data.get("activity"),
                result.get("risk"), result.get("score"), "form"
            ),
        )
        conn.commit()
        flash(f"Prediction: {result.get('risk')} ({result.get('score')}%)", "success")
        return redirect(url_for("dashboard"))

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM entries WHERE user_id = ? ORDER BY timestamp",
        (session["user_id"],),
    )
    entries = c.fetchall()

    timestamps = [e["timestamp"] for e in entries]
    glucose_values = [e["glucose"] for e in entries]
    scores = [e["score"] for e in entries]
    risk_labels = [e["risk"] for e in entries]

    return render_template(
        "dashboard.html",
        entries=entries,
        timestamps=timestamps,
        glucose_values=glucose_values,
        scores=scores,
        risk_labels=risk_labels,
        FIELD_HINTS=FIELD_HINTS,
        ACTIVITY_LABELS=ACTIVITY_LABELS,
    )

# ── Patient Profile ───────────────────────────────────────────
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    conn = get_db()
    c = conn.cursor()
    
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()[:50]
        age = float(request.form.get("age", 25))
        bmi = float(request.form.get("bmi", 22))
        emergency_contact = request.form.get("emergency_contact", "").strip()[:20]
        blood_type = request.form.get("blood_type", "").strip()[:5]
        allergies = request.form.get("allergies", "").strip()[:200]
        
        c.execute("SELECT user_id FROM profiles WHERE user_id = ?", (session["user_id"],))
        if c.fetchone():
            c.execute(
                "UPDATE profiles SET display_name=?, age=?, bmi=?, emergency_contact=?, blood_type=?, allergies=? WHERE user_id=?",
                (display_name, age, bmi, emergency_contact, blood_type, allergies, session["user_id"])
            )
        else:
            c.execute(
                "INSERT INTO profiles (user_id, display_name, age, bmi, emergency_contact, blood_type, allergies) VALUES (?,?,?,?,?,?,?)",
                (session["user_id"], display_name, age, bmi, emergency_contact, blood_type, allergies)
            )
        conn.commit()
        logger.info(f"Profile updated for user {session['username']}")
        flash("Profile saved successfully", "success")
        return redirect(url_for("profile"))
    
    c.execute("SELECT * FROM profiles WHERE user_id = ?", (session["user_id"],))
    prof = c.fetchone()
    
    c.execute("SELECT COUNT(*) as cnt FROM entries WHERE user_id = ?", (session["user_id"],))
    entry_count = c.fetchone()["cnt"]
    
    return render_template("profile.html", profile=prof, entry_count=entry_count)

# ── History Page ──────────────────────────────────────────────
@app.route("/history")
@login_required
def history_page():
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT timestamp, glucose, heart_rate, spo2, gsr, risk, score, source "
        "FROM entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT 200",
        (session["user_id"],)
    )
    entries = c.fetchall()
    return render_template("history.html", entries=entries)

# ── Auto-Persist Telemetry ────────────────────────────────────
@app.route("/api/persist_telemetry", methods=["POST"])
@login_required
@csrf.exempt
def persist_telemetry():
    """Auto-save SSE telemetry data to DB (called from frontend every N readings)."""
    data = request.get_json()
    if not data:
        return jsonify({"status": "error"}), 400
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO entries (user_id, timestamp, glucose, heart_rate, gsr, spo2, stress, age, bmi, activity, risk, score, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session["user_id"],
            data.get("timestamp", datetime.now().isoformat()),
            data.get("glucose"), data.get("heart_rate"), data.get("gsr"),
            data.get("spo2"), data.get("stress", 0), data.get("age", 25),
            data.get("bmi", 22), data.get("activity", 0),
            data.get("risk", "NORMAL"), data.get("score", 0),
            data.get("source", "sse")
        )
    )
    conn.commit()
    return jsonify({"status": "saved"})

# ── Session Stats ─────────────────────────────────────────────
@app.route("/api/stats")
@login_required
@csrf.exempt
def api_stats():
    """Return session statistics for the current user."""
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) as cnt, "
        "MIN(glucose) as min_glu, MAX(glucose) as max_glu, AVG(glucose) as avg_glu, "
        "MIN(heart_rate) as min_hr, MAX(heart_rate) as max_hr, AVG(heart_rate) as avg_hr, "
        "MIN(spo2) as min_spo2, MAX(spo2) as max_spo2, AVG(spo2) as avg_spo2, "
        "SUM(CASE WHEN risk='HIGH_RISK' THEN 1 ELSE 0 END) as high_count, "
        "SUM(CASE WHEN risk='MODERATE_RISK' THEN 1 ELSE 0 END) as mod_count, "
        "SUM(CASE WHEN risk='NORMAL' THEN 1 ELSE 0 END) as normal_count "
        "FROM entries WHERE user_id = ?",
        (session["user_id"],)
    )
    row = c.fetchone()
    return jsonify(dict(row) if row else {})

# ── Federated Learning API ────────────────────────────────────
from federated import FederatedServer, FederatedClient

_fed_server = None
def get_fed_server():
    global _fed_server
    if _fed_server is None:
        model_path = os.path.join(os.path.dirname(__file__), "model.json")
        _fed_server = FederatedServer(model_path)
    return _fed_server

@app.route("/api/fedavg", methods=["POST"])
@login_required
@csrf.exempt
def fedavg_receive():
    """Receive gradient update from a patient edge device."""
    data = request.get_json()
    if not data or "weight_deltas" not in data:
        return jsonify({"error": "Invalid gradient payload"}), 400
    
    server = get_fed_server()
    pending = server.receive_update(data)
    
    # Auto-aggregate when we have 2+ client updates
    if pending >= 2:
        result = server.aggregate(min_clients=2)
        if result:
            logger.info(f"FedAvg round {server.round_number} completed")
            return jsonify({
                "status": "aggregated",
                "round": server.round_number,
                "clients": result.get("contributing_clients")
            })
    
    return jsonify({"status": "received", "pending": pending})

@app.route("/api/global_model")
@csrf.exempt
def global_model():
    """Serve the current global model weights for edge devices."""
    server = get_fed_server()
    model = server.get_global_model()
    if model:
        return jsonify(model)
    return jsonify({"error": "Model not available"}), 404

@app.route("/api/fed_status")
@jwt_required
@csrf.exempt
def fed_status():
    """Return federated learning status."""
    server = get_fed_server()
    return jsonify(server.get_status())

# ── Multi-Patient Admin View ─────────────────────────────────
@app.route("/patients")
@login_required
def patients_view():
    conn = get_db()
    c = conn.cursor()
    
    # Get all patients (users with entries)
    c.execute("""
        SELECT u.id, u.username,
               COUNT(e.id) as reading_count,
               MAX(e.timestamp) as last_reading,
               p.display_name, p.blood_type, p.age, p.bmi
        FROM users u
        LEFT JOIN entries e ON u.id = e.user_id
        LEFT JOIN profiles p ON u.id = p.user_id
        GROUP BY u.id
        ORDER BY last_reading DESC
    """)
    patients = c.fetchall()
    
    # Get risk distribution per patient
    c.execute("""
        SELECT user_id, risk, COUNT(*) as cnt
        FROM entries
        GROUP BY user_id, risk
    """)
    risk_data = c.fetchall()
    risk_by_patient = {}
    for r in risk_data:
        uid = r["user_id"]
        if uid not in risk_by_patient:
            risk_by_patient[uid] = {}
        risk_by_patient[uid][r["risk"]] = r["cnt"]
    
    return render_template("patients.html",
                          patients=patients,
                          risk_by_patient=risk_by_patient)

_app_start_time = time.time()

# ── Health Check Endpoints (Clinical Monitoring) ─────────────
@app.route("/health")
@csrf.exempt
def health_check():
    """Readiness probe: checks DB connection, model, and uptime."""
    checks = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1")
        conn.close()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    
    model_path = os.path.join(os.path.dirname(__file__), "model.json")
    checks["model"] = "ok" if os.path.exists(model_path) else "missing"
    checks["serial"] = "connected" if app_logic.ser else "disconnected"
    checks["uptime_seconds"] = int(time.time() - _app_start_time)
    checks["status"] = "healthy" if all(v in ("ok", "connected", "disconnected") for v in [checks["database"], checks["model"]]) else "degraded"
    
    code = 200 if checks["status"] == "healthy" else 503
    return jsonify(checks), code

@app.route("/health/live")
@csrf.exempt
def health_live():
    """Liveness probe: always returns 200."""
    return jsonify({"status": "alive"}), 200

# ── JWT Token Endpoint (Edge Device Auth) ─────────────────────
@app.route("/api/auth/token", methods=["POST"])
@csrf.exempt
@limiter.limit("10 per minute")
def issue_token():
    """Issue JWT for edge device authentication. No browser session needed."""
    data = request.get_json() or {}
    username = data.get("username", "")
    password = data.get("password", "")
    
    user = query_user(username)
    if not user or not check_password_hash(user["password"], password):
        log_audit(AuditAction.LOGIN_FAILED, "jwt_auth", f"JWT denied: {username}", severity="WARNING")
        return jsonify({"error": "Invalid credentials"}), 401
    
    if is_account_locked(user):
        return jsonify({"error": "Account locked"}), 423
    
    role = user["role"] if "role" in user.keys() else "patient"
    token = generate_jwt(user["id"], user["username"], role)
    log_audit(AuditAction.LOGIN_SUCCESS, "jwt_auth", f"JWT issued for: {username}")
    return jsonify({"token": token, "expires_in": 86400, "role": role})

# ── Consent Management ────────────────────────────────────────
CONSENT_TYPES = ["data_collection", "sms_alerts", "telemetry_sharing", "research_use"]

@app.route("/consent", methods=["GET", "POST"])
@login_required
def consent_page():
    conn = get_db()
    c = conn.cursor()
    
    if request.method == "POST":
        consent_type = request.form.get("consent_type", "")
        action = request.form.get("action", "")
        
        if consent_type not in CONSENT_TYPES:
            flash("Invalid consent type", "warning")
            return redirect(url_for("consent_page"))
        
        if action == "grant":
            c.execute("INSERT INTO consents (user_id, consent_type, granted_at, ip_address) VALUES (?,?,?,?)",
                      (session["user_id"], consent_type, datetime.now().isoformat(), request.remote_addr))
            log_audit(AuditAction.CONSENT_GRANT, "consents", f"Granted: {consent_type}")
            flash(f"Consent granted: {consent_type}", "success")
        elif action == "revoke":
            c.execute("UPDATE consents SET revoked_at=? WHERE user_id=? AND consent_type=? AND revoked_at IS NULL",
                      (datetime.now().isoformat(), session["user_id"], consent_type))
            log_audit(AuditAction.CONSENT_REVOKE, "consents", f"Revoked: {consent_type}")
            flash(f"Consent revoked: {consent_type}", "warning")
        conn.commit()
        return redirect(url_for("consent_page"))
    
    # Get current consents
    c.execute("SELECT consent_type, granted_at, revoked_at FROM consents WHERE user_id=? ORDER BY granted_at DESC",
              (session["user_id"],))
    all_consents = c.fetchall()
    
    # Build active consent state
    active = {}
    for ct in CONSENT_TYPES:
        active[ct] = any(r["consent_type"] == ct and r["revoked_at"] is None for r in all_consents)
    
    return render_template("consent.html", consent_types=CONSENT_TYPES, active=active, history=all_consents)

# ── Password Change ───────────────────────────────────────────
@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new_pwd = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT password FROM users WHERE id=?", (session["user_id"],))
        user = c.fetchone()
        
        if not check_password_hash(user["password"], current):
            flash("Current password is incorrect", "danger")
            return redirect(url_for("change_password"))
        
        if new_pwd != confirm:
            flash("New passwords don't match", "warning")
            return redirect(url_for("change_password"))
        
        if not PASSWORD_RE.match(new_pwd):
            flash("Password must be 8+ chars with 1 uppercase, 1 digit, and 1 special character", "warning")
            return redirect(url_for("change_password"))
        
        c.execute("UPDATE users SET password=? WHERE id=?",
                  (generate_password_hash(new_pwd), session["user_id"]))
        conn.commit()
        log_audit(AuditAction.PASSWORD_CHANGE, "users", "Password changed")
        flash("Password changed successfully", "success")
        return redirect(url_for("dashboard"))
    
    return render_template("change_password.html")

# ── Audit Log Viewer (Admin Only) ─────────────────────────────
@app.route("/admin/audit")
@login_required
@role_required("admin")
def audit_viewer():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT 100")
    logs = c.fetchall()
    return render_template("audit.html", logs=logs)


# ── Error Handlers ────────────────────────────────────────────
@app.errorhandler(429)
def ratelimit_handler(e):
    flash("Too many requests. Please wait a moment.", "warning")
    return redirect(url_for("login")), 429

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad request"}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"Internal server error: {e}")
    return jsonify({"error": "Internal server error"}), 500

# ── Entry Point ───────────────────────────────────────────────
if __name__ == "__main__":
    from werkzeug.serving import run_simple
    with app.app_context():
        setup()
    
    logger.info("Starting GlucoRisk on http://localhost:5001 (threaded mode)")
    run_simple("0.0.0.0", 5001, app, use_reloader=False, use_debugger=False, threaded=True)
