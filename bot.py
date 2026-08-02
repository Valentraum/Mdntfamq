"""
Family Discord Bot — главный файл запуска.

Функции разложены по модулям (cogs) в папке cogs/:
  - news.py     -> слежение за RSS-источником и постинг новостей/changelog
  - music.py    -> поиск и воспроизведение музыки (YouTube) в голосовом канале
  - forms.py    -> вебхук-эндпоинт, принимающий новые ответы Google Form
  - giveaway.py -> розыгрыши с кнопкой "Участвовать" и случайным выбором победителя

Конфигурация — через переменные окружения (см. .env.example / README.md).
"""

import os
import logging
import asyncio

from dotenv import load_dotenv
import discord
from discord.ext import commands

load_dotenv()  # подхватывает переменные из файла .env, если он лежит рядом с bot.py

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("family-bot")

TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN") or ""
GUILD_ID = os.environ.get("GUILD_ID")  # опционально, для мгновенной регистрации слэш-команд

intents = discord.Intents.default()
intents.message_content = True  # нужно для команд типа !play, если решишь их добавить; слэш-командам не мешает

bot = commands.Bot(command_prefix="!", intents=intents)

INITIAL_EXTENSIONS = [
    "cogs.news",
    "cogs.music",
    "cogs.giveaway",
]
# Примечание: заявки с Google Form отправляются напрямую через Discord Webhook
# из Google Apps Script (см. google_apps_script.gs и README.md) — боту для этого
# отдельный модуль не нужен.


@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
        else:
            synced = await bot.tree.sync()
        log.info(f"Синхронизировано команд: {len(synced)}")
    except Exception:
        log.exception("Ошибка синхронизации слэш-команд")
    log.info(f"Бот запущен как {bot.user} (ID: {bot.user.id})")


async def main():
    async with bot:
        for ext in INITIAL_EXTENSIONS:
            try:
                await bot.load_extension(ext)
                log.info(f"Загружен модуль: {ext}")
            except Exception:
                log.exception(f"Не удалось загрузить модуль {ext}")
        if not TOKEN:
            raise SystemExit("Не задан DISCORD_TOKEN. Установи переменную окружения с токеном бота.")
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
