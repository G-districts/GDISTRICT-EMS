
# app.py — G Schools EMS (Teacher Edition + Admin) — Cisco-first, RSS triggers, Drill Mode
import os, ssl, smtplib, socket, time, yaml, requests, sqlite3, random, threading
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify, session, Response
import socket
import random, time
from flask import Response
# ---------------- Gotify ----------------
def send_gotify(message: str, title: str = None, priority: int = 10):
    """
    Send a notification to Gotify server.
    Config must have CFG['gotify']['url'] and CFG['gotify']['token']
    """
    try:
        gotify_cfg = CFG.get("gotify", {})
        url = gotify_cfg.get("url", "").rstrip("/") + "/message"
        token = gotify_cfg.get("token", "")
        if not url or not token:
            print("[Gotify] URL or token not configured")
            return False
        payload = {
            "title": title or _service_name(),
            "message": message,
            "priority": priority
        }
        headers = {"X-Gotify-Key": token, "Content-Type": "application/json"}
        r = requests.post(url, json=payload, headers=headers, timeout=5)
        if r.status_code == 200:
            print("[Gotify] Sent successfully")
            return True
        print(f"[Gotify] Failed: HTTP {r.status_code} – {r.text}")
    except Exception as e:
        print(f"[Gotify] Exception: {e}")
    return False


def update_rss_token(a: str):
    """Increment RSS token for alert and update timestamp"""
    FEED_STATE[a] = (FEED_STATE.get(a, 0) + 1) % 10000
    print(f"[RSS] Updated {a} → {FEED_STATE[a]}")



def clockwise_udp_trigger(trigger_name: str, zone: str = "ALL"):
    """
    ClockWise integration.

    Supports two modes configured in CFG["clockwise"]:
      - mode: "udp" (default)  -> send UDP payload to ip:port
      - mode: "http"          -> send HTTP request to a URL template

    For HTTP mode, config example:

      clockwise:
        enabled: true
        mode: "http"
        http_url: "http://172.16.50.192:8090/trigger?channel={payload}"
        triggers:
          LOCKDOWN: "LOCKDOWN"
          HOLD: "HOLD"
          ...

    The template may use {payload} and {zone}.
    """
    cfg = CFG.get("clockwise", {})
    if not cfg.get("enabled", False):
        print("[ClockWise] Disabled in config")
        return

    mode = (cfg.get("mode") or "udp").lower().strip()

    action_map = cfg.get("triggers", {})
    base_payload = action_map.get(trigger_name.upper(), trigger_name.upper())

    zone_suffix_map = cfg.get("zone_suffix", {})
    suffix = zone_suffix_map.get(zone.upper(), "")
    payload = f"{base_payload}{suffix}"

    if mode == "http":
        http_url = cfg.get("http_url")
        if not http_url:
            print("[ClockWise-HTTP] http_url not configured")
            return
        try:
            # simple template replacement
            url = http_url.replace("{payload}", payload).replace("{zone}", zone)
            import urllib.request
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                _ = resp.read()
            print(f"[ClockWise-HTTP] GET {url}")
        except Exception as e:
            print(f"[ClockWise-HTTP] Failed: {e}")
        return

    # default: UDP mode
    udp_ip = cfg.get("ip", "172.16.50.191")
    udp_port = int(cfg.get("port", 8090))

    try:
        msg = payload.encode("ascii", errors="ignore")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(msg, (udp_ip, udp_port))
        sock.close()
        print(f"[ClockWise-UDP] Sent '{payload}' for '{trigger_name}' zone '{zone}' to {udp_ip}:{udp_port}")
    except Exception as e:
        print(f"[ClockWise-UDP] Failed: {e}")


# in-memory display state for LED panels
DISPLAY_STATE = {}

# last seen timestamp per display id
DISPLAY_LAST_SEEN = {}

# last alert for Chrome extension / API
LAST_ALERT = {"id": None, "mode": "IDLE", "action": None, "text": "", "timestamp": 0, "zone": "ALL"}

# acknowledgment log (in-memory)
ACK_LOG = []

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
DB_PATH = os.path.join(ROOT, "gschool_ems.db")

# ---- Load config ----
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

app = Flask(__name__)

@app.template_filter("datetimeformat")
def datetimeformat(value):
    """Format a UNIX timestamp into local time string."""
    try:
        import datetime
        dt = datetime.datetime.fromtimestamp(int(value))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value


app.secret_key = CFG["app"]["secret_key"]
HAS_SIO = False
socketio = None

# ---------------- DB helpers ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_alerts_table():
    """Ensure alerts_history table exists."""
    try:
        conn = db(); c = conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS alerts_history ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "mode TEXT,"
            "action TEXT,"
            "text TEXT,"
            "zone TEXT,"
            "started_at INTEGER,"
            "resolved_at INTEGER,"
            "resolved_by TEXT,"
            "total_acks INTEGER"
            ")"
        )
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[alerts_history] table init failed: {e}")



def ensure_drills_table():
    """Ensure scheduled_drills table exists."""
    try:
        conn = db(); c = conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_drills ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "label TEXT,"
            "mode TEXT,"
            "action TEXT,"
            "zone TEXT,"
            "run_at INTEGER,"
            "enabled INTEGER,"
            "last_run_at INTEGER,"
            "created_by TEXT,"
            "created_at INTEGER"
            ")"
        )
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[drills] table init failed: {e}")



def fire_scheduled_alert(mode: str, action: str, zone: str = "ALL"):
    """
    Fire an alert from the scheduler without requiring a logged-in teacher.
    Mirrors core of trigger_action but without form/session.
    """
    mode = (mode or "DRILL").upper().strip()
    action = (action or "").upper().strip()
    zone = (zone or "ALL").upper().strip()
    if action not in ALLOWED:
        print(f"[scheduler] invalid action {action}")
        return

    threads = []

    # Cisco broadcast
    try:
        threads.append(_fire_and_forget(cisco_broadcast, action))
    except Exception as e:
        print(f"[scheduler] Cisco failed: {e}")

    # PBX / Asterisk page
    try:
        threads.append(_fire_and_forget(page_group))
    except Exception as e:
        print(f"[scheduler] PBX failed: {e}")

    # RSS token update
    try:
        threads.append(_fire_and_forget(update_rss_token, action))
    except Exception as e:
        print(f"[scheduler] RSS failed: {e}")

    # Gotify push
    try:
        msg = f"{mode} {action} triggered by SCHEDULER"
        threads.append(_fire_and_forget(
            send_gotify,
            msg,
            f"{_service_name()} Alert: {action}"
        ))
    except Exception as e:
        print(f"[scheduler] Gotify error: {e}")

    # ClockWise UDP
    try:
        threads.append(_fire_and_forget(clockwise_udp_trigger, action, zone))
    except Exception as e:
        print(f"[scheduler] ClockWise error: {e}")

    # Web banner for dashboards
    broadcast_web_banner(action, mode)

    # Determine displays for this zone
    zones_cfg = CFG.get("zones", {})
    target_displays = []
    if zones_cfg:
        z_cfg = zones_cfg.get(zone) or zones_cfg.get(zone.upper())
        if z_cfg and isinstance(z_cfg, dict):
            target_displays = z_cfg.get("displays", [])
        if not target_displays:
            all_cfg = zones_cfg.get("ALL") or {}
            target_displays = all_cfg.get("displays", [])
    if not target_displays:
        target_displays = ["display-1", "display-2"]

    for display_id in target_displays:
        DISPLAY_STATE[display_id] = {"mode": "ALERT", "text": f"{mode} {action}"}

    # Update latest alert
    LAST_ALERT["mode"] = mode
    LAST_ALERT["action"] = action
    LAST_ALERT["text"] = f"{mode} {action}"
    LAST_ALERT["timestamp"] = int(time.time())
    LAST_ALERT["zone"] = zone
    LAST_ALERT["id"] = None

    # New alert -> clear previous acknowledgements
    ACK_LOG.clear()

    # Insert into alerts_history table
    try:
        ensure_alerts_table()
        conn = db(); c = conn.cursor()
        c.execute(
            "INSERT INTO alerts_history (mode, action, text, zone, started_at, resolved_at, resolved_by, total_acks) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, action, f"{mode} {action}", zone, int(time.time()), None, "SCHEDULER", 0),
        )
        LAST_ALERT["id"] = c.lastrowid
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[scheduler] alerts_history insert failed: {e}")


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS teachers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        name TEXT,
        room TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS attendance(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        student TEXT,
        status TEXT,
        ts INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS roster(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        teacher_id INTEGER,
        student TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

def seed_teacher():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM teachers")
    n = c.fetchone()[0]
    if n == 0:
        c.execute("INSERT INTO teachers(username, password, name, room) VALUES(?,?,?,?)",
                  ("GschoolEMS", "letmein", "GschoolEMS", "Room 101"))
        for s in ["Alex", "Bailey", "Chris", "Devon", "Emery", "Frankie"]:
            c.execute("INSERT INTO roster(teacher_id, student) VALUES(?,?)", (1, s))
        conn.commit()
    conn.close()

seed_teacher()

# ---------------- helpers ----------------
def check_admin_passcode(code: str) -> bool:
    return (code or "") == CFG["app"]["admin_passcode"]

def _fire_and_forget(func, *args, **kwargs):
    """Run a function in a background thread so channels can fire in parallel."""
    def wrapper():
        try:
            func(*args, **kwargs)
        except Exception as e:
            print(f"[Background error in {func.__name__}] {e}")
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return t


def _icon_bytes(action: str) -> bytes:
    icon_file = CFG["ui"]["icons"].get(action, "")
    if not icon_file:
        return b""
    path = os.path.join(ROOT, CFG["ui"]["icons_dir"], icon_file)
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return b""

def _brand_site() -> str:
    return CFG.get("branding",{}).get("site_name","Glenwood Academy")

def _service_name() -> str:
    return CFG.get("branding",{}).get("service_name","G Schools EMS")

def _public_url() -> str:
    return CFG.get("branding", {}).get("public_url", "http://172.16.50.191")

def default_copy(action: str):
    if action == "HOLD":
        return ("G Schools EMS - Action Alert! - Glenwood Academy - Emergency",
                "Hold",
                "HOLD in your Room or Area. Clear the hallways. Stay in your classrooms and do not release anyone until the HOLD is released.")
    if action == "SECURE":
        return ("G Schools EMS - Action Alert! - Glenwood Academy - Emergency",
                "Secure",
                "SECURE the perimeter and outside doors. Keep doors locked. Business as usual inside the classroom. Increase situational awareness.")
    if action == "SHELTER":
        return ("G Schools EMS - Action Alert! - Glenwood Academy - Emergency",
                "Shelter",
                "Move to your designated shelter. Follow the hazard-specific safety strategy. Account for students.")
    if action == "EVACUATE":
        return ("G Schools EMS - Action Alert! - Glenwood Academy - Emergency",
                "Evacuate",
                "Evacuate to the designated location. Bring roll sheets and go-bags. Account for students.")
    if action == "LOCKDOWN":
        return ("G Schools EMS - Action Alert! - Glenwood Academy - Emergency",
                "Lockdown",
                "LOCKS, LIGHTS, OUT OF SIGHT. Students: move away from sight and maintain silence. Teachers: lock classroom doors, lights out, move away from sight, maintain silence. Do not open the door.")
    return ("G Schools EMS - Action Alert! - Glenwood Academy - Emergency",
            "Emergency", "Follow your site procedures.")

# ---------------- email (Catapult look) ----------------
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import smtplib, ssl

def catapult_style_email_html(action: str, mode: str, details_text: str, directive_text: str) -> str:
    catapult_red = "#A00000"; drill_blue  = "#1B32A8"; directive_red = "#E00000"
    border_gray = "#E5E7EB"; label_gray  = "#6B7280"
    brand_title = _service_name(); site_name = _brand_site()

    drill_banner = ""
    if mode == "DRILL":
        drill_banner = f"""
        <tr>
          <td style="background:{drill_blue};color:#fff;padding:14px 18px;text-align:center;font-size:18px;font-weight:700;letter-spacing:.5px">
            THIS IS A DRILL!
          </td>
        </tr>"""

    red_banner = f"""
        <tr>
          <td style="background:{catapult_red};color:#fff;padding:12px 18px;text-align:center;font-size:18px;font-weight:700">
            New Action Alert Reported!
          </td>
        </tr>"""

    def row(label_left: str, value_right_html: str) -> str:
        return f"""
        <tr>
          <td style="width:35%;padding:12px 12px;color:{label_gray};font-size:12px;font-weight:700;border-bottom:1px solid {border_gray};text-transform:uppercase;letter-spacing:.5px">
            {label_left}
          </td>
          <td style="padding:12px 12px;border-bottom:1px solid {border_gray};color:#111827">
            {value_right_html}
          </td>
        </tr>"""

    action_cell = f"""
      <span style="display:inline-flex;align-items:center;gap:8px;">
        <img src="cid:action_icon" alt="" style="height:40px;width:40px;vertical-align:middle;border-radius:50%;border:0;"/>
        <span style="font-weight:600">{action.title()}</span>
      </span>
    """

    directive_block = f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
        <tr>
          <td style="width:35%;padding:16px 12px;vertical-align:top;background:{directive_red};color:#fff;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid {border_gray}">
            Directive:
          </td>
          <td style="padding:16px 12px;border-bottom:1px solid {border_gray};color:#111827;line-height:1.5">
            {directive_text}
          </td>
        </tr>
      </table>
    """

    details_row = f"""
      <tr>
        <td style="width:35%;padding:12px 12px;color:{label_gray};font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px">
          Details:
        </td>
        <td style="padding:12px 12px;color:#111827">
          {details_text or 'More details soon'}
        </td>
      </tr>"""

    html = f"""<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#ffffff;font-family:Arial,Helvetica,sans-serif;color:#111827">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:760px;margin:0 auto;border-collapse:collapse">
      <tr>
        <td style="padding:20px 24px">
          <div style="font-size:26px;font-weight:800;color:#111827;letter-spacing:.3px">
            {brand_title}
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:10px;border:0;border-collapse:collapse">
            {drill_banner}
            {red_banner}
          </table>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px;border-top:1px solid {border_gray};border-collapse:collapse">
            {row('Site:', site_name)}
            {row('Type:', 'Emergency')}
            {row('Action:', action_cell)}
          </table>
          {directive_block}
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:8px">
            {details_row}
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""
    return html

def send_email(mode: str, action: str, extra_details: str = ""):
    try:
        _, _, directive = default_copy(action)
        subject = f"{_service_name()} – Action Alert! – {_brand_site()} – Emergency"
        if mode == "DRILL":
            subject += " (DRILL)"
            details_text = "THIS IS A DRILL!\n\n" + (extra_details or "More details soon")
        else:
            details_text = (extra_details or "More details soon")

        html = catapult_style_email_html(action, mode, details_text, directive)
        recipients = list(set(CFG["branding"]["fixed_recipients"]))
        msg = MIMEMultipart("related")
        msg["From"] = f"{CFG['branding']['from_display']} <{CFG['email']['from_alias']}>"
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html, "html"))
        msg.attach(alt)
        icon = _icon_bytes(action)
        if icon:
            part = MIMEImage(icon); part.add_header("Content-ID", "<action_icon>")
            part.add_header("Content-Disposition", "inline", filename="icon.png")
            msg.attach(part)

        if CFG["email"].get("use_ssl"):
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(CFG["email"]["smtp_host"], CFG["email"]["smtp_port"], context=context) as server:
                server.login(CFG["email"]["username"], CFG["email"]["app_password"])
                server.sendmail(CFG["email"]["from_alias"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(CFG["email"]["smtp_host"], CFG["email"]["smtp_port"]) as server:
                if CFG["email"].get("use_tls", True):
                    server.starttls(context=ssl.create_default_context())
                server.login(CFG["email"]["username"], CFG["email"]["app_password"])
                server.sendmail(CFG["email"]["from_alias"], recipients, msg.as_string())
        print("[Email] Sent successfully")
    except Exception as e:
        print(f"[Email] Failed: {e}")

# ---------------- PBX ----------------
def ami_send(command: str) -> bool:
    try:
        a = CFG["asterisk"]
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(5)
        s.connect((a["ami_host"], a["ami_port"]))
        def send(line): s.sendall((line + "\r\n").encode("utf-8"))
        def drain():
            try: s.recv(4096)
            except: pass
        send("Action: Login"); send(f"Username: {a['ami_username']}"); send(f"Secret: {a['ami_secret']}"); send("")
        drain()
        for ln in command.split("\n"):
            if ln.strip(): send(ln)
        send(""); drain(); send("Action: Logoff"); send("")
        s.close(); return True
    except Exception as e:
        print(f"[PBX] AMI connection failed: {e}"); return False

def page_group():
    try:
        pg = CFG["asterisk"]["page_extension"]
        cmd = (
            "Action: Originate\n"
            f"Channel: Local/{pg}@from-internal\n"
            "Context: from-internal\n"
            f"Exten: {pg}\n"
            "Priority: 1\n"
            "Async: true"
        )
        ami_send(cmd)
    except Exception as e:
        print(f"[PBX] Failed to page group: {e}")

# ---------------- Cisco ----------------
def _push_phone(ip, action, auth):
    xml_url = f"{_public_url()}/xml/{action.lower()}"
    execute_xml = (
        "<CiscoIPPhoneExecute>\n"
        '  <ExecuteItem URL="Play:tone.raw"/>\n'
        f'  <ExecuteItem URL="{xml_url}"/>\n'
        "</CiscoIPPhoneExecute>"
    )
    try:
        r = requests.post(
            f"http://{ip}/CGI/Execute",
            auth=auth,
            data={"XML": execute_xml},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=7
        )
        if r.status_code == 200 and "CiscoIPPhoneError" not in r.text:
            print(f"[Cisco] OK {ip}"); return True, ip, None
        return False, ip, f"HTTP {r.status_code} – {r.text.strip()}"
    except requests.exceptions.RequestException as e:
        return False, ip, str(e)

from concurrent.futures import ThreadPoolExecutor, as_completed
def cisco_broadcast(action):
    try:
        cisco = CFG.get("cisco", {})
        if not cisco.get("enabled", True): print("[Cisco] Disabled"); return
        phones = cisco.get("phones", [])
        if not phones: print("[Cisco] No phones"); return
        auth = (cisco.get("username","admin"), cisco.get("password","admin"))
        print("[Cisco] Triggering phones first...")
        with ThreadPoolExecutor(max_workers=min(8,len(phones) or 1)) as ex:
            futs = [ex.submit(_push_phone, ip, action, auth) for ip in phones]
            for fut in as_completed(futs):
                ok, ip, err = fut.result()
                if not ok: print(f"[Cisco] push failed for {ip}: {err}")
    except Exception as e:
        print(f"[Cisco] broadcast failed: {e}")

# ---------------- ClockWise via RSS ----------------
ALERTS = ["HOLD","SECURE","SHELTER","EVACUATE","LOCKDOWN"]
ALLOWED = ALERTS
FEED_STATE = {a: 0 for a in ALERTS}

@app.get("/rss/<alert>.xml")
def rss_feed(alert):
    a = (alert or "").upper().strip()
    if a not in ALERTS:
        return "Invalid alert", 404

    guid = FEED_STATE.get(a, 0)
    now = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{a} Alert Feed</title>
    <link>{_public_url()}/rss/{a.lower()}.xml</link>
    <description>Auto-trigger feed for {a} alerts</description>
    <lastBuildDate>{now}</lastBuildDate>
    <item>
      <title>{a} Update</title>
      <description>Alert state token: {guid}</description>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{now}</pubDate>
    </item>
  </channel>
</rss>"""
    return Response(xml, 200, {"Content-Type": "application/rss+xml"})

# ---------------- Teacher auth/views ----------------
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","").strip()
        conn = db(); c = conn.cursor()
        c.execute("SELECT id, password, name FROM teachers WHERE username=?", (u,))
        row = c.fetchone(); conn.close()
        if row and row["password"] == p:
            session["teacher_id"] = row["id"]
            session["teacher_name"] = row["name"] or u
            return redirect(url_for("dashboard"))
        flash("Invalid credentials","error"); return redirect(url_for("login"))
    return render_template("login.html",
        branding={"service_name": _service_name(), "site_name": _brand_site()})

@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

def require_teacher(): return bool(session.get("teacher_id"))

@app.get("/dashboard")
def dashboard():
    if not require_teacher(): return redirect(url_for("login"))
    conn = db(); c = conn.cursor()
    c.execute("SELECT student FROM roster WHERE teacher_id=?", (session["teacher_id"],))
    roster = [r[0] for r in c.fetchall()]; conn.close()

    # Compute ack summary for current alert
    mode = LAST_ALERT.get("mode") or "IDLE"
    action = LAST_ALERT.get("action") or ""
    alert_ts = LAST_ALERT.get("timestamp") or 0
    current = []
    for row in ACK_LOG:
        if row.get("mode") == mode and row.get("action") == action and row.get("alert_ts") == alert_ts:
            current.append(row)
    ack_summary = {
        "mode": mode,
        "action": action,
        "alert_ts": alert_ts,
        "count": len(current),
        "acks": current,
    }

    return render_template("dashboard.html",
        branding={"service_name": _service_name(), "site_name": _brand_site()},
        actions=sorted(list(ALLOWED)),
        has_sio=HAS_SIO,
        default_mode="DRILL",
        ack_summary=ack_summary,
        last_alert=LAST_ALERT
    )

# ---------------- Admin ----------------
@app.get("/admin")
def admin_page():
    return render_template("admin.html",
        branding={"service_name": _service_name(), "site_name": _brand_site()},
        cfg=CFG)

@app.route("/clockwise/debug", methods=["GET", "POST"])
def clockwise_debug():
    """
    Debug endpoint to see exactly what ClockWise sends.
    """
    try:
        print("===== ClockWise DEBUG HIT =====")
        print("Method:", request.method)
        print("Path:", request.path)
        print("Args (query string):", dict(request.args))
        print("Form:", dict(request.form))
        print("Headers:", dict(request.headers))
        print("Raw body:", request.data)
        print("===== END ClockWise DEBUG =====")
    except Exception as e:
        print("[ClockWise-DEBUG] error:", e)
    return "OK", 200


@app.post("/admin")
def admin_save():
    code = request.form.get("passcode","")
    if not check_admin_passcode(code):
        flash("Invalid admin passcode","error"); return redirect(url_for("admin_page"))

    # Branding
    CFG["branding"]["service_name"] = request.form.get("service_name", CFG["branding"].get("service_name","G Schools EMS"))
    CFG["branding"]["site_name"] = request.form.get("site_name", CFG["branding"].get("site_name","Glenwood Academy"))
    CFG["branding"]["from_display"] = request.form.get("from_display", CFG["branding"].get("from_display","G-District Alerts"))
    recips = [ln.strip() for ln in (request.form.get("fixed_recipients","")).splitlines() if ln.strip()]
    if recips: CFG["branding"]["fixed_recipients"] = recips

    # Asterisk
    CFG["asterisk"]["ami_host"] = request.form.get("ami_host", CFG["asterisk"]["ami_host"])
    CFG["asterisk"]["ami_port"] = int(request.form.get("ami_port", CFG["asterisk"]["ami_port"]))
    CFG["asterisk"]["ami_username"] = request.form.get("ami_username", CFG["asterisk"]["ami_username"])
    CFG["asterisk"]["ami_secret"] = request.form.get("ami_secret", CFG["asterisk"]["ami_secret"])
    CFG["asterisk"]["page_extension"] = request.form.get("page_extension", CFG["asterisk"]["page_extension"])

    # Cisco
    CFG.setdefault("cisco", {})
    CFG["cisco"]["enabled"] = (request.form.get("cisco_enabled") == "on")
    CFG["cisco"]["username"] = request.form.get("cisco_username", CFG["cisco"].get("username","admin"))
    CFG["cisco"]["password"] = request.form.get("cisco_password", CFG["cisco"].get("password","admin"))
    phones_txt = request.form.get("cisco_phones","")
    CFG["cisco"]["phones"] = [ln.strip() for ln in phones_txt.splitlines() if ln.strip()]


    # ClockWise / ANETD
    CFG.setdefault("clockwise", {})
    CFG["clockwise"]["enabled"] = (request.form.get("clockwise_enabled") == "on")
    CFG["clockwise"]["ip"] = request.form.get("clockwise_ip", CFG["clockwise"].get("ip", "172.16.50.191"))
    CFG["clockwise"]["port"] = int(request.form.get("clockwise_port", CFG["clockwise"].get("port", 8090)))

    trig = CFG["clockwise"].get("triggers", {})
    for act in ALLOWED:
        field = f"clockwise_{act}"
        if field in request.form:
            trig[act] = request.form.get(field, trig.get(act, act))
    CFG["clockwise"]["triggers"] = trig

    # Save
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(CFG, f, sort_keys=False)
    flash("Settings saved.","ok"); return redirect(url_for("admin_page"))


@app.post("/display/send")
def display_send():
    """Send a one-time custom message to a display from the dashboard UI."""
    code = request.form.get("passcode","")
    if not check_admin_passcode(code):
        flash("Invalid admin passcode for display message","error")
        return redirect(url_for("dashboard"))

    display_id = (request.form.get("display_id") or "display-1").strip()
    msg = (request.form.get("message") or "").strip()
    if not msg:
        flash("Message cannot be empty","error")
        return redirect(url_for("dashboard"))

    DISPLAY_STATE[display_id] = {"mode": "MESSAGE", "text": msg[:64]}
    flash(f"Sent display message to {display_id}","ok")
    return redirect(url_for("dashboard"))



@app.get("/app")
def pwa_app():
    """Redirect /app to the PWA static entry."""
    return redirect(url_for("static", filename="app/index.html"))

# ---------------- Legacy trigger page for quick tests ----------------
@app.get("/trigger")
def trigger_page():
    return render_template("trigger.html",
        branding={"service_name": _service_name(), "site_name": _brand_site()},
        actions=sorted(list(ALLOWED)))

def broadcast_web_banner(action, mode):
    if HAS_SIO and socketio:
        socketio.emit("alert", {"action": action, "mode": mode, "ts": int(time.time())})

@app.post("/trigger")
def trigger_action():
    # Accepts from dashboard & manual trigger forms
    action = (request.form.get("action") or "").upper().strip()
    mode = (request.form.get("mode") or "DRILL").upper().strip()
    zone = (request.form.get("zone") or "ALL").upper().strip()
    if action not in ALLOWED:
        flash("Invalid action", "error")
        return redirect(url_for("dashboard"))
    if not require_teacher():
        flash("Login required", "error")
        return redirect(url_for("login"))

    threads = []

    # Cisco broadcast
    try:
        threads.append(_fire_and_forget(cisco_broadcast, action))
    except Exception as e:
        print(f"[Error] Cisco failed: {e}")

    # PBX / Asterisk page
    try:
        threads.append(_fire_and_forget(page_group))
    except Exception as e:
        print(f"[Error] PBX failed: {e}")

    # RSS token update
    try:
        threads.append(_fire_and_forget(update_rss_token, action))
    except Exception as e:
        print(f"[Error] RSS failed: {e}")

    # Email broadcast
    try:
        threads.append(_fire_and_forget(send_email, mode, action))
    except Exception as e:
        print(f"[Error] Email failed: {e}")

    # Gotify push
    try:
        msg = f"{mode} {action} triggered by {session.get('teacher_name','Unknown')}"
        threads.append(_fire_and_forget(
            send_gotify,
            msg,
            f"{_service_name()} Alert: {action}"
        ))
    except Exception as e:
        print(f"[Gotify] Error: {e}")

    # ClockWise UDP
    try:
        threads.append(_fire_and_forget(clockwise_udp_trigger, action, zone))
    except Exception as e:
        print(f"[ClockWise-UDP] Error: {e}")

    # Web banner for dashboards
    broadcast_web_banner(action, mode)

    # Determine displays for this zone
    zones_cfg = CFG.get("zones", {})
    target_displays = []
    if zones_cfg:
        z_cfg = zones_cfg.get(zone) or zones_cfg.get(zone.upper())
        if z_cfg and isinstance(z_cfg, dict):
            target_displays = z_cfg.get("displays", [])
        if not target_displays:
            all_cfg = zones_cfg.get("ALL") or {}
            target_displays = all_cfg.get("displays", [])
    if not target_displays:
        target_displays = ["display-1", "display-2"]

    # Update display state so LED panels can show the alert text
    for display_id in target_displays:
        DISPLAY_STATE[display_id] = {"mode": "ALERT", "text": f"{mode} {action}"}

    # Update latest alert for API consumers (Chrome extension, PWA, etc.)
    LAST_ALERT["mode"] = mode
    LAST_ALERT["action"] = action
    LAST_ALERT["text"] = f"{mode} {action}"
    LAST_ALERT["timestamp"] = int(time.time())
    LAST_ALERT["zone"] = zone

    # Persist into alerts_history
    try:
        ensure_alerts_table()
        conn = db(); c = conn.cursor()
        c.execute(
            "INSERT INTO alerts_history (mode, action, text, zone, started_at, resolved_at, resolved_by, total_acks) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, action, f"{mode} {action}", zone, int(time.time()), None, str(session.get("teacher_id") or ""), None),
        )
        LAST_ALERT["id"] = c.lastrowid
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[alerts_history] insert failed: {e}")

    # New alert -> clear previous acknowledgements
    ACK_LOG.clear()

    flash(f"Sent {mode} {action} (all channels launched)", "ok")
    return redirect(url_for("dashboard"))


@app.get("/admin/displays")
def admin_displays():
    if not require_teacher():
        return redirect(url_for("login"))
    now_ts = int(time.time())
    displays = []
    for did, state in DISPLAY_STATE.items():
        last = DISPLAY_LAST_SEEN.get(did)
        displays.append({
            "id": did,
            "mode": state.get("mode"),
            "text": state.get("text"),
            "last_seen": last,
            "age": (now_ts - last) if last else None,
        })
    displays.sort(key=lambda d: d["id"])
    return render_template(
        "displays.html",
        branding={"service_name": _service_name(), "site_name": _brand_site()},
        displays=displays,
    )


@app.get("/api/display/<display_id>/text")
def api_display_text(display_id):
    """Return current state for a display (ESP32 polls this)."""
    state = DISPLAY_STATE.get(display_id)
    if not state:
        state = {"mode": "IDLE", "text": ""}
    try:
        DISPLAY_LAST_SEEN[display_id] = int(time.time())
    except Exception:
        pass
    return jsonify(state)


@app.post("/api/display/<display_id>/message")
def api_display_message(display_id):
    """Admin-only: push a one-time custom message to a display."""
    code = request.form.get("passcode", "")
    if not check_admin_passcode(code):
        return jsonify({"error": "invalid passcode"}), 403

    msg = (request.form.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "empty message"}), 400

    DISPLAY_STATE[display_id] = {"mode": "MESSAGE", "text": msg[:64]}
    return jsonify({"ok": True})


@app.get("/api/alerts/latest")
def api_latest_alert():
    """Return the last alert fired (for Chrome extension / dashboards)."""
    from flask import make_response
    resp = make_response(jsonify(LAST_ALERT))
    # Allow cross-origin access so Chrome extension can fetch this
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.post("/api/acknowledge")
def api_acknowledge():
    """Record that a station / client acknowledged the current alert."""
    from flask import make_response
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    station = (data.get("station") or "").strip() or "unknown"
    mode = LAST_ALERT.get("mode") or "IDLE"
    action = LAST_ALERT.get("action") or ""
    alert_ts = LAST_ALERT.get("timestamp") or 0
    now_ts = int(time.time())

    # Only log if something is actually active
    if mode != "IDLE" and alert_ts:
        ACK_LOG.append({
            "station": station,
            "mode": mode,
            "action": action,
            "alert_ts": alert_ts,
            "ack_ts": now_ts,
        })

    resp = make_response(jsonify({"ok": True}))
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.get("/api/acknowledge/summary")
def api_ack_summary():
    """Return acknowledgment summary for the current alert."""
    from flask import make_response
    mode = LAST_ALERT.get("mode") or "IDLE"
    action = LAST_ALERT.get("action") or ""
    alert_ts = LAST_ALERT.get("timestamp") or 0

    # Filter ACK_LOG for current alert
    current = []
    for row in ACK_LOG:
        if (
            row.get("mode") == mode
            and row.get("action") == action
            and row.get("alert_ts") == alert_ts
        ):
            current.append(row)

    summary = {
        "mode": mode,
        "action": action,
        "alert_ts": alert_ts,
        "count": len(current),
        "acks": current,
    }
    resp = make_response(jsonify(summary))
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.post("/resolve")
def resolve_current_alert():
    """Clear the current alert and reset all displays + ACKs."""
    if not require_teacher():
        return redirect(url_for("login"))

    alert_id = LAST_ALERT.get("id")

    # Update history row
    try:
        if alert_id:
            ensure_alerts_table()
            conn = db(); c = conn.cursor()
            c.execute(
                "UPDATE alerts_history SET resolved_at=?, resolved_by=?, total_acks=? WHERE id=?",
                (int(time.time()), str(session.get("teacher_id") or ""), len(ACK_LOG), alert_id),
            )
            conn.commit(); conn.close()
    except Exception as e:
        print(f"[alerts_history] resolve update failed: {e}")

    # Reset alert state
    LAST_ALERT["id"] = None
    LAST_ALERT["mode"] = "IDLE"
    LAST_ALERT["action"] = None
    LAST_ALERT["text"] = ""
    LAST_ALERT["timestamp"] = int(time.time())
    LAST_ALERT["zone"] = "ALL"

    # Reset displays
    for display_id in list(DISPLAY_STATE.keys()):
        DISPLAY_STATE[display_id] = {"mode": "IDLE", "text": ""}

    # Clear acknowledgements
    ACK_LOG.clear()

    # Clear web banners if any
    try:
        broadcast_web_banner("", "IDLE")
    except Exception as e:
        print(f"[Resolve] broadcast_web_banner error: {e}")

    flash("Current alert resolved and system reset to IDLE.", "ok")
    return redirect(url_for("dashboard"))




@app.get("/admin/config")
def admin_config():
    if not require_teacher():
        return redirect(url_for("login"))
    cw = CFG.get("clockwise", {})
    zones = CFG.get("zones", {}) or {}
    zones_yaml = yaml.safe_dump(zones, sort_keys=False) if zones else ""
    ctx = {
        "branding": {"service_name": _service_name(), "site_name": _brand_site()},
        "clockwise": cw,
        "zones_yaml": zones_yaml,
        "clockwise_triggers_yaml": yaml.safe_dump(cw.get("triggers", {}), sort_keys=False) if cw.get("triggers") else ""
    }
    return render_template("admin_config.html", **ctx)


@app.post("/admin/config")
def admin_config_post():
    if not require_teacher():
        return redirect(url_for("login"))
    global CFG
    cw = CFG.get("clockwise", {}) or {}
    cw["enabled"] = True if request.form.get("clockwise_enabled") == "on" else False
    cw["ip"] = (request.form.get("clockwise_ip") or cw.get("ip") or "").strip()
    try:
        cw["port"] = int(request.form.get("clockwise_port") or cw.get("port") or 8090)
    except Exception:
        cw["port"] = cw.get("port", 8090)

    # mode + HTTP URL template
    cw["mode"] = (request.form.get("clockwise_mode") or cw.get("mode") or "udp").strip().lower()
    cw["http_url"] = (request.form.get("clockwise_http_url") or cw.get("http_url") or "").strip()

    trig_txt = (request.form.get("clockwise_triggers") or "").strip()
    if trig_txt:
        try:
            tdata = yaml.safe_load(trig_txt)
            if isinstance(tdata, dict):
                cw["triggers"] = tdata
        except Exception as e:
            print(f"[admin_config] triggers parse failed: {e}")
    CFG["clockwise"] = cw

    zones_txt = (request.form.get("zones_yaml") or "").strip()
    if zones_txt:
        try:
            zdata = yaml.safe_load(zones_txt)
            if isinstance(zdata, dict):
                CFG["zones"] = zdata
        except Exception as e:
            print(f"[admin_config] zones parse failed: {e}")

    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(CFG, f, sort_keys=False)
        flash("Configuration saved.", "ok")
    except Exception as e:
        print(f"[admin_config] failed to write config: {e}")
        flash("Failed to write config file. Check server logs.", "error")

    return redirect(url_for("admin_config"))


@app.get("/alerts/history")
def alerts_history():
    if not require_teacher():
        return redirect(url_for("login"))
    ensure_alerts_table()
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, mode, action, text, zone, started_at, resolved_at, resolved_by, total_acks FROM alerts_history ORDER BY started_at DESC LIMIT 200")
    rows = c.fetchall()
    conn.close()
    return render_template(
        "alerts_history.html",
        branding={"service_name": _service_name(), "site_name": _brand_site()},
        alerts=rows,
    )


@app.get("/alerts/history.csv")
def alerts_history_csv():
    if not require_teacher():
        return redirect(url_for("login"))
    ensure_alerts_table()
    conn = db(); c = conn.cursor()
    c.execute("SELECT id, mode, action, text, zone, started_at, resolved_at, resolved_by, total_acks FROM alerts_history ORDER BY started_at DESC")
    rows = c.fetchall()
    conn.close()
    import io, csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","mode","action","text","zone","started_at","resolved_at","resolved_by","total_acks"])
    for r in rows:
        w.writerow([r["id"], r["mode"], r["action"], r["text"], r["zone"], r["started_at"], r["resolved_at"], r["resolved_by"], r["total_acks"]])
    csv_data = buf.getvalue()
    return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=alerts_history.csv"})



@app.get("/admin/drills")
def admin_drills():
    if not require_teacher():
        return redirect(url_for("login"))
    ensure_drills_table()
    conn = db(); c = conn.cursor()
    c.execute(
        "SELECT id, label, mode, action, zone, run_at, enabled, last_run_at, created_by, created_at "
        "FROM scheduled_drills ORDER BY run_at DESC"
    )
    rows = c.fetchall()
    conn.close()
    return render_template(
        "drills.html",
        branding={"service_name": _service_name(), "site_name": _brand_site()},
        drills=rows,
    )


@app.post("/admin/drills")
def admin_drills_post():
    if not require_teacher():
        return redirect(url_for("login"))
    ensure_drills_table()
    op = (request.form.get("op") or "").lower()
    try:
        import datetime
        conn = db(); c = conn.cursor()
        if op == "create":
            label = (request.form.get("label") or "").strip() or "Scheduled Drill"
            mode = (request.form.get("mode") or "DRILL").upper().strip()
            action = (request.form.get("action") or "").upper().strip()
            zone = (request.form.get("zone") or "ALL").upper().strip()
            run_at_raw = (request.form.get("run_at") or "").strip()
            run_ts = None
            if run_at_raw:
                try:
                    dt = datetime.datetime.strptime(run_at_raw, "%Y-%m-%dT%H:%M")
                    run_ts = int(dt.timestamp())
                except Exception as e:
                    print(f"[drills] parse run_at failed: {e}")
            enabled = 1 if request.form.get("enabled") == "on" else 0
            c.execute(
                "INSERT INTO scheduled_drills (label, mode, action, zone, run_at, enabled, last_run_at, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    label,
                    mode,
                    action,
                    zone,
                    run_ts,
                    enabled,
                    None,
                    str(session.get("teacher_id") or ""),
                    int(time.time()),
                ),
            )
            conn.commit()
        elif op == "delete":
            did = request.form.get("id")
            if did:
                c.execute("DELETE FROM scheduled_drills WHERE id=?", (did,))
                conn.commit()
        conn.close()
    except Exception as e:
        print(f"[drills] admin_drills_post error: {e}")
    return redirect(url_for("admin_drills"))


# Hosted XML for Cisco screens
@app.get("/xml/<action>")
def xml_display(action):
    a = (action or "").upper().strip()
    if a not in ALLOWED:
        return "Invalid", 400, {"Content-Type": "text/plain"}
    _, pretty, directive = default_copy(a)
    body = (
        '<?xml version="1.0"?>\n'
        "<CiscoIPPhoneText>\n"
        "  <Title>G School Alerts</Title>\n"
        f"  <Prompt>{a}</Prompt>\n"
        f"  <Text>{a} - {directive}</Text>\n"
        "</CiscoIPPhoneText>"
    )
    return body, 200, {"Content-Type": "text/xml"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT","5000"))
    app.run(host="0.0.0.0", port=port, debug=True)



def drill_scheduler_loop():
    """Background loop to fire scheduled drills."""
    while True:
        try:
            ensure_drills_table()
            now_ts = int(time.time())
            conn = db(); c = conn.cursor()
            c.execute(
                "SELECT id, label, mode, action, zone, run_at, enabled, last_run_at "
                "FROM scheduled_drills "
                "WHERE enabled=1 AND run_at IS NOT NULL AND run_at <= ? "
                "AND (last_run_at IS NULL OR last_run_at < run_at)",
                (now_ts,),
            )
            rows = c.fetchall()
            for r in rows:
                try:
                    print(f"[scheduler] firing drill id={r['id']} {r['mode']} {r['action']} zone={r['zone']}")
                    fire_scheduled_alert(r["mode"], r["action"], r["zone"] or "ALL")
                    c.execute(
                        "UPDATE scheduled_drills SET last_run_at=? WHERE id=?",
                        (int(time.time()), r["id"]),
                    )
                except Exception as e:
                    print(f"[scheduler] error firing drill {r['id']}: {e}")
            conn.commit(); conn.close()
        except Exception as e:
            print(f"[scheduler] loop error: {e}")
        time.sleep(30)


def start_background_threads():
    try:
        t = threading.Thread(target=drill_scheduler_loop, daemon=True)
        t.start()
        print("[scheduler] background drill scheduler started")
    except Exception as e:
        print(f"[scheduler] failed to start: {e}")


# start scheduler on import
start_background_threads()
