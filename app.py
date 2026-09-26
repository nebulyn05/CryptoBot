from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import os
import json
import re
import asyncio
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError
from telethon.sessions import StringSession
import threading
from management import (init_management, sync_user, sync_groups, record_signal, get_management_snapshot, get_management_overview, sync_available_groups, set_group_monitoring, remove_group, set_user_bot, add_user_bot, update_user_bot, delete_user_bot, available_bots, set_user_active, delete_user, admin_set_group_monitoring, admin_delete_group, admin_update_bot, admin_delete_bot, set_display_name, get_signal_page, get_management_analytics, get_telegram_session, save_telegram_session, get_monitored_chat_ids, get_enabled_bots, get_users_with_telegram_sessions)

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('SECRET_KEY environment variable is required')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'true').lower() == 'true', SESSION_COOKIE_SAMESITE='Lax')

# Telegram API
API_ID = '29469765'
API_HASH = '9592a56b2eb5ff6eb2e92ee0e6ef9f14'

# Legacy filesystem path for per-user config files. Telegram auth sessions are stored in Postgres.
SESSION_DIR = os.getenv('SESSION_DIR', '/tmp/sessions')
os.makedirs(SESSION_DIR, exist_ok=True)

# Multi-user storage
clients = {}
listeners = {}

# Global event loop
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()

# Persistent management database. This records users, monitored groups and captured signals.
init_management()


# Restore monitoring after a Render restart using persisted Telegram sessions.
# The actual listener setup is scheduled after the helper functions are defined.
def _restore_monitors_after_startup():
    for phone in get_users_with_telegram_sessions():
        start_listener(phone)


# -----------------------------
# Helpers
# -----------------------------
def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def get_client(phone):
    if phone not in clients:
        sync_user(phone)
        session_string = get_telegram_session(phone)
        telegram_session = StringSession(session_string) if session_string else StringSession()
        clients[phone] = TelegramClient(telegram_session, API_ID, API_HASH, loop=loop)
    return clients[phone]


def persist_telegram_session(phone, client):
    try:
        session_string = client.session.save()
        save_telegram_session(phone, session_string)
    except Exception:
        app.logger.exception("Failed to persist Telegram session for %s", phone)


def get_config(phone):
    return os.path.join(SESSION_DIR, f"{phone}_config.json")


# -----------------------------
# Signal extractor
# -----------------------------
def extract_token_signal(message):
    """Extract token and contract address from message."""
    lines = [line.strip() for line in message.split("\n") if line.strip()]
    if not lines:
        return None

    # -------------------------
    # Contract Address (CA)
    # -------------------------
    ca_match = re.search(r"(?:CA[:\s]*)?(0x[a-fA-F0-9]{40}|[A-Za-z0-9]{30,})", message)
    contract_address = ca_match.group(1) if ca_match else None
    if not contract_address:
        return None

    # -------------------------
    # Token detection
    # -------------------------
    token = None
    dollar_match = re.search(r"\$([A-Za-z0-9_]{2,20})", message)
    if dollar_match:
        token = dollar_match.group(1)
    else:
        caps_match = re.search(r"\b[A-Z]{3,10}\b", message)
        if caps_match:
            token = caps_match.group(0)

    if not token:
        token = "UNKNOWN"

    # -------------------------
    # Optional link
    # -------------------------
    link_match = re.search(r"(https?://(?:gmgn\.ai|dexscreener\.com|x\.com)/[^\s]+)", message)
    token_link = link_match.group(1) if link_match else None

    return {"token": token, "contract_address": contract_address, "link": token_link}
    
# -----------------------------
# Telegram Auth
# -----------------------------
async def login_telegram(phone):
    client = get_client(phone)
    await client.connect()

    if not await client.is_user_authorized():
        sent = await client.send_code_request(phone)
        session['phone_code_hash'] = sent.phone_code_hash
        return "otp_required"

    persist_telegram_session(phone, client)
    return client


async def verify_otp(phone, otp):
    client = get_client(phone)
    await client.connect()

    try:
        await client.sign_in(phone, otp, phone_code_hash=session.get('phone_code_hash'))
        persist_telegram_session(phone, client)
        session.pop('phone_code_hash', None)
        return client
    except SessionPasswordNeededError:
        return "password_required"


async def verify_password(phone, password):
    client = get_client(phone)
    await client.connect()

    try:
        await client.sign_in(password=password)
        persist_telegram_session(phone, client)
        session.pop('phone_code_hash', None)
        return client
    except PasswordHashInvalidError:
        return "invalid_password"


# -----------------------------
# Fetch groups
# -----------------------------
async def fetch_groups_async(client):
    await client.connect()  # ✅ ADD THIS

    if not await client.is_user_authorized():
        raise PermissionError("Telegram authorization has expired or the session is missing")

    result = []
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            result.append({"name": d.name, "id": d.id})

    return result

# -----------------------------
# Trading
# -----------------------------
async def trade(client, signal, phone_number):
    """Forward a parsed signal to every enabled bot configured for this user."""
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print(f"{phone_number}: Telegram client is not authorized")
            return

        bots = get_enabled_bots(phone_number)
        if not bots:
            print(f"{phone_number}: no enabled bot profiles; signal recorded but not forwarded")
            return

        token = signal.get("token", "UNKNOWN")
        ca = signal.get("contract_address")
        if token != "UNKNOWN" and token:
            msg = f"Buy {token} at {ca}"
        else:
            msg = f"Buy {ca}"

        for bot_username in bots:
            try:
                await client.send_message(bot_username, "/start")
                await asyncio.sleep(2)
                await client.send_message(bot_username, msg)
                print(f"{phone_number}: signal forwarded to @{bot_username}: {msg}")
            except Exception:
                app.logger.exception("Failed to forward signal to @%s for %s", bot_username, phone_number)

    except Exception:
        app.logger.exception("Trade/forwarding error for %s", phone_number)


# -----------------------------
# Dynamic Listener (per user)
# -----------------------------
async def update_listener_async(phone_number):
    """Create, replace, or remove the Telegram event handler for the user's monitored groups."""
    client = get_client(phone_number)
    await client.connect()

    if not await client.is_user_authorized():
        print(f"{phone_number}: Telegram client is not authorized")
        return

    chat_ids = get_monitored_chat_ids(phone_number)
    existing = listeners.get(phone_number)
    if existing:
        old_handler = existing.get("handler")
        if old_handler:
            client.remove_event_handler(old_handler)
        listeners.pop(phone_number, None)

    if not chat_ids:
        print(f"{phone_number}: monitoring stopped; no groups selected")
        return

    async def handler(event):
        message_text = event.message.text or ""
        signal = extract_token_signal(message_text)
        if not signal:
            return

        print(f"{phone_number}: signal detected from {event.chat_id}: {signal}")
        record_signal(
            phone_number,
            signal,
            source_chat_id=str(event.chat_id) if event.chat_id is not None else None,
            source_chat_name=getattr(getattr(event, "chat", None), "title", None),
            raw_message=message_text,
        )
        await trade(client, signal, phone_number)

    client.add_event_handler(handler, events.NewMessage(chats=chat_ids))
    listeners[phone_number] = {"handler": handler, "chat_ids": chat_ids}
    print(f"{phone_number}: listening on {len(chat_ids)} monitored chats")


def start_listener(phone_number):
    """Start or refresh monitoring without blocking the Flask worker."""
    asyncio.run_coroutine_threadsafe(update_listener_async(phone_number), loop)


def stop_listener(phone_number):
    """Stop monitoring for a user immediately."""
    asyncio.run_coroutine_threadsafe(_stop_listener_async(phone_number), loop)


async def _stop_listener_async(phone_number):
    client = clients.get(phone_number)
    existing = listeners.pop(phone_number, None)
    if client and existing and existing.get("handler"):
        client.remove_event_handler(existing["handler"])
    print(f"{phone_number}: monitoring stopped")

# -----------------------------
# Routes
# -----------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login', methods=['POST'])
def login():
    phone = request.form['phone_number']
    session['phone'] = phone

    result = run_async(login_telegram(phone))

    if result == "otp_required":
        return redirect(url_for('otp'))

    start_listener(phone)
    return redirect(url_for('dashboard'))


@app.route('/otp', methods=['GET', 'POST'])
def otp():
    if request.method == 'POST':
        phone = session.get('phone')
        otp = request.form['otp']

        result = run_async(verify_otp(phone, otp))

        if result == "password_required":
            return redirect(url_for('password'))

        start_listener(phone)
        return redirect(url_for('dashboard'))

    return render_template('otp.html')


@app.route('/password', methods=['GET', 'POST'])
def password():
    if request.method == 'POST':
        phone = session.get('phone')
        pwd = request.form['password']

        result = run_async(verify_password(phone, pwd))

        if result == "invalid_password":
            flash("Wrong password")
            return redirect(url_for('password'))

        start_listener(phone)
        return redirect(url_for('dashboard'))

    return render_template('password.html')


@app.route('/dashboard')
def dashboard():
    phone = session.get('phone')
    if not phone:
        return redirect(url_for('index'))
    sync_user(phone)
    management = get_management_snapshot(phone)
    return render_template('dashboard.html', management=management)


def _management_user():
    return session.get('phone')


@app.route('/api/management/me')
def management_me():
    phone = _management_user()
    if not phone:
        return jsonify({'error': 'not_authenticated'}), 401
    sync_user(phone)
    return jsonify(get_management_snapshot(phone) or {})


@app.route('/api/management/groups', methods=['POST'])
def management_groups():
    phone = _management_user()
    if not phone:
        return jsonify({'error': 'not_authenticated'}), 401
    payload = request.get_json(silent=True) or {}
    chat_id = payload.get('chat_id')
    if chat_id is None:
        return jsonify({'error': 'chat_id is required'}), 400
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'chat_id must be numeric'}), 400
    monitored = bool(payload.get('monitored'))
    if not set_group_monitoring(phone, str(cid), monitored):
        return jsonify({'error': 'user not found'}), 404

    # Apply the monitoring change immediately for dashboard/API toggles.
    start_listener(phone) if monitored else start_listener(phone)

    config_file = get_config(phone)
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config_data = {}
    selected = {int(x) for x in config_data.get('selected_chats', [])}
    if monitored:
        selected.add(cid)
    else:
        selected.discard(cid)
    config_data['selected_chats'] = sorted(selected)
    with open(config_file, 'w') as f:
        json.dump(config_data, f, indent=4)
    return jsonify(get_management_snapshot(phone))


@app.route('/api/management/groups/<path:chat_id>', methods=['DELETE'])
def management_group_delete(chat_id):
    phone = _management_user()
    if not phone:
        return jsonify({'error': 'not_authenticated'}), 401
    if not remove_group(phone, chat_id):
        return jsonify({'error': 'group not found'}), 404
    start_listener(phone)
    config_file = get_config(phone)
    try:
        with open(config_file, 'r') as f:
            config_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config_data = {}
    try:
        selected = {int(x) for x in config_data.get('selected_chats', [])}
        selected.discard(int(chat_id))
        config_data['selected_chats'] = sorted(selected)
        with open(config_file, 'w') as f:
            json.dump(config_data, f, indent=4)
    except ValueError:
        pass
    return jsonify(get_management_snapshot(phone))


@app.route('/api/management/bot', methods=['POST'])
def management_bot():
    phone = _management_user()
    if not phone:
        return jsonify({'error': 'not_authenticated'}), 401
    payload = request.get_json(silent=True) or {}
    bot_username = (payload.get('bot_username') or '').strip().lstrip('@')
    if not bot_username:
        return jsonify({'error': 'bot username is required'}), 400
    if not set_user_bot(phone, bot_username):
        return jsonify({'error': 'invalid bot username'}), 400
    return jsonify(get_management_snapshot(phone))


@app.route('/api/management/bots', methods=['POST'])
def management_bots_add():
    phone = _management_user()
    if not phone:
        return jsonify({'error': 'not_authenticated'}), 401
    payload = request.get_json(silent=True) or {}
    ok, error = add_user_bot(
        phone,
        payload.get('bot_username'),
        payload.get('label')
    )
    if not ok:
        status = 409 if error == 'bot_already_exists' else 400
        return jsonify({'error': error}), status
    return jsonify(get_management_snapshot(phone))


@app.route('/api/management/bots/<int:bot_id>', methods=['PATCH'])
def management_bots_update(bot_id):
    phone = _management_user()
    if not phone:
        return jsonify({'error': 'not_authenticated'}), 401
    payload = request.get_json(silent=True) or {}
    ok, error = update_user_bot(
        phone,
        bot_id,
        label=payload.get('label'),
        enabled=payload.get('enabled') if 'enabled' in payload else None,
    )
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify(get_management_snapshot(phone))


@app.route('/api/management/bots/<int:bot_id>', methods=['DELETE'])
def management_bots_delete(bot_id):
    phone = _management_user()
    if not phone:
        return jsonify({'error': 'not_authenticated'}), 401
    ok, error = delete_user_bot(phone, bot_id)
    if not ok:
        return jsonify({'error': error}), 404
    return jsonify(get_management_snapshot(phone))


@app.route('/api/management/profile', methods=['PATCH'])
def management_profile():
    phone = _management_user()
    if not phone: return jsonify({'error': 'not_authenticated'}), 401
    payload = request.get_json(silent=True) or {}
    if not set_display_name(phone, payload.get('display_name')): return jsonify({'error': 'user not found'}), 404
    return jsonify(get_management_snapshot(phone))

@app.route('/api/management/signals')
def management_signals():
    phone = _management_user()
    if not phone: return jsonify({'error': 'not_authenticated'}), 401
    return jsonify(get_signal_page(phone, request.args.get('limit', 50), request.args.get('offset', 0), request.args.get('token')))

@app.route('/api/management/analytics')
def management_analytics():
    phone = _management_user()
    if not phone: return jsonify({'error': 'not_authenticated'}), 401
    return jsonify(get_management_analytics(phone))

def _admin_authorized():
    expected = os.getenv('MANAGEMENT_ADMIN_KEY', '')
    if not expected:
        return False
    supplied = request.headers.get('X-Management-Admin-Key', '')
    return supplied == expected or session.get('management_admin') is True


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        expected = os.getenv('MANAGEMENT_ADMIN_KEY', '')
        if expected and request.form.get('key', '') == expected:
            session['management_admin'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Invalid admin key.')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('management_admin', None)
    return redirect(url_for('admin_login'))


@app.route('/admin/dashboard')
def admin_dashboard():
    if not _admin_authorized():
        return redirect(url_for('admin_login'))
    return render_template('admin_dashboard.html', overview=get_management_overview())


@app.route('/admin/management')
def management_admin():
    if not _admin_authorized():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify(get_management_overview())


@app.route('/admin/api/users/<int:user_id>/status', methods=['POST'])
def admin_user_status(user_id):
    if not _admin_authorized(): return jsonify({'error':'unauthorized'}), 401
    payload=request.get_json(silent=True) or {}
    if not set_user_active(user_id, payload.get('active', True)): return jsonify({'error':'user_not_found'}),404
    return jsonify(get_management_overview())

@app.route('/admin/api/users/<int:user_id>', methods=['DELETE'])
def admin_user_delete(user_id):
    if not _admin_authorized(): return jsonify({'error':'unauthorized'}), 401
    if not delete_user(user_id): return jsonify({'error':'user_not_found'}),404
    return jsonify(get_management_overview())

@app.route('/admin/api/groups/<int:group_id>/status', methods=['POST'])
def admin_group_status(group_id):
    if not _admin_authorized(): return jsonify({'error':'unauthorized'}), 401
    payload=request.get_json(silent=True) or {}
    if not admin_set_group_monitoring(group_id, payload.get('monitored', True)): return jsonify({'error':'group_not_found'}),404
    return jsonify(get_management_overview())

@app.route('/admin/api/groups/<int:group_id>', methods=['DELETE'])
def admin_group_delete(group_id):
    if not _admin_authorized(): return jsonify({'error':'unauthorized'}), 401
    if not admin_delete_group(group_id): return jsonify({'error':'group_not_found'}),404
    return jsonify(get_management_overview())

@app.route('/admin/api/bots/<int:bot_id>', methods=['PATCH'])
def admin_bot_update(bot_id):
    if not _admin_authorized(): return jsonify({'error':'unauthorized'}), 401
    payload=request.get_json(silent=True) or {}
    if not admin_update_bot(bot_id, payload.get('label') if 'label' in payload else None, payload.get('enabled') if 'enabled' in payload else None):
        return jsonify({'error':'bot_not_found_or_invalid'}),400
    return jsonify(get_management_overview())

@app.route('/admin/api/bots/<int:bot_id>', methods=['DELETE'])
def admin_bot_delete(bot_id):
    if not _admin_authorized(): return jsonify({'error':'unauthorized'}), 401
    if not admin_delete_bot(bot_id): return jsonify({'error':'bot_not_found'}),404
    return jsonify(get_management_overview())

@app.route('/fetch_groups', methods=['GET', 'POST'])
def fetch_groups():
    phone = session.get('phone')
    if not phone:
        return redirect(url_for('index'))
    client = get_client(phone)

    try:
        groups = run_async(fetch_groups_async(client))
        persist_telegram_session(phone, client)
    except PermissionError:
        # The Flask session may still exist even when Telethon's Telegram
        # authorization has expired or its session file was lost.
        session.pop('phone_code_hash', None)
        flash("Your Telegram session has expired or is no longer available. Please sign in to Telegram again.")
        return redirect(url_for('index'))
    except Exception:
        app.logger.exception("Failed to fetch Telegram groups for %s", phone)
        flash("Telegram could not be reached while loading your groups. Please try again.")
        return redirect(url_for('dashboard'))

    sync_user(phone)
    sync_available_groups(phone, groups)

    if request.method == 'POST':
        selected_ids = request.form.getlist('selected_groups')
        config_file = os.path.join(SESSION_DIR, f"{phone}_config.json")
        config_data = {"selected_chats": [int(id) for id in selected_ids]}
        with open(config_file, "w") as f:
            json.dump(config_data, f, indent=4)
        sync_groups(phone, selected_ids)
        # Apply the new watch list immediately. Adding groups starts monitoring;
        # unchecking groups removes them from the listener immediately.
        start_listener(phone)
        flash("Selected groups/channels saved and monitoring updated!")
        return redirect(url_for('dashboard'))

    existing = (
        get_management_snapshot(phone) or {}
    ).get('groups', [])
    selected_ids = {
        str(g.get('chat_id'))
        for g in existing
        if g.get('monitored')
    }
    return render_template(
        'fetch_groups.html',
        groups_and_channels=groups,
        selected_groups=selected_ids,
    )


@app.route('/listen', methods=['POST'])
def listen():
    phone_number = session.get('phone')
    if not phone_number:
        return jsonify({"error": "No user logged in"}), 400

    # Start listener in a separate thread
    threading.Thread(target=start_listener, args=(phone_number,), daemon=True).start()

    return jsonify({"status": f"Listening for signals for {phone_number}..."})

# Restore any persisted monitors once all functions are defined.
try:
    _restore_monitors_after_startup()
except Exception:
    app.logger.exception("Failed to restore Telegram monitors at startup")

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3000, debug=True)
