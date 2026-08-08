import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import random
import time
import datetime
import os
import hmac
from aiohttp import web

INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")

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
            msg = await self.cog.get_msg(self.game.guild_id, "bj_err_not_your_table")
            await interaction.response.send_message(msg, ephemeral=True)
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


# ══════════════════════════════════════════════════════════
#  💣 Mines Core Rules (Cog 상태와 무관한 순수 함수들)
# ══════════════════════════════════════════════════════════
# 🛡️ [서버 설정으로 확장 가능하게 구조 열어둠] 지금은 상수지만, 실제 쓰는 곳은
# KyvoEconomy._get_house_edge() 헬퍼 하나만 거친다 - 나중에 economy_settings에 house_edge
# 필드가 생기면 그 헬퍼 안쪽만 고치면 되고, 호출부는 하나도 안 건드려도 된다.
HOUSE_EDGE = 0.03

# 서버 경제 규모(/daily 평균 보상 ~300)에 맞춘 도박 테이블 공용 최대 베팅 - 서버 경제가 커지면
# 이 값만 올리면 된다. 지뢰찾기/룰렛 등 모든 도박 게임이 이 하나의 상수를 공유한다(게임별로
# 따로 정하지 않음 - "서버 경제 규모에 맞는 한도"라는 기준 자체는 게임 종류와 무관하기 때문).
TABLE_MAX_BET = 5_000

MINES_GRID_SIZE = 5
MINES_TOTAL_CELLS = MINES_GRID_SIZE * MINES_GRID_SIZE  # 25
MINES_MULTIPLIER_CAP = 100.0


def mines_fair_multiplier(total_cells: int, mines: int, opened: int) -> float:
    """공정 배율(하우스 엣지 0%) - 복원추출 없는 초기하분포 기반. opened=0이면 1.0."""
    if opened <= 0:
        return 1.0
    safe_cells = total_cells - mines
    multiplier = 1.0
    for i in range(opened):
        multiplier *= (total_cells - i) / (safe_cells - i)
    return multiplier


def mines_multiplier(total_cells: int, mines: int, opened: int, house_edge: float) -> float:
    """하우스 엣지 + 100배 캡까지 적용한 최종 배율. opened=0이면 엣지 적용 없이 1.0."""
    if opened <= 0:
        return 1.0
    fair = mines_fair_multiplier(total_cells, mines, opened)
    return min(fair * (1 - house_edge), MINES_MULTIPLIER_CAP)


def generate_mine_positions(total_cells: int, mines: int) -> set[int]:
    return set(random.sample(range(total_cells), mines))


class MinesGame:
    """진행 중인 지뢰찾기 한 판의 상태. 블랙잭과 동일하게 Redis 없이 View 인스턴스에 인메모리로 충분 -
    discord.py가 View를 메시지와 자동으로 연결해줘서 별도 추적 dict가 필요 없다."""

    def __init__(self, user_id: int, guild_id: int, bet: int, currency_name: str, mine_count: int, house_edge: float):
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.currency_name = currency_name
        self.mine_count = mine_count
        self.house_edge = house_edge
        self.mine_positions = generate_mine_positions(MINES_TOTAL_CELLS, mine_count)
        self.opened_cells: set[int] = set()
        self.max_safe_cells = MINES_TOTAL_CELLS - mine_count

    @property
    def current_multiplier(self) -> float:
        return mines_multiplier(MINES_TOTAL_CELLS, self.mine_count, len(self.opened_cells), self.house_edge)

    @property
    def at_cap(self) -> bool:
        return self.current_multiplier >= MINES_MULTIPLIER_CAP

    @property
    def potential_profit(self) -> int:
        """캐시아웃 시 실제로 더해질 순이익. 베팅액(bet)은 게임 시작 시 차감된 적이 없으므로
        (블랙잭 승리가 bet을 그대로 한 번만 더하는 것과 같은 '델타만 적용' 방식) 배율 전체가 아니라
        (배율-1)*베팅액만큼만 더해야 한다 - 안 그러면 원금을 이중으로 챙겨주는 꼴이 된다."""
        return int(self.bet * (self.current_multiplier - 1))

    def render_grid_text(self, reveal_all: bool = False) -> str:
        """임베드에 표시할 텍스트 그리드. reveal_all=True면(패배 시) 지뢰 위치까지 다 보여준다."""
        rows = []
        for r in range(MINES_GRID_SIZE):
            cells = []
            for c in range(MINES_GRID_SIZE):
                idx = r * MINES_GRID_SIZE + c
                if idx in self.opened_cells:
                    cells.append("💎")
                elif reveal_all and idx in self.mine_positions:
                    cells.append("💣")
                else:
                    cells.append("⬛")
            rows.append(" ".join(cells))
        return "\n".join(rows)


class MinesView(discord.ui.View):
    def __init__(self, cog: "KyvoEconomy", game: MinesGame, cashout_label: str):
        super().__init__(timeout=90)
        self.cog = cog
        self.game = game
        self.message: discord.Message | None = None
        self.cashout_label = cashout_label
        self.cashout_button: discord.ui.Button | None = None
        self.cell_buttons: dict[int, discord.ui.Button] = {}

        for i in range(MINES_TOTAL_CELLS):
            btn = discord.ui.Button(label="​", style=discord.ButtonStyle.secondary, row=i // MINES_GRID_SIZE)
            btn.callback = self._make_cell_callback(i)
            self.cell_buttons[i] = btn
            self.add_item(btn)

    def _make_cell_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            await self.cog.handle_mines_cell(interaction, self, index)
        return callback

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.user_id:
            msg = await self.cog.get_msg(self.game.guild_id, "mines_err_not_your_game")
            await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    def open_cell_button(self, index: int):
        """칸을 열어서 버튼 그리드에서 제거한다. 디스코드 View는 컴포넌트 최대 25개라 5x5 그리드만으로
        이미 한도를 다 쓰기 때문에, Cash Out 버튼을 넣으려면 자리를 비워야 한다 - 열린 칸의 상태(💎)는
        버튼이 아니라 embed의 텍스트 그리드(render_grid_text)가 대신 보여준다."""
        btn = self.cell_buttons.pop(index, None)
        if btn is not None:
            self.remove_item(btn)
        if self.cashout_button is None:
            self.cashout_button = discord.ui.Button(label=self.cashout_label, style=discord.ButtonStyle.success, row=index // MINES_GRID_SIZE)
            self.cashout_button.callback = self._cashout_callback
            self.add_item(self.cashout_button)

    async def _cashout_callback(self, interaction: discord.Interaction):
        await self.cog.handle_mines_cashout(interaction, self)

    def disable_all(self):
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        # 블랙잭과 동일한 설계: 정산은 캐시아웃/지뢰 발견 시에만 발생하므로, 타임아웃 시점엔 포인트를
        # 건드린 적이 없다. 그냥 잠그고 락만 푼다.
        self.disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.cog.active_transactions.discard(self.game.user_id)


# ══════════════════════════════════════════════════════════
#  🔫 Russian Roulette Core Rules
# ══════════════════════════════════════════════════════════
# 6연발 실린더에 총알 1발, 한 번 돌린 채로 고정(재장전 없음) - 지뢰찾기의 초기하분포 공식과
# 완전히 동일한 수학(total_cells=6, mines=1)이라 mines_fair_multiplier/mines_multiplier를
# 그대로 재사용한다. 검증 결과 이 게임의 이론적 최대 배율은 5.82x(5번째 생존 시)로,
# MINES_MULTIPLIER_CAP(100배)에는 절대 도달하지 않는다 - 캡은 안전망으로만 남겨둔다.
ROULETTE_CHAMBERS = 6
ROULETTE_BULLETS = 1
ROULETTE_MAX_PULLS = ROULETTE_CHAMBERS - ROULETTE_BULLETS  # 5


class RouletteGame:
    """진행 중인 러시안룰렛 한 판의 상태. 지뢰찾기와 동일하게 View 인스턴스에 인메모리로 충분."""

    def __init__(self, user_id: int, guild_id: int, bet: int, currency_name: str, house_edge: float):
        self.user_id = user_id
        self.guild_id = guild_id
        self.bet = bet
        self.currency_name = currency_name
        self.house_edge = house_edge
        # 지뢰찾기의 generate_mine_positions와 동일한 패턴: 총알 위치를 시작 시점에 한 번 고정해두고,
        # 방아쇠를 당길 때마다 "지금 칸이 그 위치인가"만 확인한다(실린더를 한 번 돌린 채 고정 = 재장전 없음).
        self.bullet_position = random.randint(0, ROULETTE_CHAMBERS - 1)
        self.pulls = 0  # 지금까지 생존한 방아쇠 횟수 (0~5)

    @property
    def current_multiplier(self) -> float:
        return mines_multiplier(ROULETTE_CHAMBERS, ROULETTE_BULLETS, self.pulls, self.house_edge)

    @property
    def potential_profit(self) -> int:
        """지뢰찾기와 동일한 델타 모델 - 베팅액은 시작 시 차감된 적이 없으므로 배율 전체가 아니라
        (배율-1)*베팅액만큼만 순이익으로 더한다."""
        return int(self.bet * (self.current_multiplier - 1))

    @property
    def is_final_chamber(self) -> bool:
        """5번째까지 생존하면 안전 약실이 0개 - 6번째 칸은 총알이 확정이라 더 당길 수 없다."""
        return self.pulls >= ROULETTE_MAX_PULLS

    def pull_trigger(self) -> bool:
        """다음 약실을 당긴다. True면 생존(빈 약실), False면 총알."""
        hit = self.pulls == self.bullet_position
        if not hit:
            self.pulls += 1
        return not hit

    def render_cylinder_text(self, reveal_bullet: bool = False) -> str:
        """임베드에 표시할 실린더 텍스트. reveal_bullet=True면(패배 시) 총알 위치까지 보여준다."""
        chambers = []
        for i in range(ROULETTE_CHAMBERS):
            if i < self.pulls:
                chambers.append("⚪")
            elif reveal_bullet and i == self.bullet_position:
                chambers.append("🔴")
            else:
                chambers.append("⚫")
        return " ".join(chambers)


class RouletteView(discord.ui.View):
    def __init__(self, cog: "KyvoEconomy", game: RouletteGame, trigger_label: str, cashout_label: str):
        super().__init__(timeout=90)
        self.cog = cog
        self.game = game
        self.message: discord.Message | None = None

        self.trigger_button = discord.ui.Button(label=trigger_label, style=discord.ButtonStyle.danger, row=0)
        self.trigger_button.callback = self._trigger_callback
        self.add_item(self.trigger_button)

        # 최소 1번은 생존해야 순이익이 생기므로(0번째 생존 시 배율 1.0x = 순이익 0), 지뢰찾기와
        # 동일하게 첫 생존 전까지는 비활성화해둔다.
        self.cashout_button = discord.ui.Button(label=cashout_label, style=discord.ButtonStyle.success, row=0, disabled=True)
        self.cashout_button.callback = self._cashout_callback
        self.add_item(self.cashout_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game.user_id:
            msg = await self.cog.get_msg(self.game.guild_id, "roulette_err_not_your_game")
            await interaction.response.send_message(msg, ephemeral=True)
            return False
        return True

    def sync_buttons(self):
        """생존 횟수에 맞춰 버튼 상태를 갱신한다. 5번째까지 생존하면(안전 약실 0개) 방아쇠 버튼
        자체를 뷰에서 제거한다 - 6번째 칸은 존재하지 않으므로 아예 안 보여준다."""
        self.cashout_button.disabled = self.game.pulls < 1
        if self.game.is_final_chamber and self.trigger_button in self.children:
            self.remove_item(self.trigger_button)

    async def _trigger_callback(self, interaction: discord.Interaction):
        await self.cog.handle_roulette_trigger(interaction, self)

    async def _cashout_callback(self, interaction: discord.Interaction):
        await self.cog.handle_roulette_cashout(interaction, self)

    def disable_all(self):
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        # 지뢰찾기와 동일한 설계: 정산은 캐시아웃/총알 발견 시에만 발생하므로, 타임아웃 시점엔
        # 포인트를 건드린 적이 없다. 그냥 잠그고 락만 푼다.
        self.disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.cog.active_transactions.discard(self.game.user_id)


class KyvoEconomy(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        # In-memory daily claim tracker (User ID: timestamp)
        self.cooldowns = {}
        # 🔒 TRANSACTION LOCK MATRIX: Tracks active monetary calculations to eliminate double-spending exploits
        self.active_transactions = set()

    async def cog_load(self):
        if INTERNAL_API_SECRET:
            self.bot.web_app.router.add_post("/internal/economy/shop-buy", self.handle_shop_buy_webhook)
            print("[⚡ ECONOMY] Internal shop-buy route registered at /internal/economy/shop-buy.", flush=True)
        else:
            print("[ECONOMY][WARN] INTERNAL_API_SECRET not set - dashboard-triggered purchases are disabled "
                  "(the /shop buy command still works).", flush=True)

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

        user_data = await self.bot.get_user_data(str(interaction.user.id), str(interaction.guild_id))
        current_points = user_data.get("points", 0)

        # Premium UI Upgrade: Formatted Embed
        title = await self.get_msg(interaction.guild_id, "bal_title")
        holder = await self.get_msg(interaction.guild_id, "bal_holder", mention=interaction.user.mention)
        field_label = await self.get_msg(interaction.guild_id, "bal_field_balance")
        footer = await self.get_msg(interaction.guild_id, "bal_footer")
        embed = discord.Embed(
            title=title,
            description=holder,
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name=field_label, value=f"🪙 **{current_points:,}** {currency_name}", inline=False)
        embed.set_footer(text=footer)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="daily", description="Claim your daily login rewards.")
    async def daily(self, interaction: discord.Interaction):
        """Provides a daily allocation of currency to the user with a 24-hour cooldown constraint."""
        await interaction.response.defer()
        user_id = interaction.user.id
        now = time.time()

        # Concurrency check to stop double-tap exploit on daily rewards
        if user_id in self.active_transactions:
            msg = await self.get_msg(interaction.guild_id, "daily_err_pending")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if user_id in self.cooldowns and now - self.cooldowns[user_id] < 86400:
            remaining = 86400 - (now - self.cooldowns[user_id])
            hours = int(remaining // 3600)
            minutes = int((remaining % 3600) // 60)
            msg = await self.get_msg(interaction.guild_id, "daily_err_cooldown", hours=hours, minutes=minutes)
            await interaction.followup.send(msg, ephemeral=True)
            return

        self.active_transactions.add(user_id)
        try:
            economy_set = await self._get_economy_settings(interaction.guild_id)
            currency_name = economy_set.get("currency_name", "Points")

            reward = random.randint(100, 500)
            user_data = await self.bot.get_user_data(str(user_id), str(interaction.guild_id))
            user_data["points"] = user_data.get("points", 0) + reward

            save_ok = await self.bot.save_user_data(str(user_id), str(interaction.guild_id), user_data)
            if not save_ok:
                msg = await self.get_msg(interaction.guild_id, "daily_err_save_failed")
                await interaction.followup.send(msg, ephemeral=True)
                return

            # 저장이 실제로 성공했을 때만 쿨다운을 건다 - 실패했는데 24시간 묶어두면 보상도 못 받고 재시도도 못 함
            self.cooldowns[user_id] = now

            # Premium UI Upgrade: Celebration Embed
            title = await self.get_msg(interaction.guild_id, "daily_title")
            desc = await self.get_msg(interaction.guild_id, "daily_desc", mention=interaction.user.mention, reward=reward, currency=currency_name)
            footer = await self.get_msg(interaction.guild_id, "daily_footer")
            embed = discord.Embed(
                title=title,
                description=desc,
                color=discord.Color.gold(),
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            embed.set_footer(text=footer)
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
            msg = await self.get_msg(interaction.guild_id, "bet_err_invalid_amount")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if amount > 1000000000:
            msg = await self.get_msg(interaction.guild_id, "bet_err_max_amount")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 2. Concurrency Processing Lock Interceptor
        if user_id in self.active_transactions:
            msg = await self.get_msg(interaction.guild_id, "econ_err_processing_blocked")
            await interaction.followup.send(msg, ephemeral=True)
            return

        self.active_transactions.add(user_id)
        try:
            economy_set = await self._get_economy_settings(interaction.guild_id)
            currency_name = economy_set.get("currency_name", "Points")
            min_bet = economy_set.get("min_bet", 10)

            if amount < min_bet:
                msg = await self.get_msg(interaction.guild_id, "bet_err_min_bet", min_bet=min_bet, currency=currency_name)
                await interaction.followup.send(msg, ephemeral=True)
                return

            user_data = await self.bot.get_user_data(str(user_id), str(interaction.guild_id))
            current_points = user_data.get("points", 0)

            if current_points < amount:
                msg = await self.get_msg(interaction.guild_id, "bet_err_insufficient", points=f"{current_points:,}")
                await interaction.followup.send(msg, ephemeral=True)
                return

            dice_roll = random.randint(1, 100)
            if dice_roll > 50:
                user_data["points"] = current_points + amount
                title_text = await self.get_msg(interaction.guild_id, "bet_win_title")
                status_msg = await self.get_msg(interaction.guild_id, "bet_status_win", amount=f"{amount:,}", currency=currency_name)
                embed_color = discord.Color.green()
            else:
                user_data["points"] = current_points - amount
                title_text = await self.get_msg(interaction.guild_id, "bet_lose_title")
                status_msg = await self.get_msg(interaction.guild_id, "bet_status_lose", amount=f"{amount:,}", currency=currency_name)
                embed_color = discord.Color.red()

            save_ok = await self.bot.save_user_data(str(user_id), str(interaction.guild_id), user_data)
            if not save_ok:
                msg = await self.get_msg(interaction.guild_id, "bet_err_save_failed")
                await interaction.followup.send(msg, ephemeral=True)
                return

            # Premium UI Upgrade: Casino Embed Layout
            desc = await self.get_msg(interaction.guild_id, "bet_desc", mention=interaction.user.mention, status=status_msg, roll=dice_roll)
            field_label = await self.get_msg(interaction.guild_id, "bet_field_balance")
            footer = await self.get_msg(interaction.guild_id, "bet_footer")
            embed = discord.Embed(
                title=title_text,
                description=desc,
                color=embed_color,
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            embed.add_field(name=field_label, value=f"🪙 **{user_data['points']:,}** {currency_name}", inline=False)
            embed.set_footer(text=footer)
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
            msg = await self.get_msg(guild_id, "econ_err_processing_blocked")
            await interaction.followup.send(msg, ephemeral=True)
            return

        economy_set = await self._get_economy_settings(guild_id)
        currency_name = economy_set.get("currency_name", "Points")
        min_bet = economy_set.get("min_bet", 10)

        if bet < min_bet:
            msg = await self.get_msg(guild_id, "bj_err_min_bet")
            await interaction.followup.send(msg, ephemeral=True)
            return

        user_data = await self.bot.get_user_data(str(user_id), str(guild_id))
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
            user_data = await self.bot.get_user_data(str(game.user_id), str(game.guild_id))
            current_points = user_data.get("points", 0)
            if result == "win":
                user_data["points"] = current_points + game.bet
                status_key = "bj_status_win"
            else:  # lose or bust
                user_data["points"] = max(0, current_points - game.bet)
                status_key = "bj_status_lose" if result == "lose" else "bj_status_bust"

            save_ok = await self.bot.save_user_data(str(game.user_id), str(game.guild_id), user_data)
            if not save_ok:
                title = await self.get_msg(game.guild_id, "econ_err_ledger_title")
                desc = await self.get_msg(game.guild_id, "bj_err_save_failed")
                return discord.Embed(title=title, description=desc, color=discord.Color.red())

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
    # [💣 MINES TABLE]
    # ========================================================

    async def _get_house_edge(self, guild_id) -> float:
        """지금은 상수를 그대로 반환하지만, 이 헬퍼 하나만 거치도록 해서 나중에 economy_settings에
        house_edge 필드가 생기면 이 안쪽만 고치면 되게 만들어뒀다 (호출부는 안 건드림)."""
        return HOUSE_EDGE

    @app_commands.command(name="mines", description="Play a round of Mines - reveal safe tiles and cash out before you hit one.")
    @app_commands.describe(bet="The amount of currency you want to wager.", danger_level="Number of mines hidden in the 5x5 grid.")
    @app_commands.choices(danger_level=[
        app_commands.Choice(name="Low (1 mine)", value=1),
        app_commands.Choice(name="Medium (3 mines)", value=3),
        app_commands.Choice(name="High (5 mines)", value=5),
    ])
    async def mines(self, interaction: discord.Interaction, bet: int, danger_level: app_commands.Choice[int]):
        await interaction.response.defer()
        user_id = interaction.user.id
        guild_id = interaction.guild_id
        mine_count = danger_level.value

        if user_id in self.active_transactions:
            msg = await self.get_msg(guild_id, "econ_err_processing_blocked")
            await interaction.followup.send(msg, ephemeral=True)
            return

        economy_set = await self._get_economy_settings(guild_id)
        currency_name = economy_set.get("currency_name", "Points")
        min_bet = economy_set.get("min_bet", 10)

        # 서버 설정의 min_bet이 이 게임 자체의 최대 베팅 한도보다 커진 경우를 명확히 막는다.
        if min_bet > TABLE_MAX_BET:
            msg = await self.get_msg(guild_id, "mines_err_table_unavailable", min_bet=min_bet, max_bet=TABLE_MAX_BET)
            await interaction.followup.send(msg, ephemeral=True)
            return

        if bet < min_bet:
            msg = await self.get_msg(guild_id, "mines_err_min_bet", min_bet=min_bet)
            await interaction.followup.send(msg, ephemeral=True)
            return

        if bet > TABLE_MAX_BET:
            msg = await self.get_msg(guild_id, "mines_err_max_bet", max_bet=TABLE_MAX_BET)
            await interaction.followup.send(msg, ephemeral=True)
            return

        user_data = await self.bot.get_user_data(str(user_id), str(guild_id))
        current_points = user_data.get("points", 0)

        if current_points < bet:
            msg = await self.get_msg(guild_id, "mines_err_no_points", points=current_points)
            await interaction.followup.send(msg, ephemeral=True)
            return

        self.active_transactions.add(user_id)
        house_edge = await self._get_house_edge(guild_id)
        game = MinesGame(user_id, guild_id, bet, currency_name, mine_count, house_edge)

        cashout_label = await self.get_msg(guild_id, "mines_btn_cashout")
        embed = await self._build_mines_embed(game, finished=False)
        view = MinesView(self, game, cashout_label)
        message = await interaction.followup.send(embed=embed, view=view)
        view.message = message

    async def handle_mines_cell(self, interaction: discord.Interaction, view: MinesView, index: int):
        game = view.game

        if index in game.mine_positions:
            embed = await self._resolve_mines_loss(game)
            view.disable_all()
            await interaction.response.edit_message(embed=embed, view=view)
            self.active_transactions.discard(game.user_id)
            return

        game.opened_cells.add(index)
        view.open_cell_button(index)

        if len(game.opened_cells) >= game.max_safe_cells:
            # 열 수 있는 안전칸을 전부 열었다 - 더 열 칸이 없으니 자동으로 캐시아웃 처리한다.
            embed = await self._resolve_mines_cashout(game)
            view.disable_all()
            await interaction.response.edit_message(embed=embed, view=view)
            self.active_transactions.discard(game.user_id)
            return

        embed = await self._build_mines_embed(game, finished=False)
        await interaction.response.edit_message(embed=embed, view=view)

    async def handle_mines_cashout(self, interaction: discord.Interaction, view: MinesView):
        game = view.game
        embed = await self._resolve_mines_cashout(game)
        view.disable_all()
        await interaction.response.edit_message(embed=embed, view=view)
        self.active_transactions.discard(game.user_id)

    async def _resolve_mines_loss(self, game: MinesGame) -> discord.Embed:
        """지뢰를 밟았을 때의 정산. {points}는 원래 보유 포인트나 차감 후 잔액이 아니라
        정확히 '이번에 잃은 베팅액'(game.bet)이어야 한다."""
        user_data = await self.bot.get_user_data(str(game.user_id), str(game.guild_id))
        current_points = user_data.get("points", 0)
        user_data["points"] = max(0, current_points - game.bet)

        save_ok = await self.bot.save_user_data(str(game.user_id), str(game.guild_id), user_data)
        if not save_ok:
            title = await self.get_msg(game.guild_id, "econ_err_ledger_title")
            desc = await self.get_msg(game.guild_id, "mines_err_save_failed_hit")
            return discord.Embed(title=title, description=desc, color=discord.Color.red())

        status_msg = await self.get_msg(game.guild_id, "mines_status_mine_hit", points=game.bet)
        return await self._build_mines_embed(game, finished=True, status_msg=status_msg, reveal_mines=True)

    async def _resolve_mines_cashout(self, game: MinesGame) -> discord.Embed:
        """캐시아웃 정산. 베팅액은 시작 시점에 차감된 적이 없으므로(블랙잭 승리가 bet을 그대로
        한 번만 더하는 것과 동일한 '델타만 적용' 방식) 배율 전체가 아니라 순이익(potential_profit)만 더한다."""
        net_profit = game.potential_profit
        user_data = await self.bot.get_user_data(str(game.user_id), str(game.guild_id))
        current_points = user_data.get("points", 0)
        user_data["points"] = current_points + net_profit

        save_ok = await self.bot.save_user_data(str(game.user_id), str(game.guild_id), user_data)
        if not save_ok:
            title = await self.get_msg(game.guild_id, "econ_err_ledger_title")
            desc = await self.get_msg(game.guild_id, "mines_err_save_failed_cashout")
            return discord.Embed(title=title, description=desc, color=discord.Color.red())

        status_msg = await self.get_msg(
            game.guild_id, "mines_status_cashout",
            points=net_profit, multiplier=f"{game.current_multiplier:.2f}"
        )
        return await self._build_mines_embed(game, finished=True, status_msg=status_msg)

    async def _build_mines_embed(self, game: MinesGame, finished: bool, status_msg: str | None = None, reveal_mines: bool = False) -> discord.Embed:
        title = await self.get_msg(game.guild_id, "mines_title")
        mines_label = await self.get_msg(game.guild_id, "mines_field_mines_count")
        multiplier_label = await self.get_msg(game.guild_id, "mines_field_multiplier")
        payout_label = await self.get_msg(game.guild_id, "mines_field_payout")
        grid_label = await self.get_msg(game.guild_id, "mines_field_grid")

        if finished:
            description = status_msg
        elif game.at_cap:
            description = await self.get_msg(game.guild_id, "mines_status_max_multiplier")
        elif game.opened_cells:
            description = await self.get_msg(game.guild_id, "mines_status_safe")
        else:
            description = await self.get_msg(game.guild_id, "mines_status_playing")

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold() if finished else discord.Color.dark_green()
        )
        embed.add_field(name=mines_label, value=str(game.mine_count), inline=True)
        embed.add_field(name=multiplier_label, value=f"`{game.current_multiplier:.2f}x`", inline=True)
        embed.add_field(name=payout_label, value=f"🪙 `+{game.potential_profit:,}`" if not finished else "—", inline=True)
        embed.add_field(name=grid_label, value=game.render_grid_text(reveal_all=reveal_mines), inline=False)
        embed.set_footer(text=f"Wager: {game.bet:,} {game.currency_name}")
        return embed

    # ========================================================
    # [🔫 RUSSIAN ROULETTE TABLE]
    # ========================================================

    @app_commands.command(name="roulette", description="Play Russian Roulette against the house - pull the trigger or walk away.")
    @app_commands.describe(bet="The amount of currency you want to wager.")
    async def roulette(self, interaction: discord.Interaction, bet: int):
        await interaction.response.defer()
        user_id = interaction.user.id
        guild_id = interaction.guild_id

        if user_id in self.active_transactions:
            msg = await self.get_msg(guild_id, "econ_err_processing_blocked")
            await interaction.followup.send(msg, ephemeral=True)
            return

        economy_set = await self._get_economy_settings(guild_id)
        currency_name = economy_set.get("currency_name", "Points")
        min_bet = economy_set.get("min_bet", 10)

        # 서버 설정의 min_bet이 이 게임 자체의 최대 베팅 한도보다 커진 경우를 명확히 막는다.
        if min_bet > TABLE_MAX_BET:
            msg = await self.get_msg(guild_id, "roulette_err_table_unavailable", min_bet=min_bet, max_bet=TABLE_MAX_BET)
            await interaction.followup.send(msg, ephemeral=True)
            return

        if bet < min_bet:
            msg = await self.get_msg(guild_id, "roulette_err_min_bet", min_bet=min_bet)
            await interaction.followup.send(msg, ephemeral=True)
            return

        if bet > TABLE_MAX_BET:
            msg = await self.get_msg(guild_id, "roulette_err_max_bet", max_bet=TABLE_MAX_BET)
            await interaction.followup.send(msg, ephemeral=True)
            return

        user_data = await self.bot.get_user_data(str(user_id), str(guild_id))
        current_points = user_data.get("points", 0)

        if current_points < bet:
            msg = await self.get_msg(guild_id, "roulette_err_no_points", points=current_points)
            await interaction.followup.send(msg, ephemeral=True)
            return

        self.active_transactions.add(user_id)
        house_edge = await self._get_house_edge(guild_id)
        game = RouletteGame(user_id, guild_id, bet, currency_name, house_edge)

        trigger_label = await self.get_msg(guild_id, "roulette_btn_trigger")
        cashout_label = await self.get_msg(guild_id, "roulette_btn_cashout")
        embed = await self._build_roulette_embed(game, finished=False)
        view = RouletteView(self, game, trigger_label, cashout_label)
        message = await interaction.followup.send(embed=embed, view=view)
        view.message = message

    async def handle_roulette_trigger(self, interaction: discord.Interaction, view: RouletteView):
        game = view.game
        survived = game.pull_trigger()

        if not survived:
            embed = await self._resolve_roulette_shot(game)
            view.disable_all()
            await interaction.response.edit_message(embed=embed, view=view)
            self.active_transactions.discard(game.user_id)
            return

        view.sync_buttons()
        embed = await self._build_roulette_embed(game, finished=False, final_chamber=game.is_final_chamber)
        await interaction.response.edit_message(embed=embed, view=view)

    async def handle_roulette_cashout(self, interaction: discord.Interaction, view: RouletteView):
        game = view.game
        embed = await self._resolve_roulette_cashout(game)
        view.disable_all()
        await interaction.response.edit_message(embed=embed, view=view)
        self.active_transactions.discard(game.user_id)

    async def _resolve_roulette_shot(self, game: RouletteGame) -> discord.Embed:
        """총알을 맞았을 때의 정산. {points}는 원래 보유 포인트나 차감 후 잔액이 아니라
        정확히 '이번에 잃은 베팅액'(game.bet)이어야 한다."""
        user_data = await self.bot.get_user_data(str(game.user_id), str(game.guild_id))
        current_points = user_data.get("points", 0)
        user_data["points"] = max(0, current_points - game.bet)

        save_ok = await self.bot.save_user_data(str(game.user_id), str(game.guild_id), user_data)
        if not save_ok:
            title = await self.get_msg(game.guild_id, "econ_err_ledger_title")
            desc = await self.get_msg(game.guild_id, "roulette_err_save_failed_shot")
            return discord.Embed(title=title, description=desc, color=discord.Color.red())

        status_msg = await self.get_msg(game.guild_id, "roulette_status_shot", points=game.bet)
        return await self._build_roulette_embed(game, finished=True, status_msg=status_msg, reveal_bullet=True)

    async def _resolve_roulette_cashout(self, game: RouletteGame) -> discord.Embed:
        """캐시아웃 정산. 지뢰찾기와 동일하게 배율 전체가 아니라 순이익(potential_profit)만 더한다."""
        net_profit = game.potential_profit
        user_data = await self.bot.get_user_data(str(game.user_id), str(game.guild_id))
        current_points = user_data.get("points", 0)
        user_data["points"] = current_points + net_profit

        save_ok = await self.bot.save_user_data(str(game.user_id), str(game.guild_id), user_data)
        if not save_ok:
            title = await self.get_msg(game.guild_id, "econ_err_ledger_title")
            desc = await self.get_msg(game.guild_id, "roulette_err_save_failed_cashout")
            return discord.Embed(title=title, description=desc, color=discord.Color.red())

        status_msg = await self.get_msg(
            game.guild_id, "roulette_status_cashout",
            points=net_profit, multiplier=f"{game.current_multiplier:.2f}"
        )
        return await self._build_roulette_embed(game, finished=True, status_msg=status_msg)

    async def _build_roulette_embed(self, game: RouletteGame, finished: bool, status_msg: str | None = None,
                                     reveal_bullet: bool = False, final_chamber: bool = False) -> discord.Embed:
        title = await self.get_msg(game.guild_id, "roulette_title")
        trigger_label = await self.get_msg(game.guild_id, "roulette_field_trigger_count")
        multiplier_label = await self.get_msg(game.guild_id, "roulette_field_multiplier")
        payout_label = await self.get_msg(game.guild_id, "roulette_field_payout")
        cylinder_label = await self.get_msg(game.guild_id, "roulette_field_cylinder")

        if finished:
            description = status_msg
        elif final_chamber:
            description = await self.get_msg(game.guild_id, "roulette_status_final_chamber")
        elif game.pulls > 0:
            description = await self.get_msg(game.guild_id, "roulette_status_survived")
        else:
            description = await self.get_msg(game.guild_id, "roulette_status_playing")

        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold() if finished else discord.Color.dark_red()
        )
        embed.add_field(name=trigger_label, value=f"{game.pulls} / {ROULETTE_MAX_PULLS}", inline=True)
        embed.add_field(name=multiplier_label, value=f"`{game.current_multiplier:.2f}x`", inline=True)
        embed.add_field(name=payout_label, value=f"🪙 `+{game.potential_profit:,}`" if not finished else "—", inline=True)
        embed.add_field(name=cylinder_label, value=game.render_cylinder_text(reveal_bullet=reveal_bullet), inline=False)
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
            msg = await self.get_msg(interaction.guild_id, "shop_empty")
            await interaction.followup.send(msg)
            return

        title = await self.get_msg(interaction.guild_id, "shop_title", guild=interaction.guild.name)
        desc_hint = await self.get_msg(interaction.guild_id, "shop_desc_hint")
        embed = discord.Embed(
            title=title,
            description=desc_hint,
            color=0x5865f2,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        # 🛡️ [방어적 읽기] 필드명이 안 맞는 항목(예: 예전 대시보드 버그가 남긴 'title' 키 등) 하나
        # 때문에 상점 목록 전체가 KeyError로 죽으면 안 된다 - 로그만 남기고 안전한 기본값으로 표시한다.
        for item in shop_items:
            if not isinstance(item, dict):
                print(f"[SHOP][WARN] Skipping non-dict shop item (guild={interaction.guild_id}): {item!r}", flush=True)
                continue
            if "name" not in item:
                print(f"[SHOP][WARN] Shop item missing 'name' key (guild={interaction.guild_id}): {item!r}", flush=True)
            item_name = item.get("name", "Unknown Item")
            item_price = item.get("price", 0)
            item_desc = item.get("description", "")
            field_name = await self.get_msg(interaction.guild_id, "shop_field_item_name", name=item_name)
            field_value = await self.get_msg(interaction.guild_id, "shop_field_item_value", price=f"{item_price:,}", currency=currency_name, desc=item_desc)
            embed.add_field(name=field_name, value=field_value, inline=False)
        footer = await self.get_msg(interaction.guild_id, "shop_footer")
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    @shop_group.command(name="add", description="Add a brand new item to the server shop.")
    @app_commands.describe(
        name="The item's display name.",
        price="How much the item costs, in this server's currency.",
        description="A short line describing what the item is or does.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def shop_add(self, interaction: discord.Interaction, name: str, price: int, description: str):
        """Appends a dictionary entry into guild shop items array structure."""
        await interaction.response.defer(ephemeral=True)
        if price <= 0:
            msg = await self.get_msg(interaction.guild_id, "shop_add_err_invalid_price")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if price > 1000000000:
            msg = await self.get_msg(interaction.guild_id, "shop_add_err_price_too_high")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if len(name) > 50 or len(description) > 200:
            msg = await self.get_msg(interaction.guild_id, "shop_add_err_bounds_overflow")
            await interaction.followup.send(msg, ephemeral=True)
            return

        guild_id = interaction.guild_id
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        economy_set = nested_settings.get("economy_settings") or {}
        shop_items = economy_set.get("shop_items", [])

        if any(isinstance(item, dict) and item.get('name', '').lower() == name.lower() for item in shop_items):
            msg = await self.get_msg(guild_id, "shop_add_err_duplicate")
            await interaction.followup.send(msg, ephemeral=True)
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

        msg = await self.get_msg(guild_id, "shop_add_success", name=name, price=f"{price:,}")
        await interaction.followup.send(msg, ephemeral=True)

    async def _process_purchase(self, user_id: str, guild_id, item_name: str) -> dict:
        """구매 처리 공통 로직 - /shop buy 커맨드와 대시보드발 내부 웹훅이 둘 다 이 메서드를 거친다.
        active_transactions 락이 user_id 단위이기 때문에, 같은 유저가 명령어와 대시보드로 거의
        동시에 구매를 시도해도(더블클릭 포함) 이 안에서 자연스럽게 직렬화된다 - 두 경로가 같은
        프로세스의 같은 set을 공유하므로 별도 Redis 락이 필요 없다."""
        if user_id in self.active_transactions:
            return {"status": "locked"}

        self.active_transactions.add(user_id)
        try:
            economy_set = await self._get_economy_settings(guild_id)
            currency_name = economy_set.get("currency_name", "Points")
            shop_items = economy_set.get("shop_items", [])

            # 🛡️ [방어적 읽기] 필드명이 안 맞는 항목이 섞여 있어도 검색/구매 전체가 죽지 않게 한다.
            target_item = next(
                (item for item in shop_items if isinstance(item, dict) and item.get('name', '').lower() == item_name.lower()),
                None,
            )
            if not target_item:
                return {"status": "item_not_found", "item_name": item_name}

            item_price = target_item.get("price")
            if not isinstance(item_price, (int, float)) or item_price <= 0:
                # 🛡️ 가격이 없거나 이상한 값이면 0원 구매를 허용하는 대신 명확히 막는다 - 재화가
                # 걸린 로직은 조용히 기본값으로 넘어가면 안 된다.
                print(f"[SHOP][ERROR] Item '{item_name}' has an invalid/missing price (guild={guild_id}): {target_item!r}", flush=True)
                return {"status": "item_misconfigured", "item_name": item_name}

            user_data = await self.bot.get_user_data(user_id, str(guild_id))
            current_points = user_data.get("points", 0)

            if current_points < item_price:
                return {
                    "status": "insufficient_points", "item_name": item_name,
                    "current_points": current_points, "required_points": item_price,
                    "currency_name": currency_name,
                }

            user_data["points"] = current_points - item_price
            inventory = user_data.get("inventory", [])

            inv_item = next(
                (item for item in inventory if isinstance(item, dict) and item.get('name', '').lower() == item_name.lower()),
                None,
            )
            if inv_item:
                inv_item["quantity"] = inv_item.get("quantity", 1) + 1
            else:
                inventory.append({"name": target_item.get("name", item_name), "quantity": 1})

            user_data["inventory"] = inventory
            # 🛡️ points 차감과 inventory 지급은 이 한 번의 save_user_data 호출로 같이 저장된다(별도 쓰기
            # 두 번이 아님) - 그래서 "포인트만 빠지고 아이템은 안 들어감" 같은 부분 실패는 구조적으로
            # 불가능하다.
            save_ok = await self.bot.save_user_data(user_id, str(guild_id), user_data)
            if not save_ok:
                return {"status": "save_failed", "item_name": item_name}

            return {
                "status": "ok",
                "item_name": target_item.get("name", item_name),
                "price": item_price,
                "currency_name": currency_name,
                "remaining_points": user_data["points"],
            }
        finally:
            self.active_transactions.discard(user_id)

    @shop_group.command(name="buy", description="Purchase an item from the server shop.")
    @app_commands.describe(item_name="The exact name of the item you want to purchase.")
    async def buy_item(self, interaction: discord.Interaction, item_name: str):
        """Handles cross-validation between user points and guild shop metadata arrays."""
        await interaction.response.defer()
        user_id = str(interaction.user.id)

        result = await self._process_purchase(user_id, interaction.guild_id, item_name)
        status = result["status"]

        if status == "locked":
            msg = await self.get_msg(interaction.guild_id, "shop_buy_err_locked")
            await interaction.followup.send(msg, ephemeral=True)
        elif status == "item_not_found":
            msg = await self.get_msg(interaction.guild_id, "shop_buy_err_not_found", item_name=item_name)
            await interaction.followup.send(msg)
        elif status == "item_misconfigured":
            msg = await self.get_msg(interaction.guild_id, "shop_buy_err_misconfigured")
            await interaction.followup.send(msg, ephemeral=True)
        elif status == "insufficient_points":
            msg = await self.get_msg(
                interaction.guild_id, "shop_buy_err_insufficient",
                required=f"{result['required_points']:,}", currency=result['currency_name'],
                current=f"{result['current_points']:,}",
            )
            await interaction.followup.send(msg)
        elif status == "save_failed":
            msg = await self.get_msg(interaction.guild_id, "shop_buy_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
        else:
            msg = await self.get_msg(
                interaction.guild_id, "shop_buy_success",
                name=result['item_name'], price=f"{result['price']:,}", currency=result['currency_name'],
            )
            await interaction.followup.send(msg)

    async def handle_shop_buy_webhook(self, request: web.Request) -> web.Response:
        secret_header = request.headers.get("X-Internal-Secret", "")
        if not secret_header or not hmac.compare_digest(secret_header, INTERNAL_API_SECRET):
            print("[SHOP][WARN] Rejected shop-buy request with invalid/missing internal secret", flush=True)
            return web.Response(status=403)

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        guild_id = body.get("guild_id")
        user_id = body.get("user_id")
        item_name = body.get("item_name")
        if not all([guild_id, user_id, item_name]):
            return web.Response(status=400, text="guild_id, user_id, item_name are all required")

        result = await self._process_purchase(str(user_id), str(guild_id), str(item_name))

        status_to_http = {
            "locked": 409, "item_not_found": 404, "item_misconfigured": 400,
            "insufficient_points": 400, "save_failed": 500,
        }
        if result["status"] != "ok":
            return web.json_response(result, status=status_to_http.get(result["status"], 400))

        return web.json_response(result)

    @app_commands.command(name="inventory", description="View all items currently stored in your personal vault.")
    async def inventory_view(self, interaction: discord.Interaction):
        """Renders user inventory arrays fetched dynamically out of Supabase."""
        await interaction.response.defer()
        user_data = await self.bot.get_user_data(str(interaction.user.id), str(interaction.guild_id))
        inventory = user_data.get("inventory", [])

        if not inventory:
            msg = await self.get_msg(interaction.guild_id, "inv_empty")
            await interaction.followup.send(msg)
            return

        title = await self.get_msg(interaction.guild_id, "inv_title", user=interaction.user.display_name)
        embed = discord.Embed(
            title=title,
            color=0x2b2d31,
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        # 🛡️ [레거시 데이터 방어] /shop buy는 항상 {"name", "quantity"} 딕셔너리로 쓰지만, 예전 스키마의
        # 잔재로 문자열만 들어있는 레코드가 실제로 발견됐다(item['name']에서 TypeError로 /inventory 전체가
        # 죽었음). 형태가 맞지 않는 항목도 죽지 않고 최대한 그대로 보여준다.
        description_lines = []
        for item in inventory:
            if isinstance(item, dict) and "name" in item:
                description_lines.append(f"• **{item['name']}** ` x{item.get('quantity', 1)} `")
            else:
                description_lines.append(f"• **{item}** ` x1 `")
        embed.description = "\n".join(description_lines)
        footer = await self.get_msg(interaction.guild_id, "inv_footer")
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    # ========================================================
    # [ADMINISTRATIVE CASH FLOW OVERRIDES]
    # ========================================================

    eco_group = app_commands.Group(name="eco", description="Admin monetary allocation tools", default_permissions=discord.Permissions(manage_guild=True))

    @eco_group.command(name="give", description="Give points to a user.")
    @app_commands.describe(target="The member to give points to.", amount="How many points to give.")
    async def eco_give(self, interaction: discord.Interaction, target: discord.User, amount: int):
        """Mutates user financial schema by injecting points directly."""
        await interaction.response.defer(ephemeral=True)
        if target.bot:
            msg = await self.get_msg(interaction.guild_id, "eco_err_bot_target")
            await interaction.followup.send(msg, ephemeral=True)
            return
        if amount <= 0:
            msg = await self.get_msg(interaction.guild_id, "eco_give_err_invalid_amount")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if amount > 1000000000:
            msg = await self.get_msg(interaction.guild_id, "eco_give_err_max_amount")
            await interaction.followup.send(msg, ephemeral=True)
            return

        economy_set = await self._get_economy_settings(interaction.guild_id)
        currency_name = economy_set.get("currency_name", "Points")

        user_data = await self.bot.get_user_data(str(target.id), str(interaction.guild_id))
        user_data["points"] = min(2000000000, user_data.get("points", 0) + amount)
        save_ok = await self.bot.save_user_data(str(target.id), str(interaction.guild_id), user_data)

        if not save_ok:
            msg = await self.get_msg(interaction.guild_id, "eco_give_err_save_failed", mention=target.mention)
            await interaction.followup.send(msg, ephemeral=True)
            return

        print(f"[ECONOMY ADMIN] Granted {amount} to {target.name} via admin override.")
        msg = await self.get_msg(interaction.guild_id, "eco_give_success", amount=f"{amount:,}", currency=currency_name, mention=target.mention)
        await interaction.followup.send(msg, ephemeral=True)

    @eco_group.command(name="take", description="Take points from a user.")
    @app_commands.describe(target="The member to take points from.", amount="How many points to take.")
    async def eco_take(self, interaction: discord.Interaction, target: discord.User, amount: int):
        """Enforces administrative budget cuts/penalties onto user point structures."""
        await interaction.response.defer(ephemeral=True)
        if target.bot:
            msg = await self.get_msg(interaction.guild_id, "eco_err_bot_target")
            await interaction.followup.send(msg, ephemeral=True)
            return
        if amount <= 0:
            msg = await self.get_msg(interaction.guild_id, "eco_take_err_invalid_amount")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if amount > 1000000000:
            msg = await self.get_msg(interaction.guild_id, "eco_take_err_max_amount")
            await interaction.followup.send(msg, ephemeral=True)
            return

        economy_set = await self._get_economy_settings(interaction.guild_id)
        currency_name = economy_set.get("currency_name", "Points")

        user_data = await self.bot.get_user_data(str(target.id), str(interaction.guild_id))
        current_points = user_data.get("points", 0)

        user_data["points"] = max(0, current_points - amount)
        save_ok = await self.bot.save_user_data(str(target.id), str(interaction.guild_id), user_data)

        if not save_ok:
            msg = await self.get_msg(interaction.guild_id, "eco_take_err_save_failed", mention=target.mention)
            await interaction.followup.send(msg, ephemeral=True)
            return

        print(f"[ECONOMY ADMIN] Deducted {amount} from {target.name} via admin override.")
        msg = await self.get_msg(interaction.guild_id, "eco_take_success", amount=f"{amount:,}", currency=currency_name, mention=target.mention)
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(KyvoEconomy(bot))
