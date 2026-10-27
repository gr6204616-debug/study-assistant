import hmac
import os
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# رمز الأدمن مشتق من كلمة المرور (لا تُخزَّن كلمة المرور نفسها في المتصفح)
ADMIN_TOKEN = hmac.new(ADMIN_PASSWORD.encode(), b"study-admin", "sha256").hexdigest()

SCHEMA = """
CREATE TABLE IF NOT EXISTS lectures (
    id SERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    file_url TEXT NOT NULL,
    type TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id SERIAL PRIMARY KEY,
    subject_id TEXT NOT NULL,
    q_text TEXT NOT NULL,
    a_text TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    date TEXT NOT NULL
);
"""

_schema_ready = False


def run(sql, params=(), fetch=True):
    """تنفيذ استعلام واحد وإرجاع النتائج كقواميس."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return cur.fetchall() if fetch else None
    finally:
        conn.close()


@app.before_request
def ensure_schema():
    global _schema_ready
    if not request.path.startswith("/api/"):
        return
    if not _schema_ready:
        run(SCHEMA, fetch=False)
        _schema_ready = True


def is_admin():
    token = request.headers.get("X-Admin-Token", "")
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(token, ADMIN_TOKEN)


def admin_only():
    if not is_admin():
        return jsonify({"error": "غير مصرح"}), 401
    return None


def body():
    return request.get_json(silent=True) or {}


@app.errorhandler(Exception)
def on_error(e):
    app.logger.exception(e)
    return jsonify({"error": "خطأ في الخادم"}), 500


# ---------- الصفحة الرئيسية ----------
INDEX_FILE = Path(__file__).resolve().parent.parent / "public" / "index.html"


@app.get("/")
def home():
    return Response(INDEX_FILE.read_text(encoding="utf-8"), mimetype="text/html")


# ---------- تسجيل الدخول ----------
@app.post("/api/login")
def login():
    pw = str(body().get("password", ""))
    if ADMIN_PASSWORD and hmac.compare_digest(pw, ADMIN_PASSWORD):
        return jsonify({"role": "admin", "name": "مهدي", "token": ADMIN_TOKEN})
    return jsonify({"error": "كلمة المرور خاطئة"}), 401


# ---------- التحديثات (الشارة الحمراء) ----------
@app.get("/api/updates/check")
def updates_check():
    rows = run("SELECT subject_id, MAX(id) AS max_id FROM lectures GROUP BY subject_id")
    return jsonify({r["subject_id"]: r["max_id"] for r in rows})


# ---------- المحاضرات ----------
@app.get("/api/lectures/<subject_id>")
def lectures_list(subject_id):
    return jsonify(run("SELECT * FROM lectures WHERE subject_id = %s ORDER BY id DESC", (subject_id,)))


@app.post("/api/lectures/add")
def lectures_add():
    denied = admin_only()
    if denied:
        return denied
    d = body()
    title = str(d.get("title", "")).strip()[:300]
    url = str(d.get("file_url", "")).strip()[:2000]
    subject = str(d.get("subject_id", "")).strip()[:50]
    if not (title and subject and url.lower().startswith(("http://", "https://"))):
        return jsonify({"error": "بيانات غير صالحة"}), 400
    row = run(
        "INSERT INTO lectures (subject_id, title, file_url, type) VALUES (%s,%s,%s,%s) RETURNING id",
        (subject, title, url, d.get("type")),
    )[0]
    return jsonify(row)


@app.delete("/api/lectures/<int:lec_id>")
def lectures_delete(lec_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM lectures WHERE id = %s", (lec_id,), fetch=False)
    return jsonify({"ok": True})


# ---------- الأسئلة ----------
@app.get("/api/questions/<subject_id>")
def questions_list(subject_id):
    return jsonify(run("SELECT * FROM questions WHERE subject_id = %s ORDER BY id DESC", (subject_id,)))


@app.post("/api/questions/add")
def questions_add():
    d = body()
    text = str(d.get("q_text", "")).strip()[:1000]
    subject = str(d.get("subject_id", "")).strip()[:50]
    if not (text and subject):
        return jsonify({"error": "بيانات غير صالحة"}), 400
    row = run("INSERT INTO questions (subject_id, q_text) VALUES (%s,%s) RETURNING id", (subject, text))[0]
    return jsonify(row)


@app.post("/api/questions/<int:q_id>/answer")
def questions_answer(q_id):
    denied = admin_only()
    if denied:
        return denied
    text = str(body().get("a_text", "")).strip()[:2000]
    if not text:
        return jsonify({"error": "الجواب فارغ"}), 400
    run("UPDATE questions SET a_text = %s WHERE id = %s", (text, q_id), fetch=False)
    return jsonify({"ok": True})


@app.delete("/api/questions/<int:q_id>")
def questions_delete(q_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM questions WHERE id = %s", (q_id,), fetch=False)
    return jsonify({"ok": True})


# ---------- الرسائل الأسبوعية ----------
@app.get("/api/messages")
def messages_list():
    return jsonify(run("SELECT * FROM messages ORDER BY id DESC"))


@app.post("/api/messages/add")
def messages_add():
    denied = admin_only()
    if denied:
        return denied
    d = body()
    text = str(d.get("text", "")).strip()[:2000]
    if not text:
        return jsonify({"error": "الرسالة فارغة"}), 400
    when = str(d.get("date") or date.today().isoformat())[:30]
    run("INSERT INTO messages (text, date) VALUES (%s,%s)", (text, when), fetch=False)
    return jsonify({"ok": True})


@app.delete("/api/messages/<int:m_id>")
def messages_delete(m_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM messages WHERE id = %s", (m_id,), fetch=False)
    return jsonify({"ok": True})
