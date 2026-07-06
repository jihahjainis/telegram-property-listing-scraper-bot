from telethon import TelegramClient, events
from datetime import datetime
from telegram_config import get_telegram_config

api_id, api_hash, session = get_telegram_config()

client = TelegramClient(session, api_id, api_hash)

KEYWORDS = [
    'wtr',
    'want to rent',
    'looking for tenant',
    'tenant needed',
    'room for rent',
]

@client.on(events.NewMessage)
async def handler(event):
    message = event.raw_text.lower()

    if any(keyword in message for keyword in KEYWORDS):

        print("\n========================")
        print("PROPERTY FOUND")
        print("========================")

        print(f"Date: {datetime.now()}")
        print(f"Group: {event.chat.title}")
        print(f"Message:\n{event.raw_text}")

client.start()

print("Listening for property messages...")
client.run_until_disconnected()
