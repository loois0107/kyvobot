import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import random
from datetime import timedelta

class VerificationView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Verify / Accept Rules", style=discord.ButtonStyle.success, emoji="✅", custom_id="verify_rules_btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user
        
        settings = await self.bot.get_guild_settings(str(guild.id))
        role_name = settings.get("verify_role_name", "Member")
        
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(name=role_name, reason="Auto-created verification access level role")
            except discord.Forbidden:
                await interaction.followup.send("❌ Permission layer exception. I cannot manage or compile roles.", ephemeral=True)
                return
        
        if role in member.roles:
            await interaction.followup.send("⚠️ Identity node already authorized and verified.", ephemeral=True)
        else:
            try:
                await member.add_roles(role)
                await interaction.followup.send("✅ Verification transaction successful! Welcome to the server framework.", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send("❌ Hierarchy conflict error. Move my bot role to the top tier.", ephemeral=True)

class RoleDropdown(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Announcements", description="Subscribe to network platform updates.", emoji="📢", value="Announcements Pings"),
            discord.SelectOption(label="Events", description="Subscribe to scheduled community activities.", emoji="🎉", value="Event Pings"),
            discord.SelectOption(label="Giveaways", description="Subscribe to active lottery reward notifications.", emoji="🎁", value="Giveaway Pings")
        ]
        super().__init__(placeholder="Select notification parameters...", min_values=0, max_values=3, options=options, custom_id="self_role_dropdown")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user
        
        available_role_names = ["Announcements Pings", "Event Pings", "Giveaway Pings"]
        
        for r_name in available_role_names:
            if not discord.utils.get(guild.roles, name=r_name):
                try:
                    await guild.create_role(name=r_name, reason="Auto-created dynamic self assignment tier")
                except discord.Forbidden:
                    await interaction.followup.send("❌ Lacking administrative credentials to allocate roles.", ephemeral=True)
                    return

        available_roles = [discord.utils.get(guild.roles, name=n) for n in available_role_names]
        selected_roles = [discord.utils.get(guild.roles, name=v) for v in self.values]

        roles_to_add = [r for r in selected_roles if r and r not in member.roles]
        roles_to_remove = [r for r in available_roles if r and r in member.roles and r not in selected_roles]

        try:
            if roles_to_add: await member.add_roles(*roles_to_add)
            if roles_to_remove: await member.remove_roles(*roles_to_remove)
            await interaction.followup.send("⚙️ Profile interest preferences updated successfully!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("❌ Failed to process roles. Check system hierarchy permissions.", ephemeral=True)

class SelfRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleDropdown())

class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join Giveaway", style=discord.ButtonStyle.success, emoji="🎉", custom_id="join_giveaway_btn")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id
        if msg_id not in interaction.client.giveaways:
            await interaction.response.send_message("❌ This giveaway pool instance has expired or terminated.", ephemeral=True)
            return
        
        participants = interaction.client.giveaways[msg_id]
        if interaction.user.id in participants:
            participants.remove(interaction.user.id)
            await interaction.response.send_message("🔓 Evacuated pool. You removed yourself from this lottery record.", ephemeral=True)
        else:
            participants.add(interaction.user.id)
            await interaction.response.send_message("🎉 Registration success! You entered the active giveaway tracking array.", ephemeral=True)

class TicketButtonView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="open_ticket_btn")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        
        settings = await self.bot.get_guild_settings(str(guild.id))
        category_id = settings.get("ticket_category_id")
        
        category = None
        if category_id:
            try: category = guild.get_channel(int(category_id))
            except: pass
            
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False), 
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True), 
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        
        try:
            ticket_channel = await guild.create_text_channel(name=f"ticket-{user.name.lower()}", category=category, overwrites=overwrites)
            embed = discord.Embed(title="Support Session Activated", description=f"Welcome {user.mention},\nStaff will handle your request shortly. Use the interface node underneath to delete this channel context.", color=discord.Color.blue())
            await ticket_channel.send(embed=embed, view=TicketCloseView())
            await interaction.followup.send(f"✅ Ticket created successfully: {ticket_channel.mention}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Operation failure during dynamic room construction: {e}", ephemeral=True)

class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Inquiry sequence closing down. Terminal instance will delete in 5 seconds...", ephemeral=False)
        await asyncio.sleep(5)
        try: await interaction.channel.delete()
        except: pass

class Interactive(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(TicketButtonView(self.bot))
        self.bot.add_view(TicketCloseView())
        self.bot.add_view(SelfRoleView())
        self.bot.add_view(VerificationView(self.bot))
        self.bot.add_view(GiveawayView())

    async def resolve_giveaway_task(self, channel, message_id, prize, winners_count, duration_minutes):
        await asyncio.sleep(duration_minutes * 60)
        if message_id not in self.bot.giveaways: return
        
        try: msg = await channel.fetch_message(message_id)
        except: return

        participants = list(self.bot.giveaways.pop(message_id, set()))
        if not participants:
            embed = msg.embeds[0]
            embed.description = "The lottery concluded, but active tracking records show zero entrants."
            embed.color = discord.Color.dark_grey()
            await msg.edit(embed=embed, view=None)
            await channel.send(f"⚠️ Lottery cancellation notice: No user logs found for prize **{prize}**.")
            return

        winners = random.sample(participants, min(winners_count, len(participants)))
        mentions_str = ", ".join(f"<@{w}>" for w in winners)
        
        embed = msg.embeds[0]
        embed.description = f"**Giveaway Concluded!**\n\n• **Prize Target Asset:** `{prize}`\n• **Selected Lucky Vectors:** {mentions_str}"
        embed.color = discord.Color.gold()
        await msg.edit(embed=embed, view=None)
        await channel.send(f"🎉 Congratulations {mentions_str}! You won the prize drop allocation for: **{prize}**!")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot: return
        guild = member.guild
        settings = await self.bot.get_guild_settings(str(guild.id))
        generator_id = settings.get("voice_generator_id")
        
        if after.channel and generator_id and str(after.channel.id) == str(generator_id):
            category = after.channel.category
            room_name = f"🔊 {member.name}'s Room"
            try:
                temp_ch = await guild.create_voice_channel(name=room_name, category=category, reason="Dynamic Voice Custom Framework Scaling")
                self.bot.temporary_voice_channels.add(temp_ch.id)
                await member.move_to(temp_ch)
            except: pass
                
        if before.channel and before.channel.id in self.bot.temporary_voice_channels:
            if len(before.channel.members) == 0:
                try:
                    await before.channel.delete(reason="Dynamic voice channel vacant evacuation sweep")
                    self.bot.temporary_voice_channels.discard(before.channel.id)
                except: pass

    @app_commands.command(name="setup_ticket", description="Deploys the static interaction dashboard link for private inquiry support rooms.")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_ticket(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="Support Center Gateway", description="Need help? Touch the button asset node beneath to deploy a locked private communication vector with server administrators.", color=discord.Color.blue())
        await interaction.channel.send(embed=embed, view=TicketButtonView(self.bot))
        await interaction.followup.send("✅ Support portal frame routed successfully.", ephemeral=True)

    @app_commands.command(name="setup_verification", description="Deploys rules checkpoint gate layout framework rules asset.")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_verification(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="Security Authentication Matrix", description="Accept terms of server compliance guidelines. Trigger the verify authorization event below to unlock full text channels.", color=discord.Color.green())
        await interaction.channel.send(embed=embed, view=VerificationView(self.bot))
        await interaction.followup.send("✅ Verification access vector deployed.", ephemeral=True)

    @app_commands.command(name="setup_roles", description="Deploys automated dynamic choice selector dropdown frames.")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_roles(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="Self Roles Directory", description="Select interest topics below to append pings tags safely into your user profiles context records map.", color=discord.Color.gold())
        await interaction.channel.send(embed=embed, view=SelfRoleView())
        await interaction.followup.send("✅ Dynamic assignment frame established.", ephemeral=True)

    @app_commands.command(name="setup_voice", description="Deploys custom auto-scaling voice generator target framework node.")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_voice(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        try:
            voice_ch = await guild.create_voice_channel(name="➕ Join to Create Room")
            await self.bot.bulk_update_guild_settings(str(guild.id), {"voice_generator_id": str(voice_ch.id)})
            await interaction.followup.send(f"✅ On-Demand factory loop fully initialized: {voice_ch.mention}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Internal privilege exception failure: {e}", ephemeral=True)

    @app_commands.command(name="giveaway", description="Launches a live scheduled lucky winner asset reward distribution loop.")
    @app_commands.checks.has_permissions(administrator=True)
    async def giveaway(self, interaction: discord.Interaction, duration_minutes: int, winners: int, prize: str):
        if duration_minutes <= 0 or winners <= 0:
            await interaction.response.send_message("❌ Metrics configurations out of acceptable bounds.", ephemeral=True)
            return
        await interaction.response.defer()
        
        end_time = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
        embed = discord.Embed(
            title=f"🎁 ACTIVE LOTTERY EVENT: {prize}",
            description=f"Tap the celebration icon underneath to enter the participant queues!\n\n• **Winners Allocation:** `{winners}`\n• **Lifecycle Duration Remaining:** {discord.utils.format_dt(end_time, 'R')}",
            color=discord.Color.purple()
        )
        msg = await interaction.followup.send(embed=embed, view=GiveawayView())
        
        self.bot.giveaways[msg.id] = set()
        self.bot.loop.create_task(self.resolve_giveaway_task(interaction.channel, msg.id, prize, winners, duration_minutes))

async def setup(bot):
    await bot.add_cog(Interactive(bot))
