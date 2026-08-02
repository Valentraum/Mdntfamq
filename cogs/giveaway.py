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
"""

import random
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio

log = logging.getLogger("family-bot.giveaway")

# Храним активные розыгрыши в памяти: {message_id: {"prize":.., "participants": set(), "channel_id":.., "host":..}}
active_giveaways: dict[int, dict] = {}


class GiveawayView(discord.ui.View):
    def __init__(self, message_id: int | None = None):
        super().__init__(timeout=None)  # persistent view — переживает рестарт бота
        self.message_id = message_id

    @discord.ui.button(label="Участвовать", style=discord.ButtonStyle.success, emoji="🎉", custom_id="giveaway:join")
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


class Giveaway(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

    @app_commands.command(name="розыгрыш_завершить", description="Завершить розыгрыш вручную и выбрать победителя")
    @app_commands.describe(id_сообщения="ID сообщения с розыгрышем (ПКМ по сообщению -> Копировать ID)")
    async def giveaway_end_cmd(self, interaction: discord.Interaction, id_сообщения: str):
        if not id_сообщения.isdigit():
            await interaction.response.send_message("❌ ID сообщения должен быть числом.", ephemeral=True)
            return
        message_id = int(id_сообщения)
        if message_id not in active_giveaways:
            await interaction.response.send_message("❌ Активный розыгрыш с таким ID не найден.", ephemeral=True)
            return
        await interaction.response.send_message("Завершаю розыгрыш…", ephemeral=True)
        await finish_giveaway(self.bot, message_id)


async def setup(bot: commands.Bot):
    bot.add_view(GiveawayView())  # регистрируем persistent view для кнопки
    await bot.add_cog(Giveaway(bot))
