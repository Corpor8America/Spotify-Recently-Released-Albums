import os
import secrets
import threading
from datetime import datetime, timezone

from flask import Flask, redirect, request, url_for, render_template, jsonify, session
from werkzeug.middleware.proxy_fix import ProxyFix

BUILD_TIME = "2026-07-29"

import spotify_core as core


def cfg():
    return core.load_config()


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = cfg()["flask_secret_key"]


def public_base_url():
    explicit = (cfg().get("public_base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return request.host_url.rstrip("/")


def redirect_uri():
    return f"{public_base_url()}/callback"


def _get_creds():
    c = cfg()
    return c["spotify_client_id"], c["spotify_client_secret"]


# --- Settings / first-run setup ---------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        existing = cfg()
        c = {
            "spotify_client_id": (request.form.get("spotify_client_id") or existing["spotify_client_id"]).strip(),
            "spotify_client_secret": (request.form.get("spotify_client_secret") or existing["spotify_client_secret"]).strip(),
            "spotify_playlist_id": request.form.get("spotify_playlist_id", "").strip(),
            "interval_days": int(request.form.get("interval_days", existing["interval_days"])),
            "min_request_interval": float(request.form.get("min_request_interval", existing["min_request_interval"])),
            "days_lookback": int(request.form.get("days_lookback", existing["days_lookback"])),
            "cron_schedule": request.form.get("cron_schedule", existing["cron_schedule"]).strip(),
            "public_base_url": request.form.get("public_base_url", existing["public_base_url"]).rstrip("/"),
        }
        core.save_config(c)
        core.log("Settings saved.")
        return redirect(url_for("dashboard"))
    return render_template("settings.html", config=cfg(), build_time=BUILD_TIME,
                           effective_public_base_url=public_base_url(),
                           connected=core.is_connected())


@app.route("/create_playlist", methods=["POST"])
def create_playlist():
    c = cfg()
    client_id, client_secret = _get_creds()
    refresh_token = core.load_refresh_token()
    if not all([client_id, client_secret, refresh_token]):
        return "Not connected to Spotify", 400

    name = (request.form.get("playlist_name") or "Recently Released Albums").strip()
    token = core.get_access_token(client_id, client_secret, refresh_token)
    try:
        playlist_id = core.create_playlist(token, name)
    except Exception as e:
        core.log(f"Playlist creation failed: {e}")
        return f"Playlist creation failed: {e}", 500

    c["spotify_playlist_id"] = playlist_id
    core.save_config(c)
    core.log(f"Created playlist {name!r} ({playlist_id}); set as sync target.")
    return redirect(url_for("settings"))


# --- Dashboard ---------------------------------------------------------------

def format_rate_limit_until(ts):
    now = datetime.now(timezone.utc)
    until = datetime.fromtimestamp(ts, tz=timezone.utc)
    remaining = max(0, int(ts - now.timestamp()))
    if remaining >= 3600:
        relative = f"about {(remaining + 3599) // 3600}h"
    elif remaining >= 60:
        relative = f"about {(remaining + 59) // 60}m"
    else:
        relative = f"{remaining}s"
    return f"{until.strftime('%Y-%m-%d %I:%M:%S %p UTC')} ({relative})"


@app.route("/")
def dashboard():
    if not core.is_configured():
        return redirect(url_for("settings"))

    state = core.load_state()
    if core.clear_expired_rate_limits(state):
        core.save_state(state)
    c = cfg()
    report_albums = core.get_report_albums(state, c["days_lookback"])
    excluded_albums = core.get_excluded_albums(state)

    return render_template(
        "dashboard.html",
        connected=core.is_connected(),
        playlist_id=c["spotify_playlist_id"],
        report_albums=report_albums,
        excluded_albums=excluded_albums,
        in_progress=state.get("in_progress"),
        rate_limits={
            cat: format_rate_limit_until(ts)
            for cat, ts in state.get("rate_limits", {}).items()
        },
        artists_tracked=len(state.get("artists", {})),
        known_albums_count=len(state.get("known_albums", {})),
        logs=core.get_recent_logs()[-80:],
        scan_running=core.run_lock.locked(),
        reorder_running=core.reorder_lock.locked(),
        now=datetime.now(timezone.utc),
        build_time=BUILD_TIME,
    )


# --- OAuth -------------------------------------------------------------------

@app.route("/login")
def login():
    client_id, _ = _get_creds()
    if not client_id:
        return "Spotify Client ID not configured. Go to /settings first.", 500
    csrf_state = secrets.token_urlsafe(16)
    session["oauth_state"] = csrf_state
    return redirect(core.get_auth_url(client_id, redirect_uri(), csrf_state))


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return f"Spotify authorization failed: {error}", 400

    if request.args.get("state") != session.get("oauth_state"):
        return "State mismatch -- possible CSRF, please try /login again.", 400

    code = request.args.get("code")
    if not code:
        return "Missing authorization code.", 400

    client_id, client_secret = _get_creds()
    token_data = core.exchange_code_for_token(client_id, client_secret, code, redirect_uri())
    core.save_refresh_token(token_data["refresh_token"])
    core.log("Connected to Spotify via OAuth.")
    return redirect(url_for("dashboard"))


# --- Scan actions ------------------------------------------------------------

@app.route("/run", methods=["POST"])
def run_now():
    c = cfg()
    threading.Thread(target=core.run_scan, kwargs={
        "days": c["days_lookback"],
        "interval_days": c["interval_days"],
        "min_request_interval": c["min_request_interval"],
    }, daemon=True).start()
    return redirect(url_for("dashboard"))


@app.route("/cancel", methods=["POST"])
def cancel_scan():
    core.cancel_scan()
    return redirect(url_for("dashboard"))


@app.route("/reorder", methods=["POST"])
def reorder_playlist():
    threading.Thread(target=_do_reorder, daemon=True).start()
    return redirect(url_for("dashboard"))


def _do_reorder():
    if not core.reorder_lock.acquire(blocking=False):
        core.log("Reorder already in progress.")
        return
    try:
        c = cfg()
        core.rate_limiter.min_interval_seconds = c["min_request_interval"]
        client_id, client_secret = _get_creds()
        refresh_token = core.load_refresh_token()
        if not all([client_id, client_secret, refresh_token]):
            core.log("Cannot reorder -- not connected.")
            return
        token = core.get_access_token(client_id, client_secret, refresh_token)
        state = core.load_state()
        playlist_id = c["spotify_playlist_id"]
        core.reorder_playlist(token, state, playlist_id)
    finally:
        core.reorder_lock.release()


# --- Album overrides ---------------------------------------------------------

@app.route("/albums/<album_id>/override", methods=["POST"])
def set_override(album_id):
    value = request.form.get("value")
    state = core.load_state()
    album = state.get("known_albums", {}).get(album_id)
    if not album:
        return "Unknown album", 404
    if value == "true":
        album["manual_override"] = True
    elif value == "false":
        album["manual_override"] = False
    else:
        album["manual_override"] = None
    core.save_state(state)
    return redirect(url_for("dashboard"))


# --- Status API --------------------------------------------------------------

@app.route("/status")
def status():
    state = core.load_state()
    if core.clear_expired_rate_limits(state):
        core.save_state(state)
    return jsonify({
        "connected": core.is_connected(),
        "scan_running": core.run_lock.locked(),
        "in_progress": state.get("in_progress"),
        "rate_limits": state.get("rate_limits", {}),
        "known_albums_count": len(state.get("known_albums", {})),
        "logs": core.get_recent_logs()[-40:],
    })


# --- Scheduler ---------------------------------------------------------------

def _start_scheduler():
    if os.environ.get("RUN_SCHEDULER", "1") != "1":
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    c = cfg()
    cron_expr = c["cron_schedule"]
    try:
        minute, hour, day, month, dow = cron_expr.split()
    except ValueError:
        core.log(f"Invalid cron schedule {cron_expr!r}; using default 0 6 * * *")
        minute, hour, day, month, dow = "0", "6", "*", "*", "*"

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: core.run_scan(
            days=cfg()["days_lookback"],
            interval_days=cfg()["interval_days"],
            min_request_interval=cfg()["min_request_interval"],
        ),
        trigger=CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow),
    )
    scheduler.start()
    core.log(f"Scheduler started (cron: {cron_expr} UTC)")


_start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=False)
