import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
import re
import time
import asyncio
import logging
from dotenv import load_dotenv
from database import (
    init_db, set_config, get_config,
    create_deployment, get_active_deployment, get_any_deployment,
    get_all_active_deployments, deactivate_deployment,
    update_deployment_expiry, get_all_deployments_for_user,
    get_suspended_deployment, delete_deployment
)

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"), override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

RADIANT_SKY   = 0xFF8C00
NIGHT_MOSS    = 0x2E7D52
FROST_RED     = 0xEF5350
GLASS_GOLD    = 0xFFD54F
SOFT_TEAL     = 0x26C6DA
GLASS_PURPLE  = 0x7E57C2
GLASS_NEUTRAL = 0x37474F

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

active_timers: dict[int, asyncio.Task] = {}
pending_deletions: dict[int, asyncio.Task] = {}

DELETE_AFTER_SECONDS = 7 * 86400

def parse_duration(duration_str: str) -> int | None:
    pattern = re.compile(r"(\d+)(mo|[smhdy])")
    matches = pattern.findall(duration_str.lower())
    if not matches:
        return None
    total = 0
    used_length = 0
    for value, unit in matches:
        v = int(value)
        if unit == "s":
            total += v
        elif unit == "m":
            total += v * 60
        elif unit == "h":
            total += v * 3600
        elif unit == "d":
            total += v * 86400
        elif unit == "mo":
            total += v * 86400 * 30
        elif unit == "y":
            total += v * 86400 * 365
        used_length += len(value) + len(unit)
    if used_length != len(duration_str.lower().replace(" ", "")):
        return None
    return total if total > 0 else None

def format_duration(seconds: int) -> str:
    if seconds >= 86400 * 365:
        v = seconds // (86400 * 365)
        return f"{v} year{'s' if v > 1 else ''}"
    if seconds >= 86400 * 30:
        v = seconds // (86400 * 30)
        return f"{v} month{'s' if v > 1 else ''}"
    if seconds >= 86400:
        v = seconds // 86400
        return f"{v} day{'s' if v > 1 else ''}"
    if seconds >= 3600:
        v = seconds // 3600
        return f"{v} hour{'s' if v > 1 else ''}"
    if seconds >= 60:
        v = seconds // 60
        return f"{v} minute{'s' if v > 1 else ''}"
    return f"{seconds} second{'s' if seconds > 1 else ''}"

def ios_embed(title: str, description: str, color: int, footer: str = "Deploy Manager • iOS 26") -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=footer)
    return embed

async def send_deletion(deployment_id: int, user_id: int, deployer_id: int, product_name: str, guild_id: int):
    await asyncio.sleep(DELETE_AFTER_SECONDS)
    await delete_deployment(deployment_id)
    if deployment_id in pending_deletions:
        del pending_deletions[deployment_id]
    channel_id = await get_config(f"notify_channel_{guild_id}")
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    deployer = bot.get_user(deployer_id) or await bot.fetch_user(deployer_id)
    del_embed = ios_embed(
        "🗑️  Product Deleted",
        f"**`{product_name}`** for **{user.mention}** has been permanently deleted after 7 days in suspension.",
        GLASS_NEUTRAL
    )
    if channel_id:
        channel = bot.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=del_embed)
    try:
        await user.send(embed=ios_embed(
            "🗑️  Product Deleted",
            f"Your suspended product **`{product_name}`** has been permanently deleted after 7 days.",
            GLASS_NEUTRAL
        ))
    except discord.Forbidden:
        pass
    try:
        await deployer.send(embed=ios_embed(
            "🗑️  Product Deleted",
            f"**`{product_name}`** for **{user.name}** has been permanently deleted after 7 days in suspension.",
            GLASS_NEUTRAL
        ))
    except discord.Forbidden:
        pass

def schedule_deletion(deployment_id: int, user_id: int, deployer_id: int, product_name: str, guild_id: int):
    if deployment_id in pending_deletions:
        pending_deletions[deployment_id].cancel()
    task = asyncio.create_task(
        send_deletion(deployment_id, user_id, deployer_id, product_name, guild_id)
    )
    pending_deletions[deployment_id] = task

async def send_expiry(deployment_id: int, user_id: int, deployer_id: int, product_name: str, guild_id: int, delay: float):
    await asyncio.sleep(delay)
    async with __import__("aiosqlite").connect("deployments.db") as db:
        async with db.execute("SELECT active FROM deployments WHERE id = ?", (deployment_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or row[0] == 0:
                return
    await deactivate_deployment(deployment_id)
    channel_id = await get_config(f"notify_channel_{guild_id}")
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    deployer = bot.get_user(deployer_id) or await bot.fetch_user(deployer_id)
    expiry_embed = ios_embed(
        "📦 Deployment Expired",
        f"**`{product_name}`** for **{user.mention}** has been **Undeployed**.\n🔔 Ping: {deployer.mention}",
        NIGHT_MOSS
    )
    if channel_id:
        channel = bot.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=expiry_embed, content=f"{deployer.mention}")
    dm_embed = ios_embed(
        "⏰ Deployment Expired",
        f"Your **`{product_name}`** has expired and been undeployed.\n⚠️ It will be permanently deleted in **7 days** if not redeployed.",
        NIGHT_MOSS
    )
    dm_deployer_embed = ios_embed(
        "⏰ Deployment Expired",
        f"**`{product_name}`** deployed for **{user.name}** has expired and been undeployed.\n⚠️ It will be permanently deleted in **7 days** if not redeployed.",
        NIGHT_MOSS
    )
    try:
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    try:
        await deployer.send(embed=dm_deployer_embed)
    except discord.Forbidden:
        pass
    if deployment_id in active_timers:
        del active_timers[deployment_id]
    schedule_deletion(deployment_id, user_id, deployer_id, product_name, guild_id)

def schedule_deployment(deployment_id: int, user_id: int, deployer_id: int, product_name: str, guild_id: int, expires_at: float):
    delay = expires_at - time.time()
    if delay <= 0:
        asyncio.create_task(deactivate_deployment(deployment_id))
        return
    task = asyncio.create_task(
        send_expiry(deployment_id, user_id, deployer_id, product_name, guild_id, delay)
    )
    active_timers[deployment_id] = task

@bot.event
async def on_ready():
    await init_db()
    await tree.sync()
    logger.info(f"Logged in as {bot.user} | Synced slash commands")
    deployments = await get_all_active_deployments()
    for dep in deployments:
        dep_id, product, user_id, deployer_id, guild_id, expires_at, active = dep
        if expires_at <= time.time():
            await deactivate_deployment(dep_id)
            schedule_deletion(dep_id, user_id, deployer_id, product, guild_id)
        else:
            schedule_deployment(dep_id, user_id, deployer_id, product, guild_id, expires_at)
    logger.info(f"Restored {len(deployments)} active deployment(s)")

def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            embed = ios_embed(
                "🔒 Access Restricted",
                "You do not have permission to use this command.\nAdministrator privileges are required.",
                FROST_RED
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

@tree.command(name="help", description="View all available commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐  Deploy Manager  ·  Command Center",
        description="iOS 26 — Glassmorphism UI  •  Production Ready",
        color=RADIANT_SKY
    )
    embed.add_field(
        name="⚙️  Configuration",
        value=(
            "`/setchannel` `#channel`\n"
            "↳ Set the notification channel for deployment events."
        ),
        inline=False
    )
    embed.add_field(
        name="🚀  Deployment",
        value=(
            "`/deploy` `product` `@user` `duration`\n"
            "↳ Deploy a product to a user for a set duration.\n\n"
            "`/redeploy` `@user` `product` `duration`\n"
            "↳ Reset/extend a deployment timer.\n\n"
            "`/suspend` `@user` `product`\n"
            "↳ Immediately terminate an active deployment.\n\n"
            "`/deleteproduct` `@user` `product`\n"
            "↳ Permanently delete a **suspended** product immediately.\n\n"
            "`/listproduct` `@user`\n"
            "↳ View all active and suspended products for a user."
        ),
        inline=False
    )
    embed.add_field(
        name="🗑️  Auto-Deletion",
        value=(
            "Products suspended (manually or on expiry) are automatically\n"
            "**permanently deleted after 7 days**. Both the admin and the\n"
            "user will receive a DM and a channel notification."
        ),
        inline=False
    )
    embed.add_field(
        name="⏱️  Duration Format",
        value="`s` seconds  •  `m` minutes  •  `h` hours\n`d` days  •  `mo` months  •  `y` years\n\nExample: `2h30m`, `1d`, `3mo`",
        inline=False
    )
    embed.set_footer(text="Deploy Manager • iOS 26 Design System  |  Admin commands require Administrator")
    await interaction.response.send_message(embed=embed)

@tree.command(name="setchannel", description="Set the notification channel for deployment events")
@is_admin()
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    await set_config(f"notify_channel_{interaction.guild_id}", str(channel.id))
    embed = ios_embed(
        "✅  Notification Channel Set",
        f"All deployment events will now be sent to {channel.mention}.",
        SOFT_TEAL
    )
    await interaction.response.send_message(embed=embed)

@tree.command(name="deploy", description="Deploy a product to a user for a duration")
@is_admin()
async def deploy(interaction: discord.Interaction, product_name: str, user: discord.Member, duration: str):
    seconds = parse_duration(duration)
    if seconds is None:
        embed = ios_embed(
            "⚠️  Invalid Duration",
            f"**`{duration}`** is not a valid duration.\n\n**Correct format:** `s`, `m`, `h`, `d`, `mo`, `y`\n**Examples:** `30m`, `2h`, `7d`, `1mo`, `2h30m`",
            FROST_RED
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    existing = await get_active_deployment(user.id, product_name)
    if existing:
        embed = ios_embed(
            "⚠️  Already Deployed",
            f"**`{product_name}`** is already active for **{user.mention}**.\nUse `/suspend` first or `/redeploy` to reset the timer.",
            GLASS_GOLD
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    expires_at = time.time() + seconds
    dep_id = await create_deployment(product_name, user.id, interaction.user.id, interaction.guild_id, expires_at)
    schedule_deployment(dep_id, user.id, interaction.user.id, product_name, interaction.guild_id, expires_at)
    human_duration = format_duration(seconds)
    confirm_embed = ios_embed(
        "🚀  Deployment Initiated",
        f"**Product:** `{product_name}`\n**User:** {user.mention}\n**Duration:** `{human_duration}`\n**Expires:** <t:{int(expires_at)}:R>",
        RADIANT_SKY
    )
    await interaction.response.send_message(embed=confirm_embed)
    try:
        await user.send(embed=ios_embed(
            "📦  You Received a Deployment",
            f"You have been given **`{product_name}`** for **{human_duration}**.\nDeployed by: **{interaction.user.name}**",
            GLASS_PURPLE
        ))
    except discord.Forbidden:
        pass
    try:
        await interaction.user.send(embed=ios_embed(
            "✅  Deployment Confirmed",
            f"You deployed **`{product_name}`** to **{user.name}** for **{human_duration}**.",
            NIGHT_MOSS
        ))
    except discord.Forbidden:
        pass

@tree.command(name="suspend", description="Force-terminate an active deployment immediately")
@is_admin()
async def suspend(interaction: discord.Interaction, user: discord.Member, product_name: str):
    existing = await get_active_deployment(user.id, product_name)
    if not existing:
        embed = ios_embed(
            "❌  No Active Deployment",
            f"No active deployment found for **`{product_name}`** on **{user.mention}**.",
            FROST_RED
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    dep_id = existing[0]
    deployer_id = existing[3]
    guild_id = existing[4]
    if dep_id in active_timers:
        active_timers[dep_id].cancel()
        del active_timers[dep_id]
    await deactivate_deployment(dep_id)
    try:
        deployer = bot.get_user(deployer_id) or await bot.fetch_user(deployer_id)
    except Exception:
        deployer = None
    channel_id = await get_config(f"notify_channel_{guild_id}")
    suspend_embed = ios_embed(
        "🛑  Deployment Suspended",
        f"**`{product_name}`** for **{user.mention}** has been forcefully terminated by **{interaction.user.name}**.\n"
        + (f"🔔 Ping: {deployer.mention}\n" if deployer else "")
        + "⚠️ Will be permanently deleted in **7 days**.",
        FROST_RED
    )
    if channel_id:
        channel = bot.get_channel(int(channel_id))
        if channel:
            await channel.send(
                embed=suspend_embed,
                content=deployer.mention if deployer else None
            )
    await interaction.response.send_message(embed=ios_embed(
        "🛑  Suspended",
        f"**`{product_name}`** for **{user.mention}** has been suspended.\n⚠️ It will be permanently deleted in **7 days**.",
        FROST_RED
    ))
    try:
        await user.send(embed=ios_embed(
            "🛑  Deployment Suspended",
            f"Your **`{product_name}`** has been suspended by **{interaction.user.name}**.\n⚠️ It will be permanently deleted in **7 days** if not redeployed.",
            FROST_RED
        ))
    except discord.Forbidden:
        pass
    if deployer:
        try:
            await deployer.send(embed=ios_embed(
                "🛑  Deployment Suspended",
                f"**`{product_name}`** for **{user.name}** was suspended by **{interaction.user.name}**.\n⚠️ It will be permanently deleted in **7 days** if not redeployed.",
                FROST_RED
            ))
        except discord.Forbidden:
            pass
    schedule_deletion(dep_id, user.id, deployer_id, product_name, guild_id)

@tree.command(name="deleteproduct", description="Permanently delete a product (active or suspended) for a user")
@is_admin()
async def deleteproduct(interaction: discord.Interaction, user: discord.Member, product_name: str):
    existing = await get_any_deployment(user.id, product_name)
    if not existing:
        embed = ios_embed(
            "❌  No Deployment Found",
            f"No deployment found for **`{product_name}`** on **{user.mention}**.",
            FROST_RED
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    dep_id = existing[0]
    deployer_id = existing[3]
    guild_id = existing[4]
    if dep_id in active_timers:
        active_timers[dep_id].cancel()
        del active_timers[dep_id]
    if dep_id in pending_deletions:
        pending_deletions[dep_id].cancel()
        del pending_deletions[dep_id]
    await delete_deployment(dep_id)
    try:
        deployer = bot.get_user(deployer_id) or await bot.fetch_user(deployer_id)
    except Exception:
        deployer = None
    del_embed = ios_embed(
        "🗑️  Product Deleted",
        f"**`{product_name}`** for **{user.mention}** has been permanently deleted by **{interaction.user.name}**.",
        GLASS_NEUTRAL
    )
    channel_id = await get_config(f"notify_channel_{guild_id}")
    if channel_id:
        channel = bot.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=del_embed)
    await interaction.response.send_message(embed=ios_embed(
        "🗑️  Product Deleted",
        f"**`{product_name}`** for **{user.mention}** has been permanently deleted.",
        GLASS_NEUTRAL
    ))
    try:
        await user.send(embed=ios_embed(
            "🗑️  Product Deleted",
            f"Your product **`{product_name}`** has been permanently deleted by **{interaction.user.name}**.",
            GLASS_NEUTRAL
        ))
    except discord.Forbidden:
        pass
    if deployer:
        try:
            await deployer.send(embed=ios_embed(
                "🗑️  Product Deleted",
                f"**`{product_name}`** for **{user.name}** was permanently deleted by **{interaction.user.name}**.",
                GLASS_NEUTRAL
            ))
        except discord.Forbidden:
            pass

@tree.command(name="redeploy", description="Reset or extend the timer for an existing deployment")
@is_admin()
async def redeploy(interaction: discord.Interaction, user: discord.Member, product_name: str, duration: str):
    seconds = parse_duration(duration)
    if seconds is None:
        embed = ios_embed(
            "⚠️  Invalid Duration",
            f"**`{duration}`** is not a valid duration.\n\n**Correct format:** `s`, `m`, `h`, `d`, `mo`, `y`\n**Examples:** `30m`, `2h`, `7d`, `1mo`",
            FROST_RED
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    existing = await get_any_deployment(user.id, product_name)
    if not existing:
        expires_at = time.time() + seconds
        dep_id = await create_deployment(product_name, user.id, interaction.user.id, interaction.guild_id, expires_at)
        schedule_deployment(dep_id, user.id, interaction.user.id, product_name, interaction.guild_id, expires_at)
    else:
        dep_id = existing[0]
        if dep_id in active_timers:
            active_timers[dep_id].cancel()
            del active_timers[dep_id]
        if dep_id in pending_deletions:
            pending_deletions[dep_id].cancel()
            del pending_deletions[dep_id]
        expires_at = time.time() + seconds
        await update_deployment_expiry(dep_id, expires_at)
        schedule_deployment(dep_id, user.id, interaction.user.id, product_name, interaction.guild_id, expires_at)
    human_duration = format_duration(seconds)
    confirm_embed = ios_embed(
        "🔄  Redeployment Confirmed",
        f"**Product:** `{product_name}`\n**User:** {user.mention}\n**New Duration:** `{human_duration}`\n**Expires:** <t:{int(expires_at)}:R>",
        GLASS_GOLD
    )
    await interaction.response.send_message(embed=confirm_embed)
    try:
        await user.send(embed=ios_embed(
            "🔄  Deployment Renewed",
            f"Your **`{product_name}`** has been redeployed by **{interaction.user.name}** for **{human_duration}**.",
            GLASS_PURPLE
        ))
    except discord.Forbidden:
        pass
    try:
        await interaction.user.send(embed=ios_embed(
            "✅  Redeploy Confirmed",
            f"You redeployed **`{product_name}`** for **{user.name}** for **{human_duration}**.",
            NIGHT_MOSS
        ))
    except discord.Forbidden:
        pass

@tree.command(name="listproduct", description="View all Active and Suspended products for a user")
async def listproduct(interaction: discord.Interaction, user: discord.Member):
    rows = await get_all_deployments_for_user(user.id)
    active_lines = []
    suspended_lines = []
    for product_name, active, expires_at in rows:
        if active == 1:
            active_lines.append(f"🟢 **`{product_name}`**  •  Expires <t:{int(expires_at)}:R>")
        else:
            suspended_lines.append(f"🔴 **`{product_name}`**")
    embed = discord.Embed(
        title=f"📋  Product List  ·  {user.display_name}",
        description=f"Deployment overview for {user.mention}",
        color=RADIANT_SKY
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    if active_lines:
        embed.add_field(
            name=f"✅  Active  ({len(active_lines)})",
            value="\n".join(active_lines),
            inline=False
        )
    else:
        embed.add_field(name="✅  Active  (0)", value="No product", inline=False)
    if suspended_lines:
        embed.add_field(
            name=f"🛑  Suspended  ({len(suspended_lines)})",
            value="\n".join(suspended_lines),
            inline=False
        )
    else:
        embed.add_field(name="🛑  Suspended  (0)", value="No product", inline=False)
    embed.set_footer(text="Deploy Manager • iOS 26")
    await interaction.response.send_message(embed=embed)

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    logger.error(f"Slash command error: {error}")
    embed = ios_embed(
        "⚠️  Something Went Wrong",
        f"An unexpected error occurred:\n```{str(error)[:300]}```",
        FROST_RED
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError(".env file not found or DISCORD_TOKEN is missing/empty")
bot.run(token)
