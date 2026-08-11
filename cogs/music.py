"""
Cog: Music
-----------
Слэш-команды (оставлены для гибкости):
  /play запрос_или_ссылка  -> ищет на YouTube (если это не прямая ссылка) и либо
                              начинает играть, либо ставит в очередь
  /skip                    -> пропустить текущий трек
  /pause /resume           -> пауза/продолжить
  /stop                    -> остановить и очистить очередь, бот выходит из канала
  /queue                   -> показать очередь
  /музыка_панель            -> опубликовать кнопочную панель управления в этом канале

Кнопочная панель (основной способ управления, см. MusicPanelView):
  Старт (зелёная)   -> продолжить с паузы, либо запустить следующий трек из очереди
  Стоп (красная)     -> остановить, очистить очередь, выйти из канала
  Скип (синяя)        -> пропустить текущий трек
  Поставить трек в очередь (зелёная) -> открывает окно ввода ссылки/названия

Все ответы на команды и кнопки — ephemeral (видит только тот, кто нажал/написал).

Автовыход из голосового канала:
  Бот сам отключается, как только в голосовом канале не остаётся ни одного
  человека (только бот). Пустая очередь сама по себе поводом уйти не
  является — бот спокойно ждёт в канале, /stop прописывать вручную не нужно.

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
import threading
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
    "extractor_args": {"youtube": {"player_client": ["android"]}},
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
        # регистрируем персистентную панель, чтобы кнопки работали и после рестарта бота
        self.bot.add_view(MusicPanelView(self))

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
            # очередь закончилась — бот просто ждёт в канале (выйдет сам, когда все разойдутся, см. on_voice_state_update)
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
            "--extractor-args", "youtube:player_client=android",
            track.search_query,
        ]
        proc = subprocess.Popen(ytdlp_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        state.current_process = proc

        def log_stderr(process: subprocess.Popen, track_title: str):
            for line in iter(process.stderr.readline, b""):
                text = line.decode(errors="replace").strip()
                if text:
                    log.warning(f"[yt-dlp stderr | {track_title}] {text}")

        threading.Thread(target=log_stderr, args=(proc, track.title), daemon=True).start()

        source = discord.FFmpegPCMAudio(proc.stdout, pipe=True, executable=FFMPEG_EXECUTABLE, **FFMPEG_OPTS)

        def after_playing(error):
            if error:
                log.error(f"Ошибка воспроизведения: {error}")
            exit_code = proc.poll()
            if exit_code is not None and exit_code != 0:
                log.warning(f"yt-dlp завершился с кодом {exit_code} для трека «{track.title}» — возможно, поток оборвался раньше времени.")
            try:
                proc.terminate()
            except Exception:
                pass
            self.play_next(guild_id)

        state.voice_client.play(source, after=after_playing)

    async def start_playback(self, interaction: discord.Interaction) -> str:
        """Общая логика для кнопки «Старт» и команды /resume-подобного поведения.
        Возвращает текст ответа пользователю."""
        state = self.get_state(interaction.guild_id)
        vc = await self.ensure_voice(interaction)
        if vc is None:
            return ""  # ensure_voice уже отправил сообщение об ошибке
        if vc.is_paused():
            vc.resume()
            return "▶️ Продолжаем."
        if vc.is_playing():
            return "Уже играет."
        if state.queue:
            self.play_next(interaction.guild_id)
            return f"▶️ Запускаю: **{state.current.title}**" if state.current else "▶️ Запускаю."
        return "Очередь пуста — сначала добавь трек кнопкой «Поставить трек в очередь»."

    async def add_to_queue(self, interaction: discord.Interaction, query: str):
        """Общая логика добавления трека и в /play, и в кнопку очереди."""
        vc = await self.ensure_voice(interaction)
        if vc is None:
            return

        loop = asyncio.get_event_loop()
        try:
            track = await loop.run_in_executor(None, fetch_track_info, query)
        except Exception:
            log.exception("Ошибка поиска трека")
            await interaction.followup.send("❌ Не удалось найти трек. Проверь запрос или ссылку.", ephemeral=True)
            return

        track.requested_by = str(interaction.user)
        state = self.get_state(interaction.guild_id)
        state.queue.append(track)

        if not vc.is_playing() and not vc.is_paused():
            self.play_next(interaction.guild_id)
            await interaction.followup.send(f"▶️ Сейчас играет: **{track.title}**", ephemeral=True)
        else:
            await interaction.followup.send(
                f"➕ Добавлено в очередь: **{track.title}** (позиция {len(state.queue)})",
                ephemeral=True,
            )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Реагируем на изменения состояния САМОГО бота
        if member.id == self.bot.user.id:
            # Бота отключили из канала вручную (или разрыв соединения) -> канал стал None
            if before.channel is not None and after.channel is None:
                state = self.states.get(member.guild.id)
                if state:
                    log.info(f"Бота отключили из голосового канала на сервере {member.guild.id} — чищу очередь и процессы.")
                    if state.current_process:
                        try:
                            state.current_process.terminate()
                        except Exception:
                            pass
                    state.queue.clear()
                    state.current = None
                    state.current_process = None
                    state.voice_client = None
            return

        # Кто-то другой вышел из канала, где сейчас сидит бот -> проверяем, не опустел ли канал
        if before.channel is None:
            return
        state = self.states.get(before.channel.guild.id)
        if not state or not state.voice_client or state.voice_client.channel.id != before.channel.id:
            return
        remaining_humans = [m for m in before.channel.members if not m.bot]
        if not remaining_humans:
            log.info(f"В канале {before.channel.id} никого не осталось — бот выходит сам.")
            await self._do_stop(before.channel.guild.id)

    # -- Слэш-команды ------------------------------------------------------

    @app_commands.command(name="play", description="Найти и воспроизвести музыку (название, исполнитель или ссылка YouTube)")
    @app_commands.describe(запрос="Название песни / исполнитель, либо ссылка на YouTube")
    async def play(self, interaction: discord.Interaction, запрос: str):
        await interaction.response.defer(ephemeral=True)  # ответ видит только тот, кто написал
        await self.add_to_queue(interaction, запрос)

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()  # вызовет after_playing -> play_next
            await interaction.response.send_message("⏭️ Трек пропущен.", ephemeral=True)
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

    @app_commands.command(name="pause", description="Поставить воспроизведение на паузу")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            await interaction.response.send_message("⏸️ Пауза.", ephemeral=True)
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

    @app_commands.command(name="resume", description="Продолжить воспроизведение")
    async def resume(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            await interaction.response.send_message("▶️ Продолжаем.", ephemeral=True)
        else:
            await interaction.response.send_message("Воспроизведение не на паузе.", ephemeral=True)

    @app_commands.command(name="stop", description="Остановить музыку, очистить очередь и выйти из канала")
    async def stop(self, interaction: discord.Interaction):
        await self._do_stop(interaction.guild_id)
        await interaction.response.send_message("⏹️ Остановлено, очередь очищена.", ephemeral=True)

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
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="музыка_панель", description="Опубликовать кнопочную панель управления музыкой в этом канале")
    async def post_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎵 Управление музыкой",
            description=(
                "**Старт** — продолжить с паузы или запустить следующий трек из очереди.\n"
                "**Стоп** — остановить, очистить очередь и выйти из канала.\n"
                "**Скип** — пропустить текущий трек.\n"
                "**Поставить трек в очередь** — добавить трек по ссылке или названию.\n\n"
                "Бот сам выходит из голосового канала, когда там не остаётся людей."
            ),
            color=0x2C2F33,
        )
        await interaction.response.send_message(embed=embed, view=MusicPanelView(self))

    async def _do_stop(self, guild_id: int):
        state = self.get_state(guild_id)
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


# ---------------------------------------------------------------------------
# Кнопочная панель
# ---------------------------------------------------------------------------

class QueueTrackModal(discord.ui.Modal, title="Добавить трек в очередь"):
    def __init__(self, cog: Music):
        super().__init__()
        self.cog = cog
        self.query = discord.ui.TextInput(label="Ссылка на YouTube или название трека", required=True)
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.add_to_queue(interaction, str(self.query.value))


class MusicPanelView(discord.ui.View):
    """timeout=None + фиксированные custom_id -> кнопки продолжают работать после
    перезапуска бота, если вызвать bot.add_view(MusicPanelView(cog)) при старте."""

    def __init__(self, cog: Music):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Старт", style=discord.ButtonStyle.success, custom_id="music_panel:start")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        text = await self.cog.start_playback(interaction)
        if text:
            await interaction.followup.send(text, ephemeral=True)

    @discord.ui.button(label="Стоп", style=discord.ButtonStyle.danger, custom_id="music_panel:stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._do_stop(interaction.guild_id)
        await interaction.response.send_message("⏹️ Остановлено, очередь очищена.", ephemeral=True)

    @discord.ui.button(label="Скип", style=discord.ButtonStyle.primary, custom_id="music_panel:skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = self.cog.get_state(interaction.guild_id)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.voice_client.stop()  # вызовет after_playing -> play_next
            await interaction.response.send_message("⏭️ Трек пропущен.", ephemeral=True)
        else:
            await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

    @discord.ui.button(label="Поставить трек в очередь", style=discord.ButtonStyle.success, custom_id="music_panel:queue")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QueueTrackModal(self.cog))


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
