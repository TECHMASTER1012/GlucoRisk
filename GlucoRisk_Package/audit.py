"""
GlucoRisk HIPAA Audit Trail
────────────────────────────
Immutable, append-only audit log for all clinical data access.
Every login, data view, export, modification, and alert is recorded.

Compliance: HIPAA §164.312(b) — Audit controls
"""

import logging
import sqlite3
from datetime import datetime
from functools import wraps
from flask import request, session, g

logger = logging.getLogger("glucorisk.audit")

# ── Audit Actions ──────────────────────────────────────
class AuditAction:
    LOGIN_SUCCESS     = "LOGIN_SUCCESS"
    LOGIN_FAILED      = "LOGIN_FAILED"
    LOGOUT            = "LOGOUT"
    REGISTER          = "REGISTER"
    VIEW_DASHBOARD    = "VIEW_DASHBOARD"
    VIEW_PROFILE      = "VIEW_PROFILE"
    UPDATE_PROFILE    = "UPDATE_PROFILE"
    VIEW_HISTORY      = "VIEW_HISTORY"
    VIEW_PATIENTS     = "VIEW_PATIENTS"
    EXPORT_PDF        = "EXPORT_PDF"
    DATA_ACCESS       = "DATA_ACCESS"
    DATA_MODIFY       = "DATA_MODIFY"
    TELEMETRY_PERSIST = "TELEMETRY_PERSIST"
    TREATMENT         = "TREATMENT"
    MANUAL_OVERRIDE   = "MANUAL_OVERRIDE"
    CONSENT_GRANT     = "CONSENT_GRANT"
    CONSENT_REVOKE    = "CONSENT_REVOKE"
    PASSWORD_CHANGE   = "PASSWORD_CHANGE"
    ACCOUNT_LOCKED    = "ACCOUNT_LOCKED"
    FED_GRADIENT      = "FED_GRADIENT"
    FED_AGGREGATION   = "FED_AGGREGATION"
    ALERT_SMS         = "ALERT_SMS"
    ALERT_EMERGENCY   = "ALERT_EMERGENCY"


def init_audit_table(conn):
    """Create the immutable audit_log table."""
    c = conn.cursor()
    c.execute('''
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        user_id INTEGER,
        username TEXT,
        action TEXT NOT NULL,
        resource TEXT,
        ip_address TEXT,
        user_agent TEXT,
        details TEXT,
        severity TEXT DEFAULT 'INFO'
    )
    ''')
    # Index for fast queries
    c.execute('CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)')
    conn.commit()


def log_audit(action, resource=None, details=None, severity="INFO", conn=None):
    """
    Record an audit event. This is append-only — no UPDATE or DELETE allowed.
    
    Args:
        action: AuditAction constant
        resource: What was accessed (e.g., "patient_profile", "telemetry_data")
        details: Additional context (e.g., "exported 50 rows")
        severity: INFO, WARNING, CRITICAL
        conn: SQLite connection (uses g.db if not provided)
    """
    try:
        if conn is None:
            conn = g.get("db")
        if conn is None:
            return
        
        user_id = session.get("user_id")
        username = session.get("username", "anonymous")
        ip = request.remote_addr if request else "system"
        ua = request.headers.get("User-Agent", "")[:200] if request else ""
        
        c = conn.cursor()
        c.execute(
            "INSERT INTO audit_log (timestamp, user_id, username, action, resource, "
            "ip_address, user_agent, details, severity) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(),
                user_id, username, action, resource,
                ip, ua, details, severity
            )
        )
        conn.commit()
        
        log_func = logger.warning if severity == "WARNING" else (
            logger.critical if severity == "CRITICAL" else logger.info
        )
        log_func(f"AUDIT: [{action}] user={username} resource={resource} "
                f"ip={ip} details={details}")
    except Exception as e:
        logger.error(f"Audit logging failed: {e}")


def audit_route(action, resource=None):
    """Decorator to automatically audit a route access."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            log_audit(action, resource=resource or f.__name__)
            return f(*args, **kwargs)
        return wrapper
    return decorator
