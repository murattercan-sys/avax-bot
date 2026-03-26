import os
import requests
import time

TOKEN = os.getenv("TOKEN")
URL = f"https://api.telegram.org/bot{TOKEN}"

last_update = None

def send_message(chat_id, text):
    requests.get(f"{URL}/sendMessage", params={"chat_id": chat_id, "text": text})

while True:
    r = requests.get(f"{URL}/getUpdates").json()

    for update in r["result"]:
        update_id = update["update_id"]

        if last_update is None or update_id > last_update:
            last_update = update_id

            chat_id = update["message"]["chat"]["id"]
            text = update["message"]["text"]

            if text == "/start":
                send_message(chat_id, "🚀 Murat bot aktif.")
            elif text.upper() == "STATUS":
                send_message(chat_id, "Bot çalışıyor.")
            else:
                send_message(chat_id, f"Gelen mesaj: {text}")

    time.sleep(2)
