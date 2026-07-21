import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import random
import time
import datetime

# ══════════════════════════════════════════════════════════
#  🃏 Blackjack Core Rules (Cog 상태와 무관한 순수 함수들)
# ══════════════════════════════════════════════════════════
CARD_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]


def draw_card() -> tuple[str, str]:
    return (random.choice(CARD_RANKS), random.choice(CARD_SUITS))


def card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def hand_value(hand: list[tuple[str, str]]) -> int:
    total = sum(card_value(rank) for rank, _ in hand)
    aces = sum(1 for rank, _ in hand if rank == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def is_blackjack(hand: list[tuple[str, str]]) -> bool:
    return len(hand) == 2 and hand_value(hand) == 21


def format_hand(hand: list[tuple[str, str]]) -> str:
    return " ".join(f"`{rank}{suit}`" for rank, suit in hand)


def determine_blackjack_result(player_hand: list[tuple[str, str]], dealer_hand: list[tuple[str, str]]) -> str:
    """'win' / 'lose' / 'push' / 'bust' 중 하나를 반환한다."""
    if hand_value(player_hand) > 21:
        return "bust"

    player_total = hand_value(player_hand)
    dealer_total = hand_value(dealer_hand)
    player_bj = is_blackjack(player_hand)
    dealer_bj = is_blackjack(dealer_hand)

    # 🛡️ 블랙잭(첫 2장 A+10) 우선 승리 판정 - 히트로 나중에 만든 21은 자연 블랙잭이 아니므로
    # is_blackjack이 len(hand)==2를 같이 체크해서 여기 안 걸리고 아래 점수 비교로 넘어간다.
    if player_bj and dealer_bj:
        return "push"
    if player_bj:
        return "win"
    if dealer_bj:
        return "lose"

    if dealer_total > 21:
        return "win"
    if player_total > dealer_total:
        return "win"
    if player_total < dealer_total:
        return "lose"
    return "push"


class BlackjackGame:
    """진행 중인 블랙잭 한 판의 상태. Redis까지는 필요 없고 메시지당 인메모리로 충분하다 -
    View 인스턴스가 discord.py 내부적으로 메시지 id와 연결되어 있어서 별도 추적 dict도 불필요."""

    def __init__(self, user_id: int, guild_id: int, bet: int, currency_name: str):
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.currency_name = currency_name
        self.player_hand = [draw_card(), draw_card()]
        self.dealer_hand = [draw_card(), draw_card()]


class BlackjackView(discord.ui.View):
    def __init__(self, cog: "KyvoEconomy", game: BlackjackGame, hit_label: str, stand_label: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.game = game
        self.message: discord.Message | None = None
        # 데코레이터로 만든 버튼은 self.<method_name>으로 실제 Button 인스턴스에 접근 가능 -
        # get_msg로 가져온 로케일 라벨을 여기서 덮어씌운다.
        self.hit_button.label = hit_label
        self.stand_button.label = stand_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message("❌ This is not your blackjack table.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_blackjack_hit(interaction, self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_blackjack_stand(interaction, self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        # 타임아웃까지는 포인트를 건드린 적이 없으므로(정산은 Stand/버스트에서만 발생) 그냥 락만 푼다.
        self.cog.active_transactions.discard(self.game.user_id)


class KyvoEconomy(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        # In-memory daily claim tracker (User ID: timestamp)
        self.cooldowns = {}
        # 🔒 TRANSACTION LOCK MATRIX: Tracks active monetary calculations to eliminate double-spending exploits
        self.active_transactions = set()

    async def _get_economy_settings(self, guild_id) -> dict:
        """economy_settings를 Redis Cache-Aside(KyvoBaseCog.get_guild_settings)로 읽어온다.
        guild_settings 행은 {..., settings: {economy_settings: {...}}} 구조라 한 단계 더 파고들어야 한다."""
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        return nested_settings.get("economy_settings") or {}

    # ========================================================
    # [CORE USER ECONOMY COMMANDS]
    # ========================================================

    @app_commands.command(name="balance", description="Check your current wallet balance.")
    async def balance(self, interaction: discord.Interaction):
        """Displays the user's current currency balance with customized server currency name."""
        await interaction.response.defer()

        economy_set = await self._get_economy_settings(interaction.guild_id)
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
            economy_set = await self._get_economy_settings(interaction.guild_id)
            currency_name = economy_set.get("currency_name", "Points")

            reward = random.randint(100, 500)
            user_data = await self.bot.get_user_data(str(user_id))
            user_data["points"] = user_data.get("points", 0) + reward

            save_ok = await self.bot.save_user_data(str(user_id), user_data)
            if not save_ok:
                await interaction.followup.send(
                    "❌ **Ledger Write Failed:** Your daily reward could not be saved due to a database error. Please try again shortly.",
                    ephemeral=True
                )
                return

            # 저장이 실제로 성공했을 때만 쿨다운을 건다 - 실패했는데 24시간 묶어두면 보상도 못 받고 재시도도 못 함
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
            economy_set = await self._get_economy_settings(interaction.guild_id)
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

            save_ok = await self.bot.save_user_data(str(user_id), user_data)
            if not save_ok:
                await interaction.followup.send(
                    "❌ **Ledger Write Failed:** Your wager result could not be saved due to a database error. Your balance was not changed.",
                    ephemeral=True
                )
                return

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
    # [🃏 BLACKJACK TABLE]
    # ========================================================

    @app_commands.command(name="blackjack", description="Play a hand of blackjack against the house.")
    @app_commands.describe(bet="The amount of currency you want to wager.")
    async def blackjack(self, interaction: discord.Interaction, bet: int):
        await interaction.response.defer()
        user_id = interaction.user.id
        guild_id = interaction.guild_id

        if user_id in self.active_transactions:
            await interaction.followup.send("⚠️ **Processing Blocked:** Overlapping transaction data detected. Settle down!", ephemeral=True)
            return

        economy_set = await self._get_economy_settings(guild_id)
        currency_name = economy_set.get("currency_name", "Points")
        min_bet = economy_set.get("min_bet", 10)

        if bet < min_bet:
            msg = await self.get_msg(guild_id, "bj_err_min_bet")
            await interaction.followup.send(msg, ephemeral=True)
            return

        user_data = await self.bot.get_user_data(str(user_id))
        current_points = user_data.get("points", 0)

        if current_points < bet:
            msg = await self.get_msg(guild_id, "bj_err_no_points", points=current_points)
            await interaction.followup.send(msg, ephemeral=True)
            return

        self.active_transactions.add(user_id)
        game = BlackjackGame(user_id, guild_id, bet, currency_name)

        # 딜러/플레이어 둘 중 하나라도 딜 직후 내추럴 블랙잭이면 Hit/Stand 없이 바로 정산한다.
        if is_blackjack(game.player_hand) or is_blackjack(game.dealer_hand):
            try:
                embed = await self._resolve_and_settle_blackjack(game)
                await interaction.followup.send(embed=embed)
            finally:
                self.active_transactions.discard(user_id)
            return

        embed = await self._build_blackjack_embed(game, finished=False)
        hit_label = await self.get_msg(guild_id, "bj_btn_hit")
        stand_label = await self.get_msg(guild_id, "bj_btn_stand")
        view = BlackjackView(self, game, hit_label, stand_label)
        message = await interaction.followup.send(embed=embed, view=view)
        view.message = message

    async def handle_blackjack_hit(self, interaction: discord.Interaction, view: BlackjackView):
        game = view.game
        game.player_hand.append(draw_card())

        if hand_value(game.player_hand) > 21:
            embed = await self._resolve_and_settle_blackjack(game)
            for item in view.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=view)
            self.active_transactions.discard(game.user_id)
            return

        embed = await self._build_blackjack_embed(game, finished=False)
        await interaction.response.edit_message(embed=embed, view=view)

    async def handle_blackjack_stand(self, interaction: discord.Interaction, view: BlackjackView):
        game = view.game
        # 표준 규칙: 딜러는 17 미만이면 자동으로 계속 히트한다.
        while hand_value(game.dealer_hand) < 17:
            game.dealer_hand.append(draw_card())

        embed = await self._resolve_and_settle_blackjack(game)
        for item in view.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=view)
        self.active_transactions.discard(game.user_id)

    async def _resolve_and_settle_blackjack(self, game: BlackjackGame) -> discord.Embed:
        """결과 판정 + (푸시가 아니면) save_user_data로 정산. 저장 실패 시 정직하게 실패 embed를 반환한다."""
        result = determine_blackjack_result(game.player_hand, game.dealer_hand)

        if result == "push":
            status_key = "bj_status_push"
        else:
            user_data = await self.bot.get_user_data(str(game.user_id))
            current_points = user_data.get("points", 0)
            if result == "win":
                user_data["points"] = current_points + game.bet
                status_key = "bj_status_win"
            else:  # lose or bust
                user_data["points"] = max(0, current_points - game.bet)
                status_key = "bj_status_lose" if result == "lose" else "bj_status_bust"

            save_ok = await self.bot.save_user_data(str(game.user_id), user_data)
            if not save_ok:
                return discord.Embed(
                    title="❌ Ledger Write Failed",
                    description="Your blackjack result could not be saved due to a database error. Your balance was not changed.",
                    color=discord.Color.red()
                )

        status_msg = await self.get_msg(game.guild_id, status_key, points=game.bet)
        return await self._build_blackjack_embed(game, finished=True, status_msg=status_msg)

    async def _build_blackjack_embed(self, game: BlackjackGame, finished: bool, status_msg: str | None = None) -> discord.Embed:
        title = await self.get_msg(game.guild_id, "bj_title")
        dealer_label = await self.get_msg(game.guild_id, "bj_dealer")
        player_label = await self.get_msg(game.guild_id, "bj_player")

        if finished:
            dealer_display = f"{format_hand(game.dealer_hand)} (`{hand_value(game.dealer_hand)}`)"
        else:
            # 진행 중엔 딜러 첫 카드만 공개
            hidden_rank, hidden_suit = game.dealer_hand[0]
            dealer_display = f"`{hidden_rank}{hidden_suit}` `❓`"

        player_display = f"{format_hand(game.player_hand)} (`{hand_value(game.player_hand)}`)"

        embed = discord.Embed(
            title=title,
            description=status_msg if finished else await self.get_msg(game.guild_id, "bj_status_playing"),
            color=discord.Color.gold() if finished else discord.Color.dark_green()
        )
        embed.add_field(name=dealer_label, value=dealer_display, inline=False)
        embed.add_field(name=player_label, value=player_display, inline=False)
        embed.set_footer(text=f"Wager: {game.bet:,} {game.currency_name}")
        return embed

    # ========================================================
    # [DYNAMIC SHOP & INVENTORY MODULE]
    # ========================================================

    shop_group = app_commands.Group(name="shop", description="Server custom shop interface commands")

    @shop_group.command(name="view", description="Browse items available in the server shop.")
    async def shop_view(self, interaction: discord.Interaction):
        """Fetches server-specific item matrix stored in JSONB schema."""
        await interaction.response.defer()
        economy_set = await self._get_economy_settings(interaction.guild_id)
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

        guild_id = interaction.guild_id
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        economy_set = nested_settings.get("economy_settings") or {}
        shop_items = economy_set.get("shop_items", [])

        if any(item['name'].lower() == name.lower() for item in shop_items):
            await interaction.followup.send("❌ Duplicate Item Identifier! An item with that configuration metadata already exists.", ephemeral=True)
            return

        new_item = {"name": name.strip(), "price": price, "description": description.strip()}
        shop_items.append(new_item)

        economy_set["shop_items"] = shop_items
        nested_settings["economy_settings"] = economy_set

        # settings JSON 내부(nested) 하나에만 쓴다 - 최상위 economy_settings 컬럼은 아무도 안 읽는 죽은
        # 컬럼이라 예전엔 매번 거기에도 dual-write하고 있었다.
        await self.bot.bulk_update_guild_settings(guild_id, nested_settings)
        # 🛡️ [캐시 정합성] KyvoBaseCog 전환으로 읽기가 Redis Cache-Aside를 타게 됐으니, 쓴 뒤에는 반드시
        # 캐시를 무효화해야 다음 조회가 최대 5분간 옛날 상점 목록을 보여주는 걸 막을 수 있다.
        await self.invalidate_settings_cache(guild_id)

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
            economy_set = await self._get_economy_settings(interaction.guild_id)
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
            # 🛡️ points 차감과 inventory 지급은 이 한 번의 save_user_data 호출로 같이 저장된다(별도 쓰기
            # 두 번이 아님) - 그래서 "포인트만 빠지고 아이템은 안 들어감" 같은 부분 실패는 구조적으로
            # 불가능하다. 다만 이 저장 자체가 실패하면 여태 무조건 성공 메시지를 보내고 있었다.
            save_ok = await self.bot.save_user_data(user_id, user_data)
            if not save_ok:
                await interaction.followup.send(
                    "❌ **Ledger Write Failed:** Your purchase could not be saved due to a database error. No points were deducted.",
                    ephemeral=True
                )
                return

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

        economy_set = await self._get_economy_settings(interaction.guild_id)
        currency_name = economy_set.get("currency_name", "Points")

        user_data = await self.bot.get_user_data(str(target.id))
        user_data["points"] = min(2000000000, user_data.get("points", 0) + amount)
        save_ok = await self.bot.save_user_data(str(target.id), user_data)

        if not save_ok:
            await interaction.followup.send(f"❌ **Ledger Write Failed:** Could not allocate funds to {target.mention} due to a database error.", ephemeral=True)
            return

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

        economy_set = await self._get_economy_settings(interaction.guild_id)
        currency_name = economy_set.get("currency_name", "Points")

        user_data = await self.bot.get_user_data(str(target.id))
        current_points = user_data.get("points", 0)

        user_data["points"] = max(0, current_points - amount)
        save_ok = await self.bot.save_user_data(str(target.id), user_data)

        if not save_ok:
            await interaction.followup.send(f"❌ **Ledger Write Failed:** Could not deduct funds from {target.mention} due to a database error.", ephemeral=True)
            return

        print(f"[ECONOMY ADMIN] Deducted {amount} from {target.name} via admin override.")
        await interaction.followup.send(f"✅ Successfully liquidated **-{amount:,}** {currency_name} from {target.mention}.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(KyvoEconomy(bot))
