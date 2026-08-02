"""
Cog: News / Changelog forwarding
---------------------------------
Пересылает каждое новое сообщение из одного канала сервера (SOURCE_CHANNEL_ID)
в другой канал (TARGET_CHANNEL_ID) на том же сервере.

Переносится: текст сообщения, вложения (картинки/файлы) и встроенные embed'ы
(например, если исходное сообщение само пришло от другого бота).

Настройка через переменные окружения:
  NEWS_SOURCE_CHANNEL_ID -> ID канала-источника (откуда берём новости/changelog)
  NEWS_TARGET_CHANNEL_ID -> ID канала, куда пересылать
"""

import os
import logging

import discord
from discord.ext import commands

log = logging.getLogger("family-bot.news")

SOURCE_CHANNEL_ID = os.environ.get("NEWS_SOURCE_CHANNEL_ID")
TARGET_CHANNEL_ID = os.environ.get("NEWS_TARGET_CHANNEL_ID")


class News(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not SOURCE_CHANNEL_ID or not TARGET_CHANNEL_ID:
            log.warning(
                "NEWS_SOURCE_CHANNEL_ID или NEWS_TARGET_CHANNEL_ID не заданы — "
                "пересылка новостей не активна."
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not SOURCE_CHANNEL_ID or not TARGET_CHANNEL_ID:
            return
        if str(message.channel.id) != str(SOURCE_CHANNEL_ID):
            return

        target = self.bot.get_channel(int(TARGET_CHANNEL_ID))
        if target is None:
            log.warning(f"Целевой канал {TARGET_CHANNEL_ID} не найден.")
            return

        files = [await a.to_file() for a in message.attachments]

        try:
            await target.send(
                content=message.content or None,
                embeds=message.embeds if message.embeds else None,
                files=files if files else None,
            )
        except Exception:
            log.exception("Не удалось переслать сообщение с новостями")


async def setup(bot: commands.Bot):
    await bot.add_cog(News(bot))
