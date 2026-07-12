import discord
from discord import app_commands
from discord.ext import commands
import asyncio

class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.member and payload.member.bot: return
        
        from main import supabase
        if not supabase: return

        emoji_str = str(payload.emoji)
        message_id_str = str(payload.message_id)

        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda: supabase.table("reaction_roles")
                .select("role_id")
                .eq("message_id", message_id_str)
                .eq("emoji", emoji_str)
                .execute()
            )

            if res.data:
                role_id = int(res.data[0]["role_id"])
                guild = self.bot.get_guild(payload.guild_id)
                if not guild: return

                role = guild.get_role(role_id)
                member = payload.member or await guild.fetch_member(payload.user_id)
                
                if role and member and not member.bot:
                    await member.add_roles(role, reason="KYVO REACTION_ROLES: Emoji trigger injection.")
        except Exception as e:
            print(f"[CRITICAL ERROR] Raw reaction add pipeline crashed: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        from main import supabase
        if not supabase: return

        emoji_str = str(payload.emoji)
        message_id_str = str(payload.message_id)

        try:
            res = await asyncio.get_event_loop().run_in_executor(
                None, lambda: supabase.table("reaction_roles")
                .select("role_id")
                .eq("message_id", message_id_str)
                .eq("emoji", emoji_str)
                .execute()
            )

            if res.data:
                role_id = int(res.data[0]["role_id"])
                guild = self.bot.get_guild(payload.guild_id)
                if not guild: return

                role = guild.get_role(role_id)
                member = await guild.fetch_member(payload.user_id)
                
                if role and member and not member.bot:
                    await member.remove_roles(role, reason="KYVO REACTION_ROLES: Emoji trigger extraction.")
        except Exception as e:
            print(f"[CRITICAL ERROR] Raw reaction remove pipeline crashed: {e}")

    @app_commands.command(name="reaction_role_add", description="Bind an emoji reaction role payload to a specific message.")
    @app_commands.checks.has_permissions(administrator=True)
    async def rr_add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        from main import supabase
        if not supabase:
            msg = self.bot.locale_manager.get(str(interaction.locale), "rr_system_fault")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            try:
                msg_id = int(message_id)
                await interaction.channel.fetch_message(msg_id)
            except:
                msg = self.bot.locale_manager.get(str(interaction.locale), "rr_msg_not_found")
                await interaction.followup.send(msg, ephemeral=True)
                return

            payload = {
                "guild_id": str(interaction.guild_id),
                "message_id": str(message_id),
                "emoji": str(emoji),
                "role_id": str(role.id)
            }

            await asyncio.get_event_loop().run_in_executor(
                None, lambda: supabase.table("reaction_roles").upsert(payload).execute()
            )

            target_msg = await interaction.channel.fetch_message(msg_id)
            await target_msg.add_reaction(emoji)

            success_msg = self.bot.locale_manager.get(str(interaction.locale), "rr_success", message_id=message_id, emoji=emoji, role_name=role.name)
            await interaction.followup.send(success_msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ `{str(e)}`", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
