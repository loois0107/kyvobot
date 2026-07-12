import discord
from discord.ext import commands

class Starboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.starboard_cache = {}

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.emoji.name != "⭐": return
        
        guild = self.bot.get_guild(payload.guild_id)
        if not guild: return

        settings = await self.bot.get_guild_settings(str(guild.id))
        if not settings.get("starboard_enabled", True): return

        channel = guild.get_channel(payload.channel_id)
        if not channel: return

        guild_locale = str(guild.preferred_locale)
        starboard_channel = discord.utils.get(guild.text_channels, name="starboard")
        if not starboard_channel:
            try:
                msg = self.bot.locale_manager.get(guild_locale, "starboard_err_no_channel")
                await channel.send(msg)
            except Exception:
                pass
            return

        try:
            message = await channel.fetch_message(payload.message_id)
            if message.author.bot: return

            star_reaction = discord.utils.get(message.reactions, emoji="⭐")
            if not star_reaction: return

            if star_reaction.count >= 3:
                if message.id in self.starboard_cache:
                    try:
                        target_msg_id = self.starboard_cache[message.id]
                        star_msg = await starboard_channel.fetch_message(target_msg_id)
                        await star_msg.edit(content=f"⭐ **{star_reaction.count}** | {message.channel.mention}")
                        return
                    except Exception:
                        pass

                fallback_desc = self.bot.locale_manager.get(guild_locale, "starboard_media_fallback")
                embed = discord.Embed(
                    description=message.content if message.content else fallback_desc,
                    color=discord.Color.from_str("#FFAC33"),
                    timestamp=message.created_at
                )
                embed.set_author(name=message.author.name, icon_url=message.author.display_avatar.url)
                
                lbl_context = self.bot.locale_manager.get(guild_locale, "starboard_context_location")
                jump_text = self.bot.locale_manager.get(guild_locale, "starboard_jump_to_message", url=message.jump_url)
                embed.add_field(name=lbl_context, value=jump_text, inline=False)
                
                if message.attachments:
                    embed.set_image(url=message.attachments[0].url)
                    
                embed.set_footer(text=f"Archive ID: {message.id}")
                
                star_msg = await starboard_channel.send(
                    content=f"⭐ **{star_reaction.count}** | {message.channel.mention}", 
                    embed=embed
                )
                self.starboard_cache[message.id] = star_msg.id
        except Exception as e:
            try:
                msg = self.bot.locale_manager.get(guild_locale, "starboard_err_crash", error=str(e))
                await channel.send(msg)
            except Exception:
                pass

async def setup(bot):
    await bot.add_cog(Starboard(bot))
