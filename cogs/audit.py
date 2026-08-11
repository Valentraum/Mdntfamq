"""
Cog: Кадровые аудиты
---------------------
Персистентная embed-панель с кнопками управления составом:

  Понизить (красная)               -> снимает текущий трек-ранг, выдаёт на ступень ниже
  Повысить (зелёная)                -> выдаёт следующий ранг по порядку (только среди
                                        "линейных" рангов 1-5, см. RANK_ROLES ниже)
  Назначить на должность (синяя)    -> ручное назначение сразу на одну из "должностей"
                                        (ранги 6-8) — например, перескок с 5-го ранга на 7-й
  Удалить пользователя дискорда (серая) -> кик с подтверждением (кнопки Да/Отмена)
  Выдать выговор (серая)            -> модалка (ID + причина) + после отправки бот просит
                                        прислать скриншот отдельным сообщением в канал
  Снять выговор (серая)             -> модалка (ID + причина типа "отработал"), снимает
                                        последний активный выговор

Все участники в логах упоминаются через Discord ID (<@id>), а не через поиск по нику —
это специально, чтобы аудит работал даже для тех, кто уже вышел с сервера.

Кто может нажимать кнопки:
  участники с одной из "должностных" ролей (Кадровик/Бухгалтер/Смотрящий, ранги 6-8)
  либо с правом Discord "Управление ролями" / администратор.

Хранение:
  data/reprimands.json — история выговоров по каждому Discord ID (переживает рестарт бота).

Настройка через переменные окружения:
  AUDIT_LOG_CHANNEL_ID -> канал, куда постятся все записи аудита (по умолчанию см. ниже,
                           можно поменять в .env в любой момент)
"""

import os
import json
import logging
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("family-bot.audit")

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

# Канал для логов кадрового аудита. Задан по умолчанию тем ID, что дали изначально,
# но его можно в любой момент переопределить через .env (AUDIT_LOG_CHANNEL_ID=...),
# без правки кода.
DEFAULT_AUDIT_LOG_CHANNEL_ID = 1536569352873054229
AUDIT_LOG_CHANNEL_ID = int(os.environ.get("AUDIT_LOG_CHANNEL_ID", DEFAULT_AUDIT_LOG_CHANNEL_ID))

# Линейная лестница рангов, от самого младшего к самому старшему.
# ВАЖНО: порядок имеет значение — "Повысить"/"Понизить" двигаются по этому списку.
RANK_ROLES = [
    ("Новичок", 1532060427964383257),
    ("Фармила 1", 1532060427964383258),
    ("Фармила 2", 1532060427964383259),
    ("Фармила 3", 1532439931387904050),
    ("Туллер", 1532060427964383260),
    ("Кадровик", 1532060427964383261),
    ("Бухгалтер", 1532060427964383262),
    ("Смотрящий", 1532060427981164695),
]
RANK_ROLE_IDS = {role_id for _, role_id in RANK_ROLES}

# С этого индекса (включительно) ранги считаются "должностями": их не выдаёт кнопка
# "Повысить" автоматически — только "Назначить на должность" вручную.
POSITION_START_INDEX = 4  # 0-indexed -> RANK_ROLES[4] == "Туллер"
# Максимальный индекс, до которого может дотянуть кнопка "Повысить" сама по себе.
AUTO_PROMOTE_MAX_INDEX = POSITION_START_INDEX - 1  # "Фармила 3"

POSITIONS = RANK_ROLES[POSITION_START_INDEX:]  # [("Туллер", id), ("Кадровик", id), ("Бухгалтер", id), ("Смотрящий", id)]
POSITION_NAMES_LOWER = {name.lower(): (name, role_id) for name, role_id in POSITIONS}

# Кто может нажимать кнопки панели — отдельно от списка "должностей для назначения" выше:
# доступ по-прежнему только от Кадровика и выше (Туллер в этот список не входит).
HR_PANEL_ACCESS_ROLES = RANK_ROLES[5:]  # [("Кадровик", id), ("Бухгалтер", id), ("Смотрящий", id)]

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
REPRIMANDS_PATH = os.path.join(DATA_DIR, "reprimands.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Хранилище выговоров (простой JSON-файл + asyncio.Lock, чтобы не было гонок)
# ---------------------------------------------------------------------------

class ReprimandStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump({}, f)

    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def add_reprimand(self, user_id: int, reason: str, screenshot_url: str | None, issued_by: int) -> dict:
        async with self._lock:
            data = self._read()
            key = str(user_id)
            entries = data.setdefault(key, [])
            entry = {
                "reason": reason,
                "screenshot_url": screenshot_url,
                "issued_by": issued_by,
                "issued_at": now_iso(),
                "active": True,
                "resolved_reason": None,
                "resolved_by": None,
                "resolved_at": None,
            }
            entries.append(entry)
            self._write(data)
            return entry

    async def resolve_latest(self, user_id: int, resolve_reason: str, resolved_by: int) -> dict | None:
        async with self._lock:
            data = self._read()
            key = str(user_id)
            entries = data.get(key, [])
            for entry in reversed(entries):
                if entry.get("active"):
                    entry["active"] = False
                    entry["resolved_reason"] = resolve_reason
                    entry["resolved_by"] = resolved_by
                    entry["resolved_at"] = now_iso()
                    self._write(data)
                    return entry
            return None

    async def active_count(self, user_id: int) -> int:
        async with self._lock:
            data = self._read()
            entries = data.get(str(user_id), [])
            return sum(1 for e in entries if e.get("active"))


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def has_hr_permission(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_roles:
        return True
    member_role_ids = {r.id for r in member.roles}
    access_role_ids = {role_id for _, role_id in HR_PANEL_ACCESS_ROLES}
    return bool(member_role_ids & access_role_ids)


def current_rank_index(member: discord.Member) -> int:
    """Индекс самого старшего трек-ранга, который сейчас есть у участника, либо -1."""
    member_role_ids = {r.id for r in member.roles}
    best = -1
    for i, (_, role_id) in enumerate(RANK_ROLES):
        if role_id in member_role_ids:
            best = i
    return best


async def resolve_member(guild: discord.Guild, raw_id: str) -> discord.Member | None:
    try:
        user_id = int(raw_id.strip())
    except ValueError:
        return None
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            return None
    return member


def build_log_embed(title: str, color: int, fields: list[tuple[str, str]], image_url: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    for name, value in fields:
        embed.add_field(name=name, value=value or "—", inline=False)
    if image_url:
        embed.set_image(url=image_url)
    return embed


# ---------------------------------------------------------------------------
# Подтверждение для кика
# ---------------------------------------------------------------------------

class ConfirmKickView(discord.ui.View):
    def __init__(self, cog: "Audit", target_id: int, reason: str, moderator_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.target_id = target_id
        self.reason = reason
        self.moderator_id = moderator_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.moderator_id:
            await interaction.response.send_message("Эта кнопка не для тебя.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Да, точно кикнуть", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(self.target_id) or await resolve_member(interaction.guild, str(self.target_id))
        if member is None:
            await interaction.followup.send("❌ Участник уже не на сервере.", ephemeral=True)
            self.stop()
            return
        try:
            await member.kick(reason=f"{self.reason} (модератор: {interaction.user})")
        except discord.Forbidden:
            await interaction.followup.send("❌ Не хватает прав, чтобы кикнуть этого участника.", ephemeral=True)
            self.stop()
            return

        await interaction.followup.send(f"✅ <@{self.target_id}> удалён с сервера.", ephemeral=True)
        embed = build_log_embed(
            "👢 Удаление с сервера",
            0xB0B0B0,
            [
                ("Участник", f"<@{self.target_id}> (`{self.target_id}`)"),
                ("Причина", self.reason),
                ("Модератор", f"<@{interaction.user.id}>"),
            ],
        )
        await self.cog.send_log(embed)
        self.stop()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Отменено.", view=None)
        self.stop()


# ---------------------------------------------------------------------------
# Модалки
# ---------------------------------------------------------------------------

class PromoteDemoteModal(discord.ui.Modal):
    def __init__(self, cog: "Audit", mode: str):
        # mode: "promote" | "demote"
        title = "Повысить участника" if mode == "promote" else "Понизить участника"
        super().__init__(title=title)
        self.cog = cog
        self.mode = mode
        self.discord_id = discord.ui.TextInput(label="Discord ID участника", placeholder="например: 123456789012345678", required=True)
        self.reason = discord.ui.TextInput(label="Причина (необязательно)", required=False, style=discord.TextStyle.paragraph)
        self.add_item(self.discord_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_promote_demote(interaction, self.mode, str(self.discord_id.value), str(self.reason.value or ""))


class AssignPositionModal(discord.ui.Modal, title="Назначить на должность"):
    def __init__(self, cog: "Audit"):
        super().__init__()
        self.cog = cog
        self.discord_id = discord.ui.TextInput(label="Discord ID участника", placeholder="например: 123456789012345678", required=True)
        names = " / ".join(name for name, _ in POSITIONS)
        self.position = discord.ui.TextInput(label=f"Должность ({names})", required=True)
        self.reason = discord.ui.TextInput(label="Причина (необязательно)", required=False, style=discord.TextStyle.paragraph)
        self.add_item(self.discord_id)
        self.add_item(self.position)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_assign_position(interaction, str(self.discord_id.value), str(self.position.value), str(self.reason.value or ""))


class KickModal(discord.ui.Modal, title="Удалить пользователя с сервера"):
    def __init__(self, cog: "Audit"):
        super().__init__()
        self.cog = cog
        self.discord_id = discord.ui.TextInput(label="Discord ID участника", placeholder="например: 123456789012345678", required=True)
        self.reason = discord.ui.TextInput(label="Причина", required=True, style=discord.TextStyle.paragraph)
        self.add_item(self.discord_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_kick_request(interaction, str(self.discord_id.value), str(self.reason.value))


class ReprimandModal(discord.ui.Modal, title="Выдать выговор"):
    def __init__(self, cog: "Audit"):
        super().__init__()
        self.cog = cog
        self.discord_id = discord.ui.TextInput(label="Discord ID участника", placeholder="например: 123456789012345678", required=True)
        self.reason = discord.ui.TextInput(label="Причина выговора", required=True, style=discord.TextStyle.paragraph)
        self.add_item(self.discord_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_reprimand(interaction, str(self.discord_id.value), str(self.reason.value))


class RemoveReprimandModal(discord.ui.Modal, title="Снять выговор"):
    def __init__(self, cog: "Audit"):
        super().__init__()
        self.cog = cog
        self.discord_id = discord.ui.TextInput(label="Discord ID участника", placeholder="например: 123456789012345678", required=True)
        self.reason = discord.ui.TextInput(label="Причина снятия (например: отработал)", required=True, style=discord.TextStyle.paragraph)
        self.add_item(self.discord_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_remove_reprimand(interaction, str(self.discord_id.value), str(self.reason.value))


# ---------------------------------------------------------------------------
# Персистентная панель
# ---------------------------------------------------------------------------

class AuditPanelView(discord.ui.View):
    """timeout=None + фиксированные custom_id -> кнопки продолжают работать после
    перезапуска бота, если вызвать bot.add_view(AuditPanelView(cog)) при старте."""

    def __init__(self, cog: "Audit"):
        super().__init__(timeout=None)
        self.cog = cog

    async def _check_permission(self, interaction: discord.Interaction) -> bool:
        if not has_hr_permission(interaction.user):
            await interaction.response.send_message("❌ У тебя недостаточно прав для этого действия.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Понизить", style=discord.ButtonStyle.danger, custom_id="audit_panel:demote")
    async def demote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_permission(interaction):
            return
        await interaction.response.send_modal(PromoteDemoteModal(self.cog, "demote"))

    @discord.ui.button(label="Повысить", style=discord.ButtonStyle.success, custom_id="audit_panel:promote")
    async def promote_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_permission(interaction):
            return
        await interaction.response.send_modal(PromoteDemoteModal(self.cog, "promote"))

    @discord.ui.button(label="Назначить на должность", style=discord.ButtonStyle.primary, custom_id="audit_panel:assign")
    async def assign_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_permission(interaction):
            return
        await interaction.response.send_modal(AssignPositionModal(self.cog))

    @discord.ui.button(label="Удалить пользователя дискорда", style=discord.ButtonStyle.secondary, custom_id="audit_panel:kick")
    async def kick_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_permission(interaction):
            return
        await interaction.response.send_modal(KickModal(self.cog))

    @discord.ui.button(label="Выдать выговор", style=discord.ButtonStyle.secondary, custom_id="audit_panel:reprimand")
    async def reprimand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_permission(interaction):
            return
        await interaction.response.send_modal(ReprimandModal(self.cog))

    @discord.ui.button(label="Снять выговор", style=discord.ButtonStyle.secondary, custom_id="audit_panel:unreprimand")
    async def unreprimand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_permission(interaction):
            return
        await interaction.response.send_modal(RemoveReprimandModal(self.cog))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Audit(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = ReprimandStore(REPRIMANDS_PATH)
        # регистрируем персистентную вью сразу — не нужно ждать on_ready,
        # но кнопки реально "оживут" только после первого коннекта бота
        self.bot.add_view(AuditPanelView(self))
        if not os.environ.get("AUDIT_LOG_CHANNEL_ID"):
            log.info(f"AUDIT_LOG_CHANNEL_ID не задан в .env — использую значение по умолчанию {DEFAULT_AUDIT_LOG_CHANNEL_ID}")

    async def send_log(self, embed: discord.Embed):
        channel = self.bot.get_channel(AUDIT_LOG_CHANNEL_ID)
        if channel is None:
            log.warning(f"Канал аудита {AUDIT_LOG_CHANNEL_ID} не найден — запись не отправлена.")
            return
        try:
            await channel.send(embed=embed)
        except Exception:
            log.exception("Не удалось отправить запись в канал аудита")

    # -- Повысить / Понизить -------------------------------------------------

    async def handle_promote_demote(self, interaction: discord.Interaction, mode: str, raw_id: str, reason: str):
        await interaction.response.defer(ephemeral=True)
        member = await resolve_member(interaction.guild, raw_id)
        if member is None:
            await interaction.followup.send("❌ Не нашёл такого участника на сервере по этому ID.", ephemeral=True)
            return

        idx = current_rank_index(member)

        if mode == "promote":
            if idx >= AUTO_PROMOTE_MAX_INDEX:
                await interaction.followup.send(
                    "❌ Дальше кнопка «Повысить» не работает — начиная с «Туллера» назначение "
                    "делается вручную через «Назначить на должность».",
                    ephemeral=True,
                )
                return
            new_idx = idx + 1
        else:  # demote
            if idx <= 0:
                await interaction.followup.send("❌ У участника и так нет ранга ниже, либо роли не найдены.", ephemeral=True)
                return
            if idx >= POSITION_START_INDEX:
                # понижение с любой должности (Туллер/Кадровик/Бухгалтер/Смотрящий) -> сразу до Фармилы 3,
                # а не на одну ступень ниже по списку
                new_idx = AUTO_PROMOTE_MAX_INDEX
            else:
                new_idx = idx - 1

        old_name, old_role_id = RANK_ROLES[idx] if idx >= 0 else (None, None)
        new_name, new_role_id = RANK_ROLES[new_idx]

        old_role = interaction.guild.get_role(old_role_id) if old_role_id else None
        new_role = interaction.guild.get_role(new_role_id)
        if new_role is None:
            await interaction.followup.send("❌ Не нашёл роль нового ранга на сервере — проверь ID ролей.", ephemeral=True)
            return

        try:
            if old_role:
                await member.remove_roles(old_role, reason="Изменение ранга через панель аудита")
            await member.add_roles(new_role, reason="Изменение ранга через панель аудита")
        except discord.Forbidden:
            await interaction.followup.send("❌ Не хватает прав на управление этой ролью (проверь позицию роли бота).", ephemeral=True)
            return

        action_word = "Повышение" if mode == "promote" else "Понижение"
        color = 0x2ECC71 if mode == "promote" else 0xE74C3C
        await interaction.followup.send(f"✅ {action_word}: <@{member.id}> — {old_name or '—'} → {new_name}", ephemeral=True)

        embed = build_log_embed(
            f"{'⬆️' if mode == 'promote' else '⬇️'} {action_word}",
            color,
            [
                ("Участник", f"<@{member.id}> (`{member.id}`)"),
                ("Было", old_name or "—"),
                ("Стало", new_name),
                ("Причина", reason or "—"),
                ("Модератор", f"<@{interaction.user.id}>"),
            ],
        )
        await self.send_log(embed)

    # -- Назначить на должность ----------------------------------------------

    async def handle_assign_position(self, interaction: discord.Interaction, raw_id: str, position_raw: str, reason: str):
        await interaction.response.defer(ephemeral=True)
        member = await resolve_member(interaction.guild, raw_id)
        if member is None:
            await interaction.followup.send("❌ Не нашёл такого участника на сервере по этому ID.", ephemeral=True)
            return

        match = POSITION_NAMES_LOWER.get(position_raw.strip().lower())
        if match is None:
            names = ", ".join(name for name, _ in POSITIONS)
            await interaction.followup.send(f"❌ Не узнал должность «{position_raw}». Доступные варианты: {names}.", ephemeral=True)
            return
        new_name, new_role_id = match
        new_role = interaction.guild.get_role(new_role_id)
        if new_role is None:
            await interaction.followup.send("❌ Не нашёл роль этой должности на сервере — проверь ID ролей.", ephemeral=True)
            return

        idx = current_rank_index(member)
        old_name = RANK_ROLES[idx][0] if idx >= 0 else None

        # убираем все прежние трек-роли (обычный ранг или другую должность), выдаём новую
        roles_to_remove = [interaction.guild.get_role(rid) for _, rid in RANK_ROLES if rid in {r.id for r in member.roles}]
        roles_to_remove = [r for r in roles_to_remove if r is not None]

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Назначение на должность через панель аудита")
            await member.add_roles(new_role, reason="Назначение на должность через панель аудита")
        except discord.Forbidden:
            await interaction.followup.send("❌ Не хватает прав на управление этой ролью (проверь позицию роли бота).", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Назначено: <@{member.id}> — {old_name or '—'} → {new_name}", ephemeral=True)

        embed = build_log_embed(
            "🧭 Назначение на должность",
            0x3498DB,
            [
                ("Участник", f"<@{member.id}> (`{member.id}`)"),
                ("Было", old_name or "—"),
                ("Стало", new_name),
                ("Причина", reason or "—"),
                ("Модератор", f"<@{interaction.user.id}>"),
            ],
        )
        await self.send_log(embed)

    # -- Удалить пользователя (кик) с подтверждением --------------------------

    async def handle_kick_request(self, interaction: discord.Interaction, raw_id: str, reason: str):
        try:
            target_id = int(raw_id.strip())
        except ValueError:
            await interaction.response.send_message("❌ Discord ID должен быть числом.", ephemeral=True)
            return
        member = interaction.guild.get_member(target_id)
        mention = f"<@{target_id}>"
        view = ConfirmKickView(self, target_id, reason, interaction.user.id)
        note = "" if member else "\n⚠️ Участник не найден в кэше — если он реально не на сервере, кик просто ничего не сделает."
        await interaction.response.send_message(
            f"⚠️ Точно удалить {mention} с сервера?\nПричина: {reason}{note}",
            view=view,
            ephemeral=True,
        )

    # -- Выговоры --------------------------------------------------------------

    async def handle_reprimand(self, interaction: discord.Interaction, raw_id: str, reason: str):
        try:
            target_id = int(raw_id.strip())
        except ValueError:
            await interaction.response.send_message("❌ Discord ID должен быть числом.", ephemeral=True)
            return

        await interaction.response.send_message(
            "📎 Пришли скриншот **в этот канал** одним сообщением в течение 2 минут "
            "(или напиши `нет`, если скриншота не будет).",
            ephemeral=True,
        )

        def check(m: discord.Message):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id

        screenshot_url = None
        try:
            msg = await self.bot.wait_for("message", check=check, timeout=120)
            if msg.attachments:
                screenshot_url = msg.attachments[0].url
            try:
                await msg.delete()
            except Exception:
                pass
        except asyncio.TimeoutError:
            await interaction.followup.send("⌛ Время вышло — выговор сохранён без скриншота.", ephemeral=True)

        await self.store.add_reprimand(target_id, reason, screenshot_url, interaction.user.id)
        await interaction.followup.send(f"✅ Выговор выдан: <@{target_id}>", ephemeral=True)

        embed = build_log_embed(
            "⚠️ Выговор",
            0xB0B0B0,
            [
                ("Участник", f"<@{target_id}> (`{target_id}`)"),
                ("Причина", reason),
                ("Модератор", f"<@{interaction.user.id}>"),
            ],
            image_url=screenshot_url,
        )
        await self.send_log(embed)

    async def handle_remove_reprimand(self, interaction: discord.Interaction, raw_id: str, reason: str):
        await interaction.response.defer(ephemeral=True)
        try:
            target_id = int(raw_id.strip())
        except ValueError:
            await interaction.followup.send("❌ Discord ID должен быть числом.", ephemeral=True)
            return

        resolved = await self.store.resolve_latest(target_id, reason, interaction.user.id)
        if resolved is None:
            await interaction.followup.send(f"❌ У <@{target_id}> нет активных выговоров.", ephemeral=True)
            return

        remaining = await self.store.active_count(target_id)
        await interaction.followup.send(f"✅ Выговор снят: <@{target_id}> (осталось активных: {remaining})", ephemeral=True)

        embed = build_log_embed(
            "✅ Выговор снят",
            0x2ECC71,
            [
                ("Участник", f"<@{target_id}> (`{target_id}`)"),
                ("Причина выговора", resolved.get("reason") or "—"),
                ("Причина снятия", reason),
                ("Модератор", f"<@{interaction.user.id}>"),
                ("Осталось активных выговоров", str(remaining)),
            ],
        )
        await self.send_log(embed)

    # -- Команда для публикации панели -----------------------------------------

    @app_commands.command(name="аудит_панель", description="Опубликовать панель кадрового аудита в этом канале")
    @app_commands.checks.has_permissions(administrator=True)
    async def post_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📋 Кадровый аудит",
            description=(
                "Управление составом сервера.\n\n"
                "**Понизить** / **Повысить** — движение по обычной лестнице рангов "
                "(Новичок → Фармила 1-3).\n"
                "**Назначить на должность** — ручное назначение на Туллера / Кадровика / Бухгалтера / Смотрящего.\n"
                "**Удалить пользователя дискорда** — кик с подтверждением.\n"
                "**Выдать выговор** / **Снять выговор** — с сохранением истории.\n\n"
                "Все действия попадают в лог-канал аудита."
            ),
            color=0x2C2F33,
        )
        await interaction.response.send_message(embed=embed, view=AuditPanelView(self))


async def setup(bot: commands.Bot):
    await bot.add_cog(Audit(bot))
