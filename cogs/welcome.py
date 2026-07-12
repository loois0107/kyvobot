import discord
from discord import app_commands
from discord.ext import commands
import io
import os
import aiohttp
from PIL import Image, ImageDraw, ImageFont

class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.font_path = "Font.ttf"
        self.fonts = {}
        self.preload_fonts()

    def preload_fonts(self):
        if os.path.exists(self.font_path):
            try:
                self.fonts["title"] = ImageFont.truetype(self.font_path, 36)
                self.fonts["sub"] = ImageFont.truetype(self.font_path, 42)
                self.fonts["count"] = ImageFont.truetype(self.font_path, 20)
            except Exception:
                self.set_default_fonts()
        else:
            self.set_default_fonts()

    def set_default_fonts(self):
        default = ImageFont.load_default()
        self.fonts = {"title": default, "sub": default, "count": default}

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        guild_id = str(member.guild.id)
        
        try:
            settings = await self.bot.get_guild_settings(guild_id)
            if settings:
                autorole_id = settings.get("autorole_id")
                if autorole_id:
                    role = member.guild.get_role(int(autorole_id))
                    if role:
                        try:
                            await member.add_roles(role, reason="KYVO AUTOMOD: New member autorole injection.")
                        except discord.Forbidden:
                            pass
        except Exception as e:
            print(f"[AUTOROLE ERROR] {e}")

        try:
            res = self.bot.supabase.table("guild_settings").select("welcome_settings").eq("guild_id", guild_id).execute()
            if not res.data:
                res = self.bot.supabase.table("guild_settings").select("welcome_settings").eq("guild_id", int(guild_id)).execute()
            welcome_set = res.data[0].get("welcome_settings", {}) if res.data else {}
        except Exception as e:
            print(f"[WELCOME DB FETCH ERROR] {e}")
            return

        if not welcome_set.get("enabled", False):
            return

        channel_id = welcome_set.get("channel_id")
        if not channel_id: return

        channel = member.guild.get_channel(int(channel_id))
        if not channel: return

        card_color = welcome_set.get("card_color", "#5865F2")
        card_bg_color = welcome_set.get("card_bg_color", "#1E1F22")
        overlay_opacity = float(welcome_set.get("overlay_opacity", 0.4))
        background_url = str(welcome_set.get("background_url", "")).strip()

        if not card_color.startswith('#'): card_color = f"#{card_color}"
        if not card_bg_color.startswith('#'): card_bg_color = f"#{card_bg_color}"

        base_w, base_h = 920, 240
        card = None

        if background_url and background_url.startswith("http"):
            try:
                # ⚡ MEE6 BYPASS PROTOCOL: 가짜 브라우저 신분증 헤더를 주입해 Unsplash 등의 봇 차단 방어막을 뚫어버림
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(background_url, headers=headers, timeout=5) as resp:
                        if resp.status == 200:
                            bg_data = await resp.read()
                            card = Image.open(io.BytesIO(bg_data)).convert("RGBA").resize((base_w, base_h))
            except Exception as e:
                print(f"[PRESET DOWNLOAD ERROR] Bypass failed: {e}")

        if card is None:
            card = Image.new("RGBA", (base_w, base_h), color=card_bg_color)

        overlay = Image.new("RGBA", card.size, (15, 15, 26, int(overlay_opacity * 255)))
        card = Image.alpha_composite(card.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(card)

        draw.text((240, 55), "WELCOME TO THE SERVER", fill="#b5bac1", font=self.fonts["title"])
        draw.text((240, 100), f"{member.display_name}", fill=card_color, font=self.fonts["sub"])
        draw.text((240, 160), f"Operative #{len(member.guild.members):,}", fill="#ffffff", font=self.fonts["count"])

        draw.ellipse((42, 42, 198, 198), outline=card_color, width=3)

        avatar_url = member.display_avatar.with_format("png").url
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar_url, timeout=4) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        avatar_img = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((150, 150))
                        mask = Image.new("L", (150, 150), 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, 150, 150), fill=255)
                        card.paste(avatar_img, (45, 45), mask=mask)
        except Exception:
            draw.ellipse((45, 45, 195, 195), fill=card_color)

        with io.BytesIO() as image_binary:
            card.save(image_binary, "PNG")
            image_binary.seek(0)
            file = discord.File(fp=image_binary, filename="welcome_card.png")
            
            welcome_embed = discord.Embed(
                title=f"📥 SYSTEM ACCESS GRANTED",
                description=f"Welcome {member.mention} to **{member.guild.name}**!",
                color=discord.Color.from_str(card_color)
            )
            welcome_embed.set_image(url="attachment://welcome_card.png")
            await channel.send(file=file, embed=welcome_embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot: return
        guild_id = str(member.guild.id)
        locale = str(member.guild.preferred_locale)
        try:
            settings = await self.bot.get_guild_settings(guild_id)
            if not settings or not settings.get("goodbye_enabled", False): 
                return

            channel_id = settings.get("goodbye_channel_id")
            if not channel_id: return

            channel = member.guild.get_channel(int(channel_id))
            if not channel: return

            raw_msg = settings.get("goodbye_message", "Has disconnected from the grid.")
            formatted_msg = raw_msg.replace("{username}", member.name)\
                                   .replace("{server}", member.guild.name)\
                                   .replace("{member_count}", str(member.guild.member_count))

            title = self.bot.locale_manager.get(locale, "goodbye_title")
            embed = discord.Embed(
                title=title,
                color=discord.Color.from_str("#FF0055"),
                timestamp=discord.utils.utcnow()
            )
            
            header = self.bot.locale_manager.get(locale, "goodbye_manifest_header")
            lbl_target = self.bot.locale_manager.get(locale, "goodbye_offline_target")
            lbl_pool = self.bot.locale_manager.get(locale, "goodbye_remaining_pool")
            lbl_alert = self.bot.locale_manager.get(locale, "goodbye_alert_system")
            footer_text = self.bot.locale_manager.get(locale, "goodbye_footer")

            manifest_data = (
                f"**{header}**\n\n"
                f"📡 **CLUSTER NODE:** `{member.guild.name}`\n"
                f"{lbl_target} `{member.name}`\n"
                f"{lbl_pool} {member.guild.member_count:,} Active Nodes\n\n"
                f"{lbl_alert}\n"
                f"> {formatted_msg}"
            )
            embed.description = manifest_data
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=footer_text)

            await channel.send(embed=embed)
        except Exception as e:
            print(f"[GOODBYE ERROR] {e}")

async def setup(bot):
    await bot.add_cog(Welcome(bot))
