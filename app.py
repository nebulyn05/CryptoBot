from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
import os
import json
import re
import asyncio
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError
import threading

app = Flask(__name__)
app.secret_key = 'your_secret_key'

# Telegram API
API_ID = '29469765'
API_HASH = '9592a56b2eb5ff6eb2e92ee0e6ef9f14'

SESSION_DIR = 'sessions/'
os.makedirs(SESSION_DIR, exist_ok=True)

ACHILLES_BOT_USERNAME = 'achilles_trojanbot'

# Multi-user storage
clients = {}
listeners = {}

# Global event loop
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()


# -----------------------------
# Helpers
# -----------------------------
def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def get_client(phone):
    if phone not in clients:
        session_path = os.path.join(SESSION_DIR, f"{phone}.session")
        clients[phone] = TelegramClient(session_path, API_ID, API_HASH, loop=loop)
    return clients[phone]


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

    return client


async def verify_otp(phone, otp):
    client = get_client(phone)
    await client.connect()

    try:
        await client.sign_in(phone, otp, phone_code_hash=session.get('phone_code_hash'))
        return client
    except SessionPasswordNeededError:
        return "password_required"


async def verify_password(phone, password):
    client = get_client(phone)
    await client.connect()

    try:
        await client.sign_in(password=password)
        return client
    except PasswordHashInvalidError:
        return "invalid_password"


# -----------------------------
# Fetch groups
# -----------------------------
async def fetch_groups_async(client):
    await client.connect()  # ✅ ADD THIS

    if not await client.is_user_authorized():
        raise Exception("User not authorized")

    result = []
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            result.append({"name": d.name, "id": d.id})

    return result

# -----------------------------
# Trading
# -----------------------------
async def trade(client, signal):
    """Send trade command with safe token fallback."""
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("Not authorized")
            return

        await client.send_message(ACHILLES_BOT_USERNAME, "/start")
        await asyncio.sleep(2)

        token = signal.get("token", "UNKNOWN")
        ca = signal.get("contract_address")

        # -------------------------
        # Smart trade message
        # -------------------------
        if token != "UNKNOWN" and token:
            msg = f"Buy {token} at {ca}"
        else:
            msg = f"Buy {ca}"  # fallback mode if token unknown

        await client.send_message(ACHILLES_BOT_USERNAME, msg)
        print(f"Trade sent: {msg}")

    except Exception as e:
        print("Trade error:", e)

# -----------------------------
# Listener (per user)
# -----------------------------
async def listen_for_signals(client, phone_number):
    """Listen to multiple groups/channels for a specific user."""
    config_file = os.path.join(SESSION_DIR, f"{phone_number}_config.json")
    try:
        with open(config_file, "r") as f:
            config_data = json.load(f)
    except FileNotFoundError:
        print(f"❌ Config file not found for {phone_number}. Please select groups first.")
        return

    selected_chats = config_data.get("selected_chats", [])
    if not selected_chats:
        print(f"❌ No chats selected for {phone_number}.")
        return

    @client.on(events.NewMessage(chats=selected_chats))
    async def handler(event):
        message_text = event.message.text
        signal = extract_token_signal(message_text)
        if signal:
            print(f"{phone_number} signal: {signal}")
            await trade(client, signal)

    print(f"✅ {phone_number} is listening on {len(selected_chats)} chats...")

    if not await client.is_user_authorized():
        print(f"❌ {phone_number} not authorized")
        return

    await client.run_until_disconnected()

def start_listener(phone_number):
    """Start Telegram listener in a separate thread but use the global loop."""
    client = get_client(phone_number)

    def runner():
        # This runs on the global loop
        asyncio.run_coroutine_threadsafe(listen_for_signals(client, phone_number), loop)

    t = threading.Thread(target=runner, daemon=True)
    t.start()

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

    return redirect(url_for('dashboard'))


@app.route('/otp', methods=['GET', 'POST'])
def otp():
    if request.method == 'POST':
        phone = session.get('phone')
        otp = request.form['otp']

        result = run_async(verify_otp(phone, otp))

        if result == "password_required":
            return redirect(url_for('password'))

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

        return redirect(url_for('dashboard'))

    return render_template('password.html')


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/fetch_groups', methods=['GET', 'POST'])
def fetch_groups():
    phone = session.get('phone')
    client = get_client(phone)

    groups = run_async(fetch_groups_async(client))  # ✅ FIXED

    if request.method == 'POST':
        selected_ids = request.form.getlist('selected_groups')
        config_file = os.path.join(SESSION_DIR, f"{phone}_config.json")
        config_data = {"selected_chats": [int(id) for id in selected_ids]}
        with open(config_file, "w") as f:
            json.dump(config_data, f, indent=4)
        flash("✅ Selected groups/channels saved!")
        return redirect(url_for('dashboard'))

    return render_template('fetch_groups.html', groups_and_channels=groups)

@app.route('/listen', methods=['POST'])
def listen():
    phone_number = session.get('phone')
    if not phone_number:
        return jsonify({"error": "No user logged in"}), 400

    # Start listener in a separate thread
    threading.Thread(target=start_listener, args=(phone_number,), daemon=True).start()

    return jsonify({"status": f"Listening for signals for {phone_number}..."})

# -----------------------------
# Run
# -----------------------------
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3000, debug=True)