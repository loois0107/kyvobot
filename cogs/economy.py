import discord
from discord import app_commands
from discord.ext import commands
import random
import time
import datetime

class KyvoEconomy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # In-memory daily claim tracker (User ID: timestamp)
        self.cooldowns = {}
        # 🔒 TRANSACTION LOCK MATRIX: Tracks active monetary calculations to eliminate double-spending exploits
        self.active_transactions = set()

    # ========================================================
    # [CORE USER ECONOMY COMMANDS]
    # ========================================================

    @app_commands.command(name="balance", description="Check your current wallet balance.")
    async def balance(self, interaction: discord.Interaction):
        """Displays the user's current currency balance with customized server currency name."""
        await interaction.response.defer()
        
        guild_settings = await self.bot.get_guild_settings(str(interaction.guild_id))
        
        # Robust unpacking supporting both legacy JSON blocks and direct flat columns
        economy_set = guild_settings.get("economy_settings")
        if not economy_set:
            economy_set = guild_settings if "currency_name" in guild_settings else {}
            
        currency_name = economy_set.get("currency_name", "Points")

        user_data = await self.bot.get_user_data(str(interaction.user.id))
        current_points = user_data.get("points", 0)

        # Premium UI Upgrade: Formatted Embed
        embed = discord.Embed(
            title="💳 Financial Statement",
            description=f"Account holder: {interaction.user.mention}",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="Available Balance", value=f"🪙 **{current_points:,}** {currency_name}", inline=False)
        embed.set_footer(text="KyvoBot Decentralized Ledger Asset")
        
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="daily", description="Claim your daily login rewards.")
    async def daily(self, interaction: discord.Interaction):
        """Provides a daily allocation of currency to the user with a 24-hour cooldown constraint."""
        await interaction.response.defer()
        user_id = interaction.user.id
        now = time.time()

        # Concurrency check to stop double-tap exploit on daily rewards
        if user_id in self.active_transactions:
            await interaction.followup.send("⚠️ **Transaction Pending:** Your ledger entry is currently being updated. Please wait.", ephemeral=True)
            return

        if user_id in self.cooldowns and now - self.cooldowns[user_id] < 86400:
            remaining = 86400 - (now - self.cooldowns[user_id])
            hours = int(remaining // 3600)
            minutes = int((remaining % 3600) // 60)
            await interaction.followup.send(
                f"⏳ Cooldown Active! You have already claimed your daily reward. Try again in **{hours}h {minutes}m**.",
                ephemeral=True
            )
            return

        self.active_transactions.add(user_id)
        try:
            guild_settings = await self.bot.get_guild_settings(str(interaction.guild_id))
            economy_set = guild_settings.get("economy_settings")
            if not economy_set:
                economy_set = guild_settings if "currency_name" in guild_settings else {}
                
            currency_name = economy_set.get("currency_name", "Points")

            reward = random.randint(100, 500)
            user_data = await self.bot.get_user_data(str(user_id))
            user_data["points"] = user_data.get("points", 0) + reward

            await self.bot.save_user_data(str(user_id), user_data)
            self.cooldowns[user_id] = now

            # Premium UI Upgrade: Celebration Embed
            embed = discord.Embed(
                title="🎁 Daily Allowance Credited",
                description=f"Success {interaction.user.mention}! You received an injection of **+{reward}** {currency_name}.",
                color=discord.Color.gold(),
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            embed.set_footer(text="KyvoBot Automated Cash Flow Layer")
            await interaction.followup.send(embed=embed)
        finally:
            self.active_transactions.discard(user_id)

    @app_commands.command(name="bet", description="Gamble a specific amount of your currency.")
    @app_commands.describe(amount="The amount of currency you want to wager.")
    # ⚡ COOLDOWN SYNC: Seamlessly hooks into main.py global exception framework to halt rapid spam
    @app_commands.checks.cooldown(1, 4.0, key=lambda i: i.user.id)
    async def bet(self, interaction: discord.Interaction, amount: int):
        """A simple high-risk transaction mechanics using custom database mutations."""
        await interaction.response.defer()
        user_id = interaction.user.id

        # 1. Base Boundary Guards
        if amount <= 0:
            await interaction.followup.send("❌ You must wager a positive number greater than 0.", ephemeral=True)
            return
            
        if amount > 1000000000:
            await interaction.followup.send("❌ Transaction Refused: Bet amount exceeds maximum allowable parameter of 1,000,000,000.", ephemeral=True)
            return

        # 2. Concurrency Processing Lock Interceptor
        if user_id in self.active_transactions:
            await interaction.followup.send("⚠️ **Processing Blocked:** Overlapping transaction data detected. Settle down!", ephemeral=True)
            return

        self.active_transactions.add(user_id)
        try:
            guild_settings = await self.bot.get_guild_settings(str(interaction.guild_id))
            economy_set = guild_settings.get("economy_settings")
            if not economy_set:
                economy_set = guild_settings if "currency_name" in guild_settings else {}
                
            currency_name = economy_set.get("currency_name", "Points")
            min_bet = economy_set.get("min_bet", 10)

            if amount < min_bet:
                await interaction.followup.send(f"❌ The minimum wager amount allowed on this server is **{min_bet}** {currency_name}.", ephemeral=True)
                return

            user_data = await self.bot.get_user_data(str(user_id))
            current_points = user_data.get("points", 0)

            if current_points < amount:
                await interaction.followup.send(f"❌ Transaction declined: Insufficient liquidity. You only possess **{current_points:,}**.", ephemeral=True)
                return

            dice_roll = random.randint(1, 100)
            if dice_roll > 50:
                user_data["points"] = current_points + amount
                title_text = "🟩 WIN! Wager Successful"
                status_msg = f"The algorithm settled in your favor! You gained **+{amount:,}** {currency_name}."
                embed_color = discord.Color.green()
            else:
                user_data["points"] = current_points - amount
                title_text = "🟥 LOSE! Liquidated"
                status_msg = f"The house cleared your position. You lost **-{amount:,}** {currency_name}."
                embed_color = discord.Color.red()

            await self.bot.save_user_data(str(user_id), user_data)
            
            # Premium UI Upgrade: Casino Embed Layout
            embed = discord.Embed(
                title=title_text,
                description=f"{interaction.user.mention}, {status_msg}\n*(Dice Engine Roll Matrix: {dice_roll}/100)*",
                color=embed_color,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            embed.add_field(name="Updated Wallet Balance", value=f"🪙 **{user_data['points']:,}** {currency_name}", inline=False)
            embed.set_footer(text="KyvoBot Risk Assessment Terminal")
            await interaction.followup.send(embed=embed)
        finally:
            self.active_transactions.discard(user_id)

    # ========================================================
    # [DYNAMIC SHOP & INVENTORY MODULE]
    # ========================================================

    shop_group = app_commands.Group(name="shop", description="Server custom shop interface commands")

    @shop_group.command(name="view", description="Browse items available in the server shop.")
    async def shop_view(self, interaction: discord.Interaction):
        """Fetches server-specific item matrix stored in JSONB schema."""
        await interaction.response.defer()
        guild_settings = await self.bot.get_guild_settings(str(interaction.guild_id))
        
        economy_set = guild_settings.get("economy_settings")
        if not economy_set:
            economy_set = guild_settings if "currency_name" in guild_settings else {}
            
        currency_name = economy_set.get("currency_name", "Points")
        shop_items = economy_set.get("shop_items", [])

        if not shop_items:
            await interaction.followup.send("🛒 The server shop is currently empty. Staff has not added any assets yet.")
            return

        embed = discord.Embed(
            title=f"🛒 {interaction.guild.name} Market Index", 
            description="Use `/buy <item_name>` to settle an order.",
            color=0x5865f2,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        for item in shop_items:
            embed.add_field(
                name=f"📦 {item['name']}",
                value=f"💵 Price: **{item['price']:,}** {currency_name}\n📝 *{item['description']}*",
                inline=False
            )
        embed.set_footer(text="KyvoBot Custom Guild Commerce Layer")
        await interaction.followup.send(embed=embed)

    @shop_group.command(name="add", description="Add a brand new item to the server shop.")
    @app_commands.default_permissions(manage_guild=True)
    async def shop_add(self, interaction: discord.Interaction, name: str, price: int, description: str):
        """Appends a dictionary entry into guild shop items array structure."""
        await interaction.response.defer(ephemeral=True)
        if price <= 0:
            await interaction.followup.send("❌ Base item cost value parameter must be a positive integer.", ephemeral=True)
            return
            
        if price > 1000000000:
            await interaction.followup.send("❌ Item cost parameter cannot exceed max boundary threshold of 1,000,000,000.", ephemeral=True)
            return

        if len(name) > 50 or len(description) > 200:
            await interaction.followup.send("❌ Bounds overflow: Item name max length is 50, description max length is 200.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        settings = await self.bot.get_guild_settings(guild_id)
        
        economy_set = settings.get("economy_settings")
        if economy_set is None:
            economy_set = settings if "currency_name" in settings else {}
            
        shop_items = economy_set.get("shop_items", [])

        if any(item['name'].lower() == name.lower() for item in shop_items):
            await interaction.followup.send("❌ Duplicate Item Identifier! An item with that configuration metadata already exists.", ephemeral=True)
            return

        new_item = {"name": name.strip(), "price": price, "description": description.strip()}
        shop_items.append(new_item)
        
        economy_set["shop_items"] = shop_items
        settings["economy_settings"] = economy_set

        # Dual-Write synchronization to maintain compatibility with legacy structures and new dashboard columns
        await self.bot.bulk_update_guild_settings(guild_id, settings)
        try:
            self.bot.supabase.table("guild_settings").update({"economy_settings": economy_set}).eq("guild_id", guild_id).execute()
        except Exception as e:
            print(f"[DB INTEGRATION WARNING] Failed mapping direct column updates to dashboard: {e}")
            
        await interaction.followup.send(f"✅ Successfully appended asset **{name}** to shop registry for **{price:,}** units.", ephemeral=True)

    @shop_group.command(name="buy", description="Purchase an item from the server shop.")
    @app_commands.describe(item_name="The exact name of the item you want to purchase.")
    async def buy_item(self, interaction: discord.Interaction, item_name: str):
        """Handles cross-validation between user points and guild shop metadata arrays."""
        await interaction.response.defer()
        user_id = str(interaction.user.id)

        # Concurrency Guard to lock user checkout execution thread
        if user_id in self.active_transactions:
            await interaction.followup.send("⚠️ **Processing Blocked:** Active purchase order pending on this account context. Hold on.", ephemeral=True)
            return

        self.active_transactions.add(user_id)
        try:
            guild_settings = await self.bot.get_guild_settings(str(interaction.guild_id))
            economy_set = guild_settings.get("economy_settings")
            if not economy_set:
                economy_set = guild_settings if "currency_name" in guild_settings else {}
                
            currency_name = economy_set.get("currency_name", "Points")
            shop_items = economy_set.get("shop_items", [])

            target_item = next((item for item in shop_items if item['name'].lower() == item_name.lower()), None)
            if not target_item:
                await interaction.followup.send(f"❌ Asset lookup failure: '**{item_name}**' does not exist inside the shop index.")
                return

            user_data = await self.bot.get_user_data(user_id)
            current_points = user_data.get("points", 0)
            item_price = target_item["price"]

            if current_points < item_price:
                await interaction.followup.send(f"❌ Settle Order Denied: You require **{item_price:,}** {currency_name}, but only hold **{current_points:,}**.")
                return

            user_data["points"] = current_points - item_price
            inventory = user_data.get("inventory", [])

            inv_item = next((item for item in inventory if item['name'].lower() == item_name.lower()), None)
            if inv_item:
                inv_item["quantity"] = inv_item.get("quantity", 1) + 1
            else:
                inventory.append({"name": target_item["name"], "quantity": 1})

            user_data["inventory"] = inventory
            await self.bot.save_user_data(user_id, user_data)

            await interaction.followup.send(f"🛍️ **Transaction Complete!** Purchased asset **{target_item['name']}** for **{item_price:,}** {currency_name}!")
        finally:
            self.active_transactions.discard(user_id)

    @app_commands.command(name="inventory", description="View all items currently stored in your personal vault.")
    async def inventory_view(self, interaction: discord.Interaction):
        """Renders user inventory arrays fetched dynamically out of Supabase."""
        await interaction.response.defer()
        user_data = await self.bot.get_user_data(str(interaction.user.id))
        inventory = user_data.get("inventory", [])

        if not inventory:
            await interaction.followup.send("🎒 Vault Empty! Your inventory holds zero assets. Purchase items from `/shop view`.")
            return

        embed = discord.Embed(
            title=f"🎒 Personal Asset Vault: {interaction.user.display_name}", 
            color=0x2b2d31,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        description_lines = [f"• **{item['name']}** ` x{item['quantity']} `" for item in inventory]
        embed.description = "\n".join(description_lines)
        embed.set_footer(text="KyvoBot Distributed Inventory Node")
        await interaction.followup.send(embed=embed)

    # ========================================================
    # [ADMINISTRATIVE CASH FLOW OVERRIDES]
    # ========================================================

    eco_group = app_commands.Group(name="eco", description="Admin monetary allocation tools", default_permissions=discord.Permissions(manage_guild=True))

    @eco_group.command(name="give", description="Forcefully credit currency allocation to a specific user profile.")
    async def eco_give(self, interaction: discord.Interaction, target: discord.User, amount: int):
        """Mutates user financial schema by injecting points directly."""
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Allocation quantity threshold error: Must be greater than 0.", ephemeral=True)
            return
            
        if amount > 1000000000:
            await interaction.followup.send("❌ Allocation quantity threshold error: Max limit is 1,000,000,000.", ephemeral=True)
            return

        guild_settings = await self.bot.get_guild_settings(str(interaction.guild_id))
        economy_set = guild_settings.get("economy_settings")
        if not economy_set:
            economy_set = guild_settings if "currency_name" in guild_settings else {}
            
        currency_name = economy_set.get("currency_name", "Points")

        user_data = await self.bot.get_user_data(str(target.id))
        user_data["points"] = min(2000000000, user_data.get("points", 0) + amount)
        await self.bot.save_user_data(str(target.id), user_data)

        print(f"[ECONOMY ADMIN] Granted {amount} to {target.name} via admin override.")
        await interaction.followup.send(f"✅ Successfully allocated **+{amount:,}** {currency_name} to {target.mention}.", ephemeral=True)

    @eco_group.command(name="take", description="Deduct custom balance indexes away from a targeted user.")
    async def eco_take(self, interaction: discord.Interaction, target: discord.User, amount: int):
        """Enforces administrative budget cuts/penalties onto user point structures."""
        await interaction.response.defer(ephemeral=True)
        if amount <= 0:
            await interaction.followup.send("❌ Deduction quantity threshold error: Must be greater than 0.", ephemeral=True)
            return
            
        if amount > 1000000000:
            await interaction.followup.send("❌ Deduction quantity threshold error: Max limit is 1,000,000,000.", ephemeral=True)
            return

        guild_settings = await self.bot.get_guild_settings(str(interaction.guild_id))
        economy_set = guild_settings.get("economy_settings")
        if not economy_set:
            economy_set = guild_settings if "currency_name" in guild_settings else {}
            
        currency_name = economy_set.get("currency_name", "Points")

        user_data = await self.bot.get_user_data(str(target.id))
        current_points = user_data.get("points", 0)
        
        user_data["points"] = max(0, current_points - amount)
        await self.bot.save_user_data(str(target.id), user_data)

        print(f"[ECONOMY ADMIN] Deducted {amount} from {target.name} via admin override.")
        await interaction.followup.send(f"✅ Successfully liquidated **-{amount:,}** {currency_name} from {target.mention}.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(KyvoEconomy(bot))
