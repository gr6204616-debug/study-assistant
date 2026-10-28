import hmac
import os
from datetime import date
from pathlib import Path
import json
import urllib.request
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
ALTER TABLE questions ADD COLUMN IF NOT EXISTS asker TEXT;
ALTER TABLE questions ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE questions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE TABLE IF NOT EXISTS replies (
    id SERIAL PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    if not request.path.startswith("/api/") or request.path == "/api/login":
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
    if not ADMIN_PASSWORD:
        return jsonify({"error": "ADMIN_PASSWORD غير مضبوط على الخادم"}), 500
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
    
    # --- إرسال الإشعار لجروب تيليجرام ---
    # --- إرسال الإشعار لجروب تيليجرام ---
    try:
        msg = (
            f"📢 <b>تم رفع ملف جديد!</b>\n\n"
            f"📚 المادة: {subject}\n"
            f"📄 العنوان: {title}\n\n"
            f"🌐 <b>لتحميل الملف والوصول لجميع المواد، ادخل إلى المنصة:</b>\n"
            f"<a href='https://study-assistant-ivory-two.vercel.app'>https://study-assistant-ivory-two.vercel.app</a>"
        )
        send_telegram_notification(msg)
    except Exception:
        pass
        
    return jsonify(row)


@app.delete("/api/lectures/<int:lec_id>")
def lectures_delete(lec_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM lectures WHERE id = %s", (lec_id,), fetch=False)
    return jsonify({"ok": True})


# ---------- تعديل عنوان محاضرة ----------
@app.put("/api/lectures/<int:lec_id>")
def lectures_update(lec_id):
    denied = admin_only()
    if denied:
        return denied
    title = str(body().get("title", "")).strip()[:300]
    if not title:
        return jsonify({"error": "العنوان فارغ"}), 400
    run("UPDATE lectures SET title = %s WHERE id = %s", (title, lec_id), fetch=False)
    return jsonify({"ok": True})


# ---------- الأسئلة والمراسلة ----------
def iso(rows):
    for r in rows:
        if r.get("created_at") is not None:
            r["created_at"] = r["created_at"].isoformat()
    return rows


@app.get("/api/questions/<subject_id>")
def questions_list(subject_id):
    qs = iso(run(
        "SELECT id, subject_id, q_text, a_text, asker, is_admin, created_at "
        "FROM questions WHERE subject_id = %s ORDER BY id DESC LIMIT 200",
        (subject_id,),
    ))
    ids = [q["id"] for q in qs]
    replies = iso(run(
        "SELECT id, question_id, is_admin, author, text, created_at "
        "FROM replies WHERE question_id = ANY(%s) ORDER BY id",
        (ids,),
    )) if ids else []
    by_q = {}
    for r in replies:
        by_q.setdefault(r["question_id"], []).append(r)
    for q in qs:
        q["replies"] = by_q.get(q["id"], [])
    return jsonify(qs)


@app.post("/api/questions/add")
def questions_add():
    d = body()
    text = str(d.get("q_text", "")).strip()[:1000]
    subject = str(d.get("subject_id", "")).strip()[:50]
    if not (text and subject):
        return jsonify({"error": "بيانات غير صالحة"}), 400
    admin = is_admin()
    asker = "مهدي (الأدمن)" if admin else (str(d.get("asker", "")).strip()[:40] or "طالب")
    row = run(
        "INSERT INTO questions (subject_id, q_text, asker, is_admin) VALUES (%s,%s,%s,%s) RETURNING id",
        (subject, text, asker, admin),
    )[0]
    return jsonify(row)


@app.post("/api/questions/<int:q_id>/reply")
def questions_reply(q_id):
    d = body()
    text = str(d.get("text", "")).strip()[:2000]
    if not text:
        return jsonify({"error": "الرد فارغ"}), 400
    if not run("SELECT 1 FROM questions WHERE id = %s", (q_id,)):
        return jsonify({"error": "السؤال غير موجود"}), 404
    admin = is_admin()
    author = "مهدي (الأدمن)" if admin else (str(d.get("author", "")).strip()[:40] or "طالب")
    run(
        "INSERT INTO replies (question_id, is_admin, author, text) VALUES (%s,%s,%s,%s)",
        (q_id, admin, author, text),
        fetch=False,
    )
    return jsonify({"ok": True})


@app.delete("/api/replies/<int:r_id>")
def replies_delete(r_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM replies WHERE id = %s", (r_id,), fetch=False)
    return jsonify({"ok": True})


@app.delete("/api/questions/<int:q_id>")
def questions_delete(q_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM questions WHERE id = %s", (q_id,), fetch=False)
    return jsonify({"ok": True})


@app.get("/api/admin/pending")
def admin_pending():
    denied = admin_only()
    if denied:
        return denied
    rows = run(
        "SELECT q.subject_id, COUNT(*) AS c FROM questions q "
        "WHERE q.is_admin = FALSE AND (q.a_text IS NULL OR q.a_text = '') "
        "AND NOT EXISTS (SELECT 1 FROM replies r WHERE r.question_id = q.id AND r.is_admin) "
        "GROUP BY q.subject_id"
    )
    return jsonify({r["subject_id"]: r["c"] for r in rows})


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
    
    # --- إرسال الإشعار لجروب تيليجرام ---
    try:
        msg = f"📣 <b>تبليغ جديد للدفعة ({when})</b>\n\n{text}"
        send_telegram_notification(msg)
    except Exception:
        pass

    return jsonify({"ok": True})

@app.delete("/api/messages/<int:m_id>")
def messages_delete(m_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM messages WHERE id = %s", (m_id,), fetch=False)
    return jsonify({"ok": True})
def send_telegram_notification(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID")
    
    # إذا نسينا المتغيرات في Vercel، الدالة تتوقف بصمت دون تخريب الموقع
    if not token or not chat_id:
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    
    try:
        urllib.request.urlopen(req, timeout=5) # مهلة 5 ثوانٍ كي لا يتجمد الموقع
    except Exception as e:
        app.logger.error(f"فشل إرسال إشعار تيليجرام: {e}")
# أضف هذه الدالة تحت دالة send_telegram_notification السابقة
def send_telegram_reply(chat_id, message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token: return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        app.logger.error(f"فشل إرسال الرد: {e}")

# أضف هذا الـ Route الجديد في نهاية الملف لاستقبال الرسائل
@app.post("/api/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    
    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"]
        
        # 1. الرد على كلمات الجدول والتبليغات
        if any(keyword in text for keyword in ["باچر شعدنة", "شعدنا باجر", "الجدول", "تبليغ"]):
            rows = run("SELECT text, date FROM messages ORDER BY id DESC LIMIT 1")
            if rows:
                msg = f"📅 <b>تبليغ الأسبوع ({rows[0]['date']}):</b>\n\n{rows[0]['text']}"
            else:
                msg = "لا توجد تبليغات مسجلة حالياً."
            send_telegram_reply(chat_id, msg)
            
        # 2. الرد على طلب الملفات والمحاضرات بشكل مخصص وبحث ذكي
        elif "ملف" in text or "محاضرة" in text or "محاضره" in text:
            # قائمة الكلمات التي سيتجاهلها البوت تماماً لكي لا تفسد البحث
            stop_words = [
                # كلمات الطلب والرجاء
                "اريد", "ريد", "محتاج", "ممكن", "دز", "دزلي", "دزولي", "انطيني", "عطني", 
                "بلا", "زحمة", "زحمه", "فدوة", "فدوه", "رحم", "والديك", "الله", "يخليك",
                "عفية", "عفيه", "سمحتوا", "احد", "عنده", "عدكم", "موجود",
                
                # المصطلحات العامة للمواد
                "ملف", "الملف", "ملفات", "ملزمة", "الملزمة", "ملازم", "ملزمه", "الملزمه",
                "محاضرة", "المحاضرة", "محاضره", "المحاضره", "محاضرات", "رقم", "مادة", "المادة", "ماده", "الماده",
                "pdf", "بي", "دي", "اف", "تسجيل", "فيديو",
                
                # حروف الجر وكلمات الربط العراقية
                "مال", "مالت", "عن", "بخصوص", "حق",
                
                # التحيات
                "السلام", "عليكم", "هلو", "مرحبا", "شباب", "بنات", "اخوان", "يا"
            ]
            words = text.split()
            keywords = [w for w in words if w not in stop_words]
            
            aliases = {
                # 1. التحليل العددي (حسب قاعدة بياناتك: num_analysis)
                "عددي": "num_analysis",
                "العددي": "num_analysis",
                "تحليل": "num_analysis",
                "التحليل": "num_analysis",
                "نيومركال": "num_analysis",
                
                # 2. النظرية الاحتسابية (حسب قاعدة بياناتك: computation)
                "احتسابية": "computation",
                "الاحتسابية": "computation",
                "احتسابيه": "computation",
                "الاحتسابيه": "computation",
                "نظرية": "computation",
                "النظريه": "computation",
                
                # 3. برمجة شيئية (حسب قاعدة بياناتك: oop)
                "شيئية": "oop",
                "الشيئية": "oop",
                "شيئيه": "oop",
                "الشيئيه": "oop",
                "اوب": "oop",
                "oop": "oop",
                
                # 4. خوارزميات AI (افترضت أن المعرف هو: ai - تأكد منه)
                "خوارزميات": "ai",
                "الخوارزميات": "ai",
                "خوارزميه": "ai",
                "الخوارزميه": "ai",
                "ذكاء": "ai",
                "الذكاء": "ai",
                "ai": "ai",
                
                # 5. لغة بايثون (افترضت أن المعرف هو: python - تأكد منه)
                "بايثون": "python",
                "البايثون": "python",
                "بايثن": "python",
                "python": "python",
                "py": "python",
                
                # 6. اللغة العربية (افترضت أن المعرف هو: arabic - تأكد منه)
                "عربي": "arabic",
                "العربي": "arabic",
                "عربية": "arabic",
                "العربية": "arabic",
                "عربيه": "arabic",
                "العربيه": "arabic",
                
                # أرقام المحاضرات (لتطابق العناوين في الموقع)
                "1": "الأولى",
                "الاولى": "الأولى",
                "اولى": "الأولى",
                "2": "الثانية",
                "الثانية": "الثانية",
                "ثانية": "الثانية",
                "3": "الثالثة",
                "الثالثة": "الثالثة",
                "ثالثة": "الثالثة",
                "4": "الرابعة",
                "الرابعة": "الرابعة",
                "رابعة": "الرابعة",
                "5": "الخامسة",
                "الخامسة": "الخامسة",
                "خامسة": "الخامسة",
                "6": "السادسة",
                "السادسة": "السادسة",
                "سادسة": "السادسة",
                "7": "السابعة",
                "السابعة": "السابعة",
                "سابعة": "السابعة",
                "8": "الثامنة",
                "الثامنة": "الثامنة",
                "ثامنة": "الثامنة",
                "9": "التاسعة",
                "التاسعة": "التاسعة",
                "تاسعة": "التاسعة",
                "10": "العاشرة",
                "العاشرة": "العاشرة",
                "عاشرة": "العاشرة"
            }
            
            search_terms = []
            for kw in keywords:
                cleaned_kw = aliases.get(kw, kw)
                if cleaned_kw not in search_terms:
                    search_terms.append(cleaned_kw)
                    
            if not search_terms:
                send_telegram_reply(chat_id, "يرجى تحديد المادة التي تريدها بدقة، مثلاً: 'اريد ملف بايثون المحاضرة الاولى'")
            else:
                query = "SELECT title, file_url, subject_id FROM lectures WHERE "
                conditions = []
                params = []
                for term in search_terms:
                    conditions.append("(title ILIKE %s OR subject_id ILIKE %s)")
                    params.extend([f"%{term}%", f"%{term}%"])
                
                query += " AND ".join(conditions) + " ORDER BY id DESC LIMIT 3"
                rows = run(query, tuple(params))
                
                if rows:
                    msg = "🔍 <b>تفضل، وجدت هذه الملفات:</b>\n\n"
                    for r in rows:
                        msg += f"▪️ {r['subject_id']}: <a href='{r['file_url']}'>{r['title']}</a>\n"
                    msg += "\n🌐 <b>تصفح جميع المواد والشروحات على:</b>\n<a href='https://study-assistant-ivory-two.vercel.app'>منصة مساعد دراسي</a>"
                else:
                    joined_terms = ' '.join(search_terms)
                    msg = f"لم أجد أي ملف يطابق ( {joined_terms} ). تأكد من كتابة اسم المادة ورقم المحاضرة بشكل صحيح."
                
                send_telegram_reply(chat_id, msg)

    # هذا السطر هو الذي تسبب بالخطأ لأنه كان مفقوداً أو موضوعاً بمسافة خاطئة
    return jsonify({"ok": True})