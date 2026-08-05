"""
Cog: Giveaway (розыгрыши)
--------------------------
Команда /розыгрыш создаёт сообщение с призом и кнопкой "🎉 Участвовать".
Люди жмут кнопку, чтобы записаться (без реакций — так надёжнее и участник
не может проголосовать 2 раза).

Завершение розыгрыша:
  - Автоматически, через N минут (параметр длительность в команде)
  - Или вручную командой /розыгрыш_завершить (по ID сообщения)

Победитель выбирается так: генерируется случайное число от 1 до количества
участников, и по этому числу берётся участник из списка (как лотерейный билет).

Новые возможности:
  /участники_розыгрыша id_сообщения
      → Показывает список участников (доступно всем, ephemeral).
        Хост видит дополнительные кнопки: «Добавить участника» и «Удалить участника».

  Редактирование участников доступно ТОЛЬКО хосту розыгрыша.
"""

import random
import logging

import discord
from discord import app_commands
from discord.ext import commands
import asyncio

log = logging.getLogger("family-bot.giveaway")

# Храним активные розыгрыши в памяти:
# {message_id: {"prize":.., "participants": set(), "channel_id":.., "host":..}}
active_giveaways: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Persistent view — кнопка "Участвовать" на сообщении розыгрыша
# ---------------------------------------------------------------------------

class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent — переживает рестарт бота

    @discord.ui.button(
        label="Участвовать",
        style=discord.ButtonStyle.success,
        emoji="🎉",
        custom_id="giveaway:join",
    )
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        giveaway = active_giveaways.get(interaction.message.id)
        if giveaway is None:
            await interaction.response.send_message("Этот розыгрыш уже завершён.", ephemeral=True)
            return

        if interaction.user.id in giveaway["participants"]:
            giveaway["participants"].discard(interaction.user.id)
            await interaction.response.send_message("Ты вышел из розыгрыша.", ephemeral=True)
        else:
            giveaway["participants"].add(interaction.user.id)
            await interaction.response.send_message("✅ Ты участвуешь в розыгрыше!", ephemeral=True)

        await update_giveaway_embed(interaction.client, interaction.message.id)


# ---------------------------------------------------------------------------
# View управления участниками — показывается хосту через /участники_розыгрыша
# ---------------------------------------------------------------------------

class ParticipantsManageView(discord.ui.View):
    """Кнопки «Добавить» и «Удалить» участника — видит только хост."""

    def __init__(self, message_id: int, host_id: int):
        super().__init__(timeout=120)
        self.message_id = message_id
        self.host_id = host_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "❌ Управлять участниками может только создатель розыгрыша.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="➕ Добавить участника", style=discord.ButtonStyle.primary)
    async def add_participant(self, interaction: discord.Interaction, button: discord.ui.Button):
        giveaway = active_giveaways.get(self.message_id)
        if giveaway is None:
            await interaction.response.send_message("Розыгрыш уже завершён.", ephemeral=True)
            return
        await interaction.response.send_modal(AddParticipantModal(self.message_id))

    @discord.ui.button(label="➖ Удалить участника", style=discord.ButtonStyle.danger)
    async def remove_participant(self, interaction: discord.Interaction, button: discord.ui.Button):
        giveaway = active_giveaways.get(self.message_id)
        if giveaway is None:
            await interaction.response.send_message("Розыгрыш уже завершён.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveParticipantModal(self.message_id))


# ---------------------------------------------------------------------------
# Модальные окна для добавления / удаления участника по ID
# ---------------------------------------------------------------------------

class AddParticipantModal(discord.ui.Modal, title="Добавить участника"):
    user_id_input = discord.ui.TextInput(
        label="ID пользователя",
        placeholder="Например: 123456789012345678",
        min_length=17,
        max_length=20,
    )

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_id_input.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message("❌ ID должен быть числом.", ephemeral=True)
            return

        user_id = int(raw)
        giveaway = active_giveaways.get(self.message_id)
        if giveaway is None:
            await interaction.response.send_message("Розыгрыш уже завершён.", ephemeral=True)
            return

        if user_id in giveaway["participants"]:
            await interaction.response.send_message(
                f"ℹ️ <@{user_id}> уже участвует в розыгрыше.", ephemeral=True
            )
            return

        giveaway["participants"].add(user_id)
        await update_giveaway_embed(interaction.client, self.message_id)
        await interaction.response.send_message(
            f"✅ <@{user_id}> добавлен в розыгрыш. Итого участников: {len(giveaway['participants'])}.",
            ephemeral=True,
        )


class RemoveParticipantModal(discord.ui.Modal, title="Удалить участника"):
    user_id_input = discord.ui.TextInput(
        label="ID пользователя",
        placeholder="Например: 123456789012345678",
        min_length=17,
        max_length=20,
    )

    def __init__(self, message_id: int):
        super().__init__()
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_id_input.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message("❌ ID должен быть числом.", ephemeral=True)
            return

        user_id = int(raw)
        giveaway = active_giveaways.get(self.message_id)
        if giveaway is None:
            await interaction.response.send_message("Розыгрыш уже завершён.", ephemeral=True)
            return

        if user_id not in giveaway["participants"]:
            await interaction.response.send_message(
                f"ℹ️ <@{user_id}> не найден среди участников.", ephemeral=True
            )
            return

        giveaway["participants"].discard(user_id)
        await update_giveaway_embed(interaction.client, self.message_id)
        await interaction.response.send_message(
            f"🗑️ <@{user_id}> удалён из розыгрыша. Итого участников: {len(giveaway['participants'])}.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

async def update_giveaway_embed(bot: commands.Bot, message_id: int):
    giveaway = active_giveaways.get(message_id)
    if not giveaway:
        return
    channel = bot.get_channel(giveaway["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return

    embed = message.embeds[0]
    embed.set_field_at(0, name="Участников", value=str(len(giveaway["participants"])))
    await message.edit(embed=embed)


async def finish_giveaway(bot: commands.Bot, message_id: int):
    giveaway = active_giveaways.pop(message_id, None)
    if giveaway is None:
        return

    channel = bot.get_channel(giveaway["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        message = None

    participants = list(giveaway["participants"])

    if not participants:
        result_text = f"🎉 Розыгрыш **{giveaway['prize']}** завершён — но участников не было."
        await channel.send(result_text)
    else:
        winning_number = random.randint(1, len(participants))
        winner_id = participants[winning_number - 1]
        result_text = (
            f"🎉 Розыгрыш **{giveaway['prize']}** завершён!\n"
            f"Участников: {len(participants)}. Выпало число: **{winning_number}**.\n"
            f"Победитель: <@{winner_id}> 🏆"
        )
        await channel.send(result_text)

    if message:
        try:
            embed = message.embeds[0]
            embed.title = f"🎉 [ЗАВЕРШЁН] {embed.title.replace('🎉 ', '')}"
            embed.color = discord.Color.dark_grey()
            await message.edit(embed=embed, view=None)
        except Exception:
            log.exception("Не удалось обновить сообщение завершённого розыгрыша")


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Giveaway(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # /розыгрыш
    # ------------------------------------------------------------------
    @app_commands.command(name="розыгрыш", description="Запустить розыгрыш с кнопкой участия")
    @app_commands.describe(
        приз="Что разыгрываем",
        минуты="Через сколько минут розыгрыш завершится автоматически (0 = без автозавершения)",
    )
    async def giveaway_cmd(self, interaction: discord.Interaction, приз: str, минуты: int = 10):
        embed = discord.Embed(
            title=f"🎉 Розыгрыш: {приз}",
            description="Нажми **Участвовать**, чтобы принять участие!",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Участников", value="0")
        if минуты > 0:
            embed.set_footer(text=f"Автозавершение через {минуты} мин.")

        view = GiveawayView()
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()

        active_giveaways[message.id] = {
            "prize": приз,
            "participants": set(),
            "channel_id": interaction.channel_id,
            "host": interaction.user.id,
        }

        if минуты > 0:
            async def auto_finish():
                await asyncio.sleep(минуты * 60)
                await finish_giveaway(self.bot, message.id)

            self.bot.loop.create_task(auto_finish())

    # ------------------------------------------------------------------
    # /розыгрыш_завершить
    # ------------------------------------------------------------------
    @app_commands.command(
        name="розыгрыш_завершить",
        description="Завершить розыгрыш вручную и выбрать победителя",
    )
    @app_commands.describe(id_сообщения="ID сообщения с розыгрышем (ПКМ → Копировать ID)")
    async def giveaway_end_cmd(self, interaction: discord.Interaction, id_сообщения: str):
        if not id_сообщения.isdigit():
            await interaction.response.send_message("❌ ID сообщения должен быть числом.", ephemeral=True)
            return
        message_id = int(id_сообщения)
        if message_id not in active_giveaways:
            await interaction.response.send_message(
                "❌ Активный розыгрыш с таким ID не найден.", ephemeral=True
            )
            return
        await interaction.response.send_message("Завершаю розыгрыш…", ephemeral=True)
        await finish_giveaway(self.bot, message_id)

    # ------------------------------------------------------------------
    # /участники_розыгрыша
    # ------------------------------------------------------------------
    @app_commands.command(
        name="участники_розыгрыша",
        description="Показать список участников розыгрыша (хост может добавлять/удалять)",
    )
    @app_commands.describe(id_сообщения="ID сообщения с розыгрышем (ПКМ → Копировать ID)")
    async def participants_cmd(self, interaction: discord.Interaction, id_сообщения: str):
        if not id_сообщения.isdigit():
            await interaction.response.send_message("❌ ID сообщения должен быть числом.", ephemeral=True)
            return

        message_id = int(id_сообщения)
        giveaway = active_giveaways.get(message_id)
        if giveaway is None:
            await interaction.response.send_message(
                "❌ Активный розыгрыш с таким ID не найден.", ephemeral=True
            )
            return

        participants = list(giveaway["participants"])
        is_host = interaction.user.id == giveaway["host"]

        embed = discord.Embed(
            title=f"👥 Участники розыгрыша: {giveaway['prize']}",
            color=discord.Color.blurple(),
        )

        if not participants:
            embed.description = "Участников пока нет."
        else:
            # Разбиваем на чанки по 30 упоминаний, чтобы не переполнить embed
            chunks = [participants[i : i + 30] for i in range(0, len(participants), 30)]
            for idx, chunk in enumerate(chunks, start=1):
                field_name = "Список" if len(chunks) == 1 else f"Список (часть {idx})"
                lines = [f"{n}. <@{uid}>" for n, uid in enumerate(chunk, start=(idx - 1) * 30 + 1)]
                embed.add_field(name=field_name, value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Всего участников: {len(participants)}")

        # Хост видит кнопки управления, остальные — только список
        if is_host:
            view = ParticipantsManageView(message_id=message_id, host_id=giveaway["host"])
            note = "\n\n*(Ты — хост. Используй кнопки ниже для управления участниками.)*"
            embed.description = (embed.description or "") + note
        else:
            view = None

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot):
    bot.add_view(GiveawayView())  # регистрируем persistent view для кнопки "Участвовать"
    await bot.add_cog(Giveaway(bot))
