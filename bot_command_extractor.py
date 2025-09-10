import asyncio
from telethon import TelegramClient, events
from telegram import Update, Bot, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIG ==========
API_ID = 29028065          # ganti dengan API_ID dari my.telegram.org
API_HASH = "048042efcb7d21612b1eb1a8adced921"  # ganti dengan API_HASH dari my.telegram.org
SESSION_NAME = "buyer_session"

MASTER_USERNAME = "MasterBot"   # username bot master, misal "myosintbot"
TOKEN_TIRUAN = "TOKEN_TIRUAN"   # token bot tiruan dari BotFather
# ============================

tg_user = TelegramClient(SESSION_NAME, API_ID, API_HASH)
bot_tiruan = Bot(TOKEN_TIRUAN)

COMMANDS = [
    BotCommand("help", "Menampilkan informasi bantuan"),
    BotCommand("location", "Lokasi pelanggan berdasarkan MSISDN"),
    BotCommand("locimei", "Data pelanggan berdasarkan IMEI"),
    BotCommand("quota", "Informasi kuota dan penggunaan harian")
]

async def set_commands():
    await bot_tiruan.set_my_commands(COMMANDS)

async def relay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    cmd = update.message.text

    async with tg_user:
        await tg_user.send_message(MASTER_USERNAME, cmd)

        @tg_user.on(events.NewMessage(from_users=MASTER_USERNAME))
        async def handler(event):
            reply = event.raw_text
            await bot_tiruan.send_message(chat_id=user_id, text=reply)
            tg_user.remove_event_handler(handler)

def main():
    app = Application.builder().token(TOKEN_TIRUAN).build()
    for cmd in ["help", "location", "locimei", "quota"]:
        app.add_handler(CommandHandler(cmd, relay_command))
    app.post_init = set_commands
    print("🤖 Bot Tiruan aktif...")
    app.run_polling()

if __name__ == "__main__":
    main()
