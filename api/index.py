import base64
import hmac
import html
import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import psycopg2
import psycopg2.extras
from flask import Flask, Response, jsonify, request, send_file

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # الموقع يعمل حتى لو لم تُثبّت المكتبة، فقط بدون إشعارات
    webpush = None

app = Flask(__name__)

# مفاتيح إشعارات الجوال (Web Push) — تُضبط في متغيرات Vercel
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

# أسماء المواد لنص الإشعار
SUBJECT_NAMES = {
    "oop": "برمجة شيئية",
    "num_analysis": "التحليل العددي",
    "computation": "النظرية الاحتسابية",
    "python": "لغة بايثون",
    "arabic": "اللغة العربية",
    "ai_algo": "خوارزميات AI",
}

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# رمز الأدمن مشتق من كلمة المرور (لا تُخزَّن كلمة المرور نفسها في المتصفح)
ADMIN_TOKEN = hmac.new(ADMIN_PASSWORD.encode(), b"study-admin", "sha256").hexdigest()

SITE_URL = "https://study-assistant-ivory-two.vercel.app"

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
ALTER TABLE lectures ADD COLUMN IF NOT EXISTS display_order INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS image_url TEXT;
CREATE TABLE IF NOT EXISTS replies (
    id SERIAL PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS message_images (
    id SERIAL PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    mime TEXT NOT NULL,
    data BYTEA NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id SERIAL PRIMARY KEY,
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_schema_ready = False

MSG_MAX = 10000  # الحد الأقصى لطول الرسالة الأسبوعية
MAX_IMAGES = 6  # أقصى عدد صور مع الرسالة الواحدة
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # أقصى حجم للصورة بعد الضغط
DATA_URL_RE = re.compile(r"^data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$")


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


# ---------- إرسال رسائل تيليجرام ----------
def send_telegram_notification(message):
    """يرسل رسالة إلى جروب الطلاب (TELEGRAM_GROUP_CHAT_ID)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID")

    # إذا نسينا المتغيرات في Vercel، الدالة تتوقف بصمت دون تخريب الموقع
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})

    try:
        urllib.request.urlopen(req, timeout=5)  # مهلة 5 ثوانٍ كي لا يتجمد الموقع
    except Exception as e:
        app.logger.error(f"فشل إرسال إشعار تيليجرام: {e}")


def send_telegram_reply(chat_id, message):
    """يرد على محادثة معينة (يستعمله الـ webhook)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        app.logger.error(f"فشل إرسال الرد: {e}")


def send_telegram_photos(images):
    """يرسل صور الرسالة (قائمة من (mime, bytes)) إلى جروب الطلاب كألبوم واحد."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID")
    if not token or not chat_id or not images:
        return

    boundary = "----studyassistant" + os.urandom(8).hex()
    parts = []

    def field(name, value):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )

    def file(name, mime, data):
        ext = mime.split("/")[1]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}.{ext}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n"
        )

    field("chat_id", chat_id)
    if len(images) == 1:
        method = "sendPhoto"
        file("photo", *images[0])
    else:
        method = "sendMediaGroup"
        field("media", json.dumps([{"type": "photo", "media": f"attach://p{i}"} for i in range(len(images))]))
        for i, (mime, data) in enumerate(images):
            file(f"p{i}", mime, data)
    payload = b"".join(parts) + f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=payload,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        app.logger.error(f"فشل إرسال الصور إلى تيليجرام: {e}")


# ---------- إشعارات الجوال (Web Push) ----------
def push_enabled():
    return bool(webpush and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)


def send_push_all(title, body, url="/", tag="study"):
    """يرسل إشعاراً ينبثق على جوالات كل الطلاب المشتركين، ويحذف الاشتراكات المنتهية."""
    if not push_enabled():
        return
    subs = run("SELECT id, endpoint, p256dh, auth FROM push_subscriptions")
    if not subs:
        return
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag}, ensure_ascii=False)
    dead = []

    def send_one(s):
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},  # قاموس جديد لكل طلب (المكتبة تعدّله)
                ttl=24 * 3600,
                timeout=10,
            )
        except WebPushException as e:
            # 404/410 = الطالب حذف التطبيق أو ألغى الإشعارات
            if e.response is not None and e.response.status_code in (404, 410):
                dead.append(s["id"])
            else:
                app.logger.error(f"فشل إرسال إشعار: {e}")
        except Exception as e:
            app.logger.error(f"فشل إرسال إشعار: {e}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(send_one, subs))
    if dead:
        run("DELETE FROM push_subscriptions WHERE id = ANY(%s)", (dead,), fetch=False)


def send_telegram_long(header, text, footer=""):
    """يقسّم الرسالة الطويلة إلى أجزاء (حد تيليجرام 4096) ويهرّب رموز HTML."""
    chunks = [text[i:i + 3000] for i in range(0, len(text), 3000)] or [""]
    for i, chunk in enumerate(chunks):
        part = html.escape(chunk)
        if i == 0:
            part = header + part
        if i == len(chunks) - 1:
            part += footer
        send_telegram_notification(part)


# ---------- الصفحة الرئيسية ----------
INDEX_FILE = Path(__file__).resolve().parent.parent / "public" / "index.html"


SOUND_FILE = INDEX_FILE.parent / "notify.mp4"


@app.get("/")
def home():
    return Response(INDEX_FILE.read_text(encoding="utf-8"), mimetype="text/html")


@app.get("/share")
def share_target():
    """صفحة استقبال المشاركة: عند مشاركة فيديو من يوتيوب إلى التطبيق يفتح هنا."""
    return home()


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


@app.get("/api/link-title")
def link_title():
    """يجلب عنوان فيديو يوتيوب تلقائياً (يوتيوب فقط، كي لا يُستعمل الخادم لجلب مواقع أخرى)."""
    url = str(request.args.get("url", "")).strip()[:500]
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        host = ""
    if not url.lower().startswith(("http://", "https://")) or host.lower() not in YOUTUBE_HOSTS:
        return jsonify({"title": None})
    oembed = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
    try:
        with urllib.request.urlopen(oembed, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return jsonify({"title": str(data.get("title") or "")[:300] or None})
    except Exception:
        return jsonify({"title": None})


@app.get("/notify.mp4")
def notify_sound():
    """صوت التنبيه (يستخدمه زر الجرس في الموقع)."""
    return send_file(SOUND_FILE, mimetype="audio/mp4", max_age=7 * 24 * 3600)


# ---------- ملفات التطبيق (PWA) ----------
PUBLIC_DIR = INDEX_FILE.parent


APK_FILE = PUBLIC_DIR / "app.apk"
APK_URL = os.environ.get("APK_URL", "").strip()


def apk_link():
    """رابط ملف APK إن وُجد: ملف public/app.apk أو رابط خارجي من APK_URL."""
    if APK_FILE.is_file():
        return "/app.apk"
    if APK_URL.startswith("https://"):
        return APK_URL
    return None


@app.get("/api/app-info")
def app_info():
    return jsonify({"apk": apk_link()})


@app.get("/app.apk")
def download_apk():
    if APK_FILE.is_file():
        return send_file(APK_FILE, mimetype="application/vnd.android.package-archive",
                         as_attachment=True, download_name="مساعد-دراسي.apk", max_age=3600)
    if APK_URL.startswith("https://"):
        return Response(status=302, headers={"Location": APK_URL})
    return jsonify({"error": "التطبيق غير متوفر بعد"}), 404


@app.get("/sw.js")
def service_worker():
    resp = send_file(PUBLIC_DIR / "sw.js", mimetype="application/javascript", max_age=0)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    return send_file(PUBLIC_DIR / "manifest.webmanifest", mimetype="application/manifest+json", max_age=3600)


@app.get("/icons/<name>")
def icons(name):
    if not re.fullmatch(r"[a-z0-9-]+\.png", name) or not (PUBLIC_DIR / "icons" / name).is_file():
        return jsonify({"error": "غير موجود"}), 404
    return send_file(PUBLIC_DIR / "icons" / name, mimetype="image/png", max_age=7 * 24 * 3600)


@app.get("/.well-known/assetlinks.json")
def asset_links():
    """يربط تطبيق الأندرويد (من PWABuilder) بالموقع كي يختفي شريط العنوان.
    يُضبط عبر متغيري ANDROID_PACKAGE و ANDROID_SHA256 في Vercel."""
    package = os.environ.get("ANDROID_PACKAGE", "").strip()
    prints = [p.strip() for p in os.environ.get("ANDROID_SHA256", "").split(",") if p.strip()]
    if not package or not prints:
        return jsonify([])
    return jsonify([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": package, "sha256_cert_fingerprints": prints},
    }])


# ---------- اشتراكات الإشعارات ----------
@app.get("/api/push/key")
def push_key():
    return jsonify({"key": VAPID_PUBLIC_KEY if push_enabled() else None})


@app.post("/api/push/subscribe")
def push_subscribe():
    d = body()
    endpoint = str(d.get("endpoint", "")).strip()
    keys = d.get("keys") or {}
    p256dh = str(keys.get("p256dh", "")).strip()
    auth = str(keys.get("auth", "")).strip()
    if not (endpoint.startswith("https://") and len(endpoint) < 1000 and 0 < len(p256dh) < 200 and 0 < len(auth) < 100):
        return jsonify({"error": "اشتراك غير صالح"}), 400
    run(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES (%s, %s, %s) "
        "ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth",
        (endpoint, p256dh, auth),
        fetch=False,
    )
    return jsonify({"ok": True})


@app.post("/api/push/unsubscribe")
def push_unsubscribe():
    endpoint = str(body().get("endpoint", "")).strip()
    if endpoint:
        run("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,), fetch=False)
    return jsonify({"ok": True})


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
    rows = run(
        "SELECT subject_id, MAX(id) AS max_id, "
        "COUNT(*) FILTER (WHERE LEFT(title, 7) = '[VIDEO]') AS videos, "
        "COUNT(*) FILTER (WHERE LEFT(title, 7) <> '[VIDEO]') AS files "
        "FROM lectures GROUP BY subject_id"
    )
    return jsonify({
        r["subject_id"]: {"max_id": r["max_id"], "files": r["files"], "videos": r["videos"]}
        for r in rows
    })


@app.get("/api/updates/latest")
def updates_latest():
    """آخر ملف وآخر تبليغ — يستخدمه الموقع كل دقيقة لتنبيه الطلاب بالجديد."""
    lec = run("SELECT id, subject_id, title FROM lectures ORDER BY id DESC LIMIT 1")
    msg = run("SELECT id, LEFT(text, 120) AS text FROM messages ORDER BY id DESC LIMIT 1")
    return jsonify({
        "lecture": lec[0] if lec else None,
        "message": msg[0] if msg else None,
    })


# ---------- المحاضرات ----------
@app.get("/api/lectures/<subject_id>")
def lectures_list(subject_id):
    # الترتيب أولاً حسب display_order ثم حسب id
    return jsonify(run(
        "SELECT * FROM lectures WHERE subject_id = %s ORDER BY display_order ASC, id ASC",
        (subject_id,),
    ))


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
    try:
        clean_title = re.sub(r"^\[(VIDEO|PDF)\]", "", title)
        msg = (
            f"📢 <b>تم رفع ملف جديد!</b>\n\n"
            f"📚 المادة: {html.escape(subject)}\n"
            f"📄 العنوان: {html.escape(clean_title)}\n\n"
            f"🌐 <b>لتحميل الملف والوصول لجميع المواد، ادخل إلى المنصة:</b>\n"
            f"<a href='{SITE_URL}'>{SITE_URL}</a>"
        )
        send_telegram_notification(msg)
    except Exception:
        pass

    # --- إشعار ينبثق على جوالات الطلاب ---
    try:
        is_video = title.startswith("[VIDEO]")
        name = SUBJECT_NAMES.get(subject, subject)
        send_push_all(
            f"{'🎬 شرح جديد' if is_video else '📚 ملف جديد'} - {name}",
            re.sub(r"^\[(VIDEO|PDF)\]", "", title),
            f"/#subject-{subject}",
            tag=f"lec-{subject}",
        )
    except Exception as e:
        app.logger.error(f"Push Error: {e}")

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


# ---------- ترتيب المحاضرات ----------
@app.put("/api/lectures/reorder")
def lectures_reorder():
    denied = admin_only()
    if denied:
        return denied

    # نتوقع استلام مصفوفة تحتوي على معرفات الملفات وترتيبها الجديد
    order_list = body().get("order", [])
    if not order_list:
        return jsonify({"error": "قائمة الترتيب فارغة"}), 400

    try:
        pairs = [(int(item["display_order"]), int(item["id"])) for item in order_list]
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "بيانات غير صالحة"}), 400

    # تحديث الترتيب كله في اتصال واحد (أسرع بكثير من اتصال لكل ملف)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(
                    cur, "UPDATE lectures SET display_order = %s WHERE id = %s", pairs
                )
    finally:
        conn.close()

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
    text = str(d.get("text", "")).strip()[:4000]
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


# ----------- الرسائل الأسبوعية -----------
@app.get("/api/messages")
def messages_list():
    # image_ids: الصور المرفوعة مع الرسالة، و image_url: رابط صورة قديم (إن وجد)
    return jsonify(run(
        "SELECT m.*, ARRAY(SELECT i.id FROM message_images i WHERE i.message_id = m.id ORDER BY i.id) AS image_ids "
        "FROM messages m ORDER BY m.id DESC"
    ))


@app.get("/api/images/<int:img_id>")
def message_image(img_id):
    rows = run("SELECT mime, data FROM message_images WHERE id = %s", (img_id,))
    if not rows:
        return jsonify({"error": "الصورة غير موجودة"}), 404
    resp = Response(bytes(rows[0]["data"]), mimetype=rows[0]["mime"])
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def parse_images(raw):
    """يحوّل الصور القادمة كـ data URL إلى (mime, bytes) ويتحقق من النوع والحجم."""
    images = []
    for item in (raw or [])[:MAX_IMAGES]:
        m = DATA_URL_RE.match(str(item))
        if not m:
            raise ValueError("صيغة صورة غير مدعومة")
        data = base64.b64decode(m.group(2))
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("الصورة كبيرة جداً")
        images.append((m.group(1), data))
    return images


@app.post("/api/messages/add")
def messages_add():
    denied = admin_only()
    if denied:
        return denied

    d = body()
    text = str(d.get("text", "")).strip()[:MSG_MAX]
    image_url = str(d.get("image_url", "")).strip()[:2000]
    if image_url and not image_url.lower().startswith(("http://", "https://")):
        image_url = ""
    try:
        images = parse_images(d.get("images"))
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    if not text and not image_url and not images:
        return jsonify({"error": "يجب إدخال نص أو صورة"}), 400

    when = str(d.get("date") or date.today().isoformat())[:30]

    # الرسالة وصورها تُحفظ معاً في معاملة واحدة
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (text, date, image_url) VALUES (%s, %s, %s) RETURNING id",
                    (text, when, image_url),
                )
                msg_id = cur.fetchone()[0]
                for mime, data in images:
                    cur.execute(
                        "INSERT INTO message_images (message_id, mime, data) VALUES (%s, %s, %s)",
                        (msg_id, mime, psycopg2.Binary(data)),
                    )
    finally:
        conn.close()

    # --- إرسال الإشعار لجروب تيليجرام (يُقسّم تلقائياً إذا كان طويلاً) ---
    try:
        footer = f"\n\n🖼️ <b>صورة مرفقة:</b> {html.escape(image_url)}" if image_url else ""
        if text or footer:
            send_telegram_long(
                f"📣 <b>تبليغ جديد للدفعة ({html.escape(when)})</b>\n\n",
                text,
                footer,
            )
        elif images:
            send_telegram_notification(f"📣 <b>تبليغ جديد للدفعة ({html.escape(when)})</b>")
        send_telegram_photos(images)
    except Exception as e:
        app.logger.error(f"Telegram Error: {e}")

    # --- إشعار ينبثق على جوالات الطلاب ---
    try:
        preview = " ".join(text.split())[:150] or "🖼️ صور جديدة"
        send_push_all("📣 تبليغ جديد للدفعة", preview, "/#batch", tag="batch")
    except Exception as e:
        app.logger.error(f"Push Error: {e}")

    return jsonify({"success": True})


@app.put("/api/messages/<int:msg_id>")
def edit_message(msg_id):
    denied = admin_only()
    if denied:
        return denied

    d = body()
    new_text = str(d.get("text", "")).strip()[:MSG_MAX]

    run("UPDATE messages SET text = %s WHERE id = %s", (new_text, msg_id), fetch=False)
    return jsonify({"success": True})


@app.delete("/api/messages/<int:m_id>")
def messages_delete(m_id):
    denied = admin_only()
    if denied:
        return denied
    run("DELETE FROM messages WHERE id = %s", (m_id,), fetch=False)
    return jsonify({"ok": True})


# ----------- بوت تيليجرام: استقبال الرسائل -----------
@app.post("/api/telegram/webhook")
def telegram_webhook():
    # حماية اختيارية: إذا ضُبط TELEGRAM_WEBHOOK_SECRET نقبل فقط الطلبات القادمة من تيليجرام
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if secret and not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret
    ):
        return jsonify({"error": "غير مصرح"}), 401

    update = request.get_json(silent=True) or {}

    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"]

        # 1. الرد على كلمات الجدول والتبليغات
        if any(keyword in text for keyword in ["باچر شعدنة", "شعدنا باجر", "الجدول", "تبليغ"]):
            rows = run("SELECT text, date FROM messages ORDER BY id DESC LIMIT 1")
            if rows:
                msg = (
                    f"📅 <b>تبليغ الأسبوع ({html.escape(str(rows[0]['date']))}):</b>\n\n"
                    f"{html.escape(rows[0]['text'] or '')}"
                )
            else:
                msg = "لا توجد تبليغات مسجلة حالياً."
            send_telegram_reply(chat_id, msg)

        # 2. الرد على طلب الملفات والمحاضرات (بحث ذكي)
        # يشترط وجود كلمة طلب + كلمة ملف/محاضرة كي لا يتدخل في المحادثات العادية
        elif (
            any(rq in text for rq in ["اريد", "ريد", "دز", "محتاج", "ممكن", "عنده", "عدكم", "وين", "ارسل"])
            and any(tgt in text for tgt in ["ملف", "ملفات", "محاضرة", "محاضره", "ملزمة", "ملزمه"])
        ):
            # كلمات يتجاهلها البوت كي لا تفسد البحث
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
                "السلام", "عليكم", "هلو", "مرحبا", "شباب", "بنات", "اخوان", "يا",
            ]
            words = text.split()
            keywords = [w for w in words if w not in stop_words]

            aliases = {
                # 1. التحليل العددي (num_analysis)
                "عددي": "num_analysis",
                "العددي": "num_analysis",
                "تحليل": "num_analysis",
                "التحليل": "num_analysis",
                "نيومركال": "num_analysis",

                # 2. النظرية الاحتسابية (computation)
                "احتسابية": "computation",
                "الاحتسابية": "computation",
                "احتسابيه": "computation",
                "الاحتسابيه": "computation",
                "نظرية": "computation",
                "النظريه": "computation",

                # 3. برمجة شيئية (oop)
                "شيئية": "oop",
                "الشيئية": "oop",
                "شيئيه": "oop",
                "الشيئيه": "oop",
                "اوب": "oop",
                "oop": "oop",

                # 4. خوارزميات AI (ai_algo)
                "خوارزميات": "ai_algo",
                "الخوارزميات": "ai_algo",
                "خوارزميه": "ai_algo",
                "الخوارزميه": "ai_algo",
                "ذكاء": "ai_algo",
                "الذكاء": "ai_algo",
                "ai": "ai_algo",

                # 5. لغة بايثون (python)
                "بايثون": "python",
                "البايثون": "python",
                "بايثن": "python",
                "python": "python",
                "py": "python",

                # 6. اللغة العربية (arabic)
                "عربي": "arabic",
                "العربي": "arabic",
                "عربية": "arabic",
                "العربية": "arabic",
                "عربيه": "arabic",
                "العربيه": "arabic",

                # استدعاء المادة كاملة
                "كاملة": "كاملة",
                "كامله": "كاملة",
                "كامل": "كاملة",
                "كلها": "كاملة",
                "الكل": "كاملة",

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
                "عاشرة": "العاشرة",
            }

            search_terms = []
            for kw in keywords:
                cleaned_kw = aliases.get(kw, kw)
                if cleaned_kw not in search_terms:
                    search_terms.append(cleaned_kw)

            if not search_terms:
                send_telegram_reply(
                    chat_id,
                    "يرجى تحديد المادة التي تريدها بدقة، مثلاً: 'اريد ملف بايثون المحاضرة الاولى'",
                )
            else:
                query = "SELECT title, file_url, subject_id FROM lectures WHERE "
                conditions = []
                params = []
                for term in search_terms:
                    # كلمة "كاملة" ليست شرط بحث، فلا نضيفها كشرط على العنوان
                    if term == "كاملة":
                        continue
                    conditions.append("(title ILIKE %s OR subject_id ILIKE %s)")
                    params.extend([f"%{term}%", f"%{term}%"])

                if not conditions:
                    send_telegram_reply(
                        chat_id,
                        "يرجى تحديد اسم المادة، مثلاً: 'اريد ملفات بايثون كاملة'",
                    )
                    return jsonify({"ok": True})

                has_full = "كاملة" in search_terms
                query += " AND ".join(conditions) + f" ORDER BY id DESC LIMIT {10 if has_full else 3}"
                rows = run(query, tuple(params))

                if rows:
                    msg = "🔍 <b>تفضل، وجدت هذه الملفات:</b>\n\n"
                    for r in rows:
                        clean_title = re.sub(r"^\[(VIDEO|PDF)\]", "", r["title"])
                        msg += (
                            f"▪️ {html.escape(r['subject_id'])}: "
                            f"<a href='{html.escape(r['file_url'], quote=True)}'>{html.escape(clean_title)}</a>\n"
                        )
                    msg += f"\n🌐 <b>تصفح جميع المواد والشروحات على:</b>\n<a href='{SITE_URL}'>منصة مساعد دراسي</a>"
                else:
                    joined_terms = html.escape(" ".join(t for t in search_terms if t != "كاملة"))
                    msg = f"لم أجد أي ملف يطابق ( {joined_terms} ). تأكد من كتابة اسم المادة ورقم المحاضرة بشكل صحيح."

                send_telegram_reply(chat_id, msg)

    return jsonify({"ok": True})