"""
Cog: Music
-----------
Команды:
  /play запрос_или_ссылка  -> ищет на YouTube (если это не прямая ссылка) и либо
                              начинает играть, либо ставит в очередь
  /skip                    -> пропустить текущий трек
  /pause /resume           -> пауза/продолжить
  /stop                    -> остановить и очистить очередь, бот выходит из канала
  /queue                   -> показать очередь

Требования на сервере, где крутится бот:
  - установлен ffmpeg (apt install ffmpeg на Ubuntu)
  - установлен yt-dlp (pip install yt-dlp)
  - PyNaCl (pip install pynacl) для работы голоса
"""

import os
import asyncio
import logging
from collections import deque
from dataclasses import dataclass

import discord
import yt_dlp
import imageio_ffmpeg
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("family-bot.music")

FFMPEG_EXECUTABLE = imageio_ffmpeg.get_ffmpeg_exe()  # путь к встроенному бинарнику ffmpeg (из пакета imageio-ffmpeg)

# discord.py нужна библиотека libopus для кодирования голоса. На некоторых хостингах
# (контейнерах без системного apt-доступа) её нет — подгружаем встроенную версию
# из пакета opuslib-next-bundled, если discord.py не нашла системную сама.
if not discord.opus.is_loaded():
    try:
        import opuslib_next
        _opus_path = os.path.join(os.path.dirname(opuslib_next.__file__), "_native", "libopus.so")
        if os.path.exists(_opus_path):
            discord.opus.load_opus(_opus_path)
            log.info(f"libopus загружена из opuslib-next-bundled: {_opus_path}")
    except Exception:
        log.exception("Не удалось загрузить libopus из opuslib-next-bundled")

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

FFMPEG_OPTS = {
    "before_options": "-nostdin",
    "options": "-vn -threads 1",
}


@dataclass
class Track:
    title: str
    url: str
    stream_url: str
    requested_by: str
    http_headers: dict


def extract_track(query: str) -> Track:
    """Синхронная функция — вызывается через run_in_executor, чтобы не блокировать бота."""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:  # результат поиска -> берём первый
            info = info["entries"][0]
        return Track(
            title=info.get("title", "Неизвестный трек"),
            url=info.get("webpage_url", query),
            stream_url=info["url"],
            requested_by="",
            http_headers=info.get("http_headers", {}),
        )


class GuildMusicState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.current: Track | None = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildMusicState()
        return self.states[guild_id]

    async def ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        state = self.get_state(interaction.guild_id)
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.followup.send("❌ Зайди сначала в голосовой канал.", ephemeral=True)
            return None
        channel = interaction.user.voice.channel
        if state.voice_client is None or not state.voice_client.is_connected():
            state.voice_client = await channel.connect()
        elif state.voice_client.channel.id != channel.id:
            await state.voice_client.move_to(channel)
        return state.voice_client

    def play_next(self, guild_id: int):
        state = self.get_state(guild_id)
        if not state.queue:
            state.current = None
            return
        track = state.queue.popleft()
        state.current = track

        ffmpeg_opts = dict(FFMPEG_OPTS)
        if track.http_headers:
            headers_str = "".join(f"{k}: {v}\r\n" for k, v in track.http_headers.items())
            ffmpeg_opts["before_options"] = f'-headers "{headers_str}" ' + ffmpeg_opts["before_options"]

        source = discord.FFmpegPCMAudio(track.stream_url, executable=FFMPEG_EXECUTABLE, **ffmpeg_opts)

        def after_playing(error):
            if error:
                log.error(f"Ошибка воспроизведения: {error}")
            self.play_next(guild_id)

        state.voice_client.play(source, after=after_playing)

    @app_commands.command(name="play", description="Найти и воспроизвести музыку (название, исполнитель или ссылка YouTube)")
    @app_commands.describe(запрос="Название песни / исполнитель, либо ссылка на YouTube")
    async def play(self, interaction: discord.Interaction, запрос: str):
        await interaction.response.defer()
        vc = await self.ensure_voice(interaction)
        if vc is None:
            return

        loop = asyncio.get_event_loop()
        try:
            track = await loop.run_in_executor(None, extract_track, запрос)
        except Exception:
            log.exception("Ошибка поиска трека")
            await interaction.followup.send("❌ Не удалось найти/загрузить трек. Проверь запрос или ссылку.")
            return

        track.requested_by = str(interaction.user)
        state = self.get_state(interaction.guild_id)
        state.queue.append(track)

        if not vc.is_playing() and not vc.is_paused():
            self.play_next(interaction.guild_id)
            await interaction.followup.send(f"▶️ Сейчас играет: **{track.title}**")
        else:
            await interaction.followup.send(f"➕ Добавлено в очередь: **{track.title}** (позиция {len(state.queue)})")

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()  # вызовет after_playing -> play_next
            await interaction.response.send_message("⏭️ Трек пропущен.")
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

    @app_commands.command(name="pause", description="Поставить воспроизведение на паузу")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            await interaction.response.send_message("⏸️ Пауза.")
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

    @app_commands.command(name="resume", description="Продолжить воспроизведение")
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            await interaction.response.send_message("▶️ Продолжаем.")
        else:
            await interaction.response.send_message("Воспроизведение не на паузе.", ephemeral=True)

    @app_commands.command(name="stop", description="Остановить музыку, очистить очередь и выйти из канала")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.queue.clear()
        if state.voice_client:
            await state.voice_client.disconnect()
            state.voice_client = None
        state.current = None
        await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")

    @app_commands.command(name="queue", description="Показать текущую очередь треков")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        lines = []
        if state.current:
            lines.append(f"▶️ Сейчас играет: **{state.current.title}**")
        if state.queue:
            for i, t in enumerate(state.queue, start=1):
                lines.append(f"{i}. {t.title} (заказал: {t.requested_by})")
        if not lines:
            lines = ["Очередь пуста."]
        await interaction.response.send_message("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
