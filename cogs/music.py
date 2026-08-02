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

Как это работает:
  yt-dlp запускается отдельным процессом и передаёт аудио-байты напрямую в
  ffmpeg через pipe (без записи на диск и без сети внутри самого ffmpeg —
  всей сетью занимается yt-dlp). Играть начинает почти сразу, как при обычном
  стриминге, но при этом ffmpeg никогда не открывает HTTPS-соединения сам —
  это было нужно, т.к. на некоторых урезанных хостингах ffmpeg падал с
  segfault именно при попытке читать сетевой поток напрямую.

Требования на сервере, где крутится бот:
  - ffmpeg (идёт через пакет imageio-ffmpeg, ставится через pip)
  - yt-dlp (pip install yt-dlp)
  - libopus (идёт через пакет opuslib-next-bundled, ставится через pip)
"""

import os
import sys
import asyncio
import logging
import subprocess
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

INFO_YDL_OPTS = {
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
    search_query: str  # то, что передадим в yt-dlp CLI при непосредственном запуске
    requested_by: str


def fetch_track_info(query: str) -> Track:
    """Синхронная функция (run_in_executor) — только достаёт метаданные (название,
    ссылку), САМО аудио тут не скачивается — это отдельный быстрый запрос."""
    with yt_dlp.YoutubeDL(INFO_YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
        if "entries" in info:  # результат поиска -> берём первый
            info = info["entries"][0]
        webpage_url = info.get("webpage_url", query)
        return Track(
            title=info.get("title", "Неизвестный трек"),
            url=webpage_url,
            search_query=webpage_url,  # запускаем yt-dlp CLI уже по прямой ссылке на видео
            requested_by="",
        )


class GuildMusicState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.voice_client: discord.VoiceClient | None = None
        self.current: Track | None = None
        self.current_process: subprocess.Popen | None = None


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
            state.current_process = None
            return
        track = state.queue.popleft()
        state.current = track

        # yt-dlp сам качает и пишет аудио-байты в stdout, ffmpeg читает их из этого
        # pipe — сети внутри ffmpeg нет вообще, ею занимается только yt-dlp.
        ytdlp_cmd = [
            sys.executable, "-m", "yt_dlp",
            "-f", "bestaudio/best",
            "--no-playlist",
            "-o", "-",
            "--quiet",
            "--no-warnings",
            track.search_query,
        ]
        proc = subprocess.Popen(ytdlp_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        state.current_process = proc

        source = discord.FFmpegPCMAudio(proc.stdout, pipe=True, executable=FFMPEG_EXECUTABLE, **FFMPEG_OPTS)

        def after_playing(error):
            if error:
                log.error(f"Ошибка воспроизведения: {error}")
            try:
                proc.terminate()
            except Exception:
                pass
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
            track = await loop.run_in_executor(None, fetch_track_info, запрос)
        except Exception:
            log.exception("Ошибка поиска трека")
            await interaction.followup.send("❌ Не удалось найти трек. Проверь запрос или ссылку.")
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
        if state.current_process:
            try:
                state.current_process.terminate()
            except Exception:
                pass
        if state.voice_client:
            await state.voice_client.disconnect()
            state.voice_client = None
        state.current = None
        state.current_process = None
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
