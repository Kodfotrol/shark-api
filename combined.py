import os
import logging
import requests
import time
import threading
import sqlite3
import base64
import uuid
from pathlib import Path
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ---------- НАСТРОЙКИ ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8761774819:AAFYhj0Uuo-wyfnLgiCZVZfc21xDCIYE8e8")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")  # убрал фейковый ключ
if not OPENROUTER_KEY:
    raise ValueError("OPENROUTER_API_KEY не задан в переменных окружения")
API_URL = f"https://api.telegram.org/bot{TOKEN}/"
PROJECTS_DIR = Path(__file__).parent / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect("ai_factory.db")
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT DEFAULT 'default',
        type TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        result TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()
init_db()

def generate_image(prompt):
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    data = {"model": "sourceful/riverflow-v2-fast", "messages": [{"role": "user", "content": prompt}], "modalities": ["image"]}
    resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=90)
    if resp.status_code != 200:
        logger.error(f"Image API error: {resp.text[:300]}")
        raise Exception(f"API Error: {resp.text[:200]}")
    result = resp.json()
    message = result["choices"][0]["message"]
    if "images" in message:
        image_data = message["images"][0]["image_url"]["url"]
        base64_str = image_data.split(",", 1)[-1] if image_data.startswith("data:image") else image_data
        img_id = str(uuid.uuid4())[:8]
        img_path = PROJECTS_DIR / f"img_{img_id}.png"
        with open(img_path, "wb") as f:
            f.write(base64.b64decode(base64_str))
        logger.info(f"Image saved: {img_path}")
        return str(img_path)
    else:
        raise Exception("В ответе нет изображения")

def generate_code(project_type, description):
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"}
    system_prompts = {
        "web": "Ты — веб-разработчик. Создай HTML/CSS/JS сайт по описанию. Выведи полный код в одном файле.",
        "bot": "Ты — разработчик Telegram-ботов. Создай бота на Python (python-telegram-bot) по описанию. Выведи полный код.",
        "app": "Ты — разработчик приложений. Создай простое приложение на Python по описанию. Выведи полный код."
    }
    data = {
        "model": "deepseek/deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompts.get(project_type, "Ты — программист. Создай код по описанию.")},
            {"role": "user", "content": description}
        ],
        "max_tokens": 2500
    }
    resp = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data, timeout=120)
    if resp.status_code != 200:
        logger.error(f"Code API error: {resp.text[:300]}")
        raise Exception(f"API Error: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]

app = Flask(__name__)

@app.route("/")
def root():
    return jsonify({"message": "Shark AI API v3.0", "bot": "running"})

@app.route("/generations/")
def get_generations():
    conn = sqlite3.connect("ai_factory.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM generations ORDER BY created_at DESC LIMIT 20")
    rows = cursor.fetchall()
    conn.close()
    return jsonify([{"id": r[0], "type": r[2], "description": r[3], "status": r[4]} for r in rows])

# ---------- TELEGRAM BOT (webhook + polling) ----------
# Обработчик вебхука
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = request.get_json()
        if "message" in update:
            process_message(update["message"])
        return "OK", 200
    return "Not Found", 404

# Глобальные переменные для polling
offset = 0

def send_message(chat_id, text, parse_mode=None):
    try:
        requests.post(API_URL + "sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode}, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")

def send_photo(chat_id, photo_path):
    try:
        with open(photo_path, "rb") as f:
            requests.post(API_URL + "sendPhoto", data={"chat_id": chat_id}, files={"photo": f})
    except Exception as e:
        logger.error(f"Failed to send photo to {chat_id}: {e}")
        send_message(chat_id, f"❌ Ошибка отправки фото: {str(e)[:100]}")

def process_message(msg):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")
    if text == "/start":
        send_message(chat_id, "🤖 *Shark AI Bot v3.0*\n\n🎨 /image описание\n💻 /web описание\n🤖 /bot описание\n📱 /app описание\n📋 /history", parse_mode="Markdown")
    elif text == "/history":
        try:
            conn = sqlite3.connect("ai_factory.db")
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM generations ORDER BY created_at DESC LIMIT 10")
            rows = cursor.fetchall()
            conn.close()
            if rows:
                msg = "📋 *История генераций:*\n\n"
                for r in rows:
                    msg += f"• {r[2]}: {r[3][:40]}... ({r[4]})\n"
            else:
                msg = "📋 История пуста."
            send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"History error: {e}")
            send_message(chat_id, "❌ Ошибка получения истории")
    elif text.startswith("/image"):
        desc = text[7:].strip() or "пейзаж"
        send_message(chat_id, f"🎨 Генерирую изображение «{desc}»...")
        try:
            img_path = generate_image(desc)
            send_photo(chat_id, img_path)
            conn = sqlite3.connect("ai_factory.db")
            cursor = conn.cursor()
            cursor.execute("INSERT INTO generations (type, description, status, result) VALUES ('image', ?, 'done', ?)", (desc, img_path))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Image generation error: {e}")
            send_message(chat_id, f"❌ Ошибка генерации: {str(e)[:200]}")
    elif text.startswith("/web") or text.startswith("/bot") or text.startswith("/app"):
        parts = text.split(" ", 1)
        cmd = parts[0].replace("/", "")
        desc = parts[1] if len(parts) > 1 else "проект"
        send_message(chat_id, f"💻 Генерирую {cmd}: «{desc}»...")
        try:
            code = generate_code(cmd, desc)
            if len(code) > 1500:
                file_ext = "html" if cmd == "web" else "py"
                file_path = PROJECTS_DIR / f"gen_{uuid.uuid4().hex[:8]}.{file_ext}"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(code)
                send_message(chat_id, f"✅ *{cmd.upper()} «{desc}» готов!*")
            else:
                send_message(chat_id, f"✅ *{cmd.upper()} «{desc}» готов!*\n\n```\n{code}\n```", parse_mode="Markdown")
            conn = sqlite3.connect("ai_factory.db")
            cursor = conn.cursor()
            cursor.execute("INSERT INTO generations (type, description, status, result) VALUES (?, ?, 'done', ?)", (cmd, desc, code))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Code generation error: {e}")
            send_message(chat_id, f"❌ Ошибка генерации кода: {str(e)[:200]}")
    else:
        send_message(chat_id, "Используйте команды:\n/image, /web, /bot, /app, /history")

def bot_loop():
    global offset
    while True:
        try:
            resp = requests.get(API_URL + "getUpdates", params={"offset": offset, "timeout": 30}, timeout=35).json()
            if resp.get("ok"):
                for upd in resp["result"]:
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        process_message(upd["message"])
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

# Запускаем цикл опроса в фоне
threading.Thread(target=bot_loop, daemon=True).start()
logger.info("Shark AI Bot + API started!")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
