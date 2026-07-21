import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import time
import random
import io
import os
from PIL import Image, ImageDraw, ImageFont
import aiohttp

class KyvoLeveling(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.xp_cooldowns = {}
        self.font_path = "Font.ttf"
        self.fonts = {}
        self.preload_fonts()

    def preload_fonts(self):
        if os.path.exists(self.font_path):
            try:
                self.fonts["name"] = ImageFont.truetype(self.font_path, 42)
                self.fonts["large"] = ImageFont.truetype(self.font_path, 46)
                self.fonts["small"] = ImageFont.truetype(self.font_path, 18)
                self.fonts["xp"] = ImageFont.truetype(self.font_path, 22)
                print("[PERFORMANCE LOG] Leveling Font Cache Refreshed.")
            except Exception:
                self.set_default_fonts()
        else:
            self.set_default_fonts()

    def set_default_fonts(self):
        default = ImageFont.load_default()
        self.fonts = {k: default for k in ["name", "large", "small", "xp"]}

    def calculate_xp_for_next_level(self, level: int) -> int:
        return 5 * (level ** 2) + 50 * level + 100

    # ══════════════════════════════════════════════════════════
    #  XP 쿨다운 (Redis SET NX EX, automod의 check_spam/_check_spam_local과 동일한
    #  "Redis 우선 시도 -> 예외 시 로컬 dict 폴백" 구조)
    # ══════════════════════════════════════════════════════════
    async def _try_start_xp_cooldown(self, guild_id: int, user_id: str) -> bool:
        """True면 쿨다운이 새로 시작된 것(=XP 지급 가능), False면 아직 쿨다운 중."""
        key = f"cooldown:{guild_id}:{user_id}"
        try:
            acquired = await self.redis.set(key, "1", ex=60, nx=True)
            return bool(acquired)
        except Exception as e:
            print(f"[XP][WARN] Redis cooldown check failed, falling back to local: {type(e).__name__}: {e}", flush=True)
            return self._try_start_xp_cooldown_local(user_id)

    def _try_start_xp_cooldown_local(self, user_id: str) -> bool:
        now = time.time()
        last = self.xp_cooldowns.get(user_id)
        if last is not None and now - last < 60:
            return False
        self.xp_cooldowns[user_id] = now
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 🐞 [XP DEBUG] 아래 print들은 어느 조건에서 스킵됐는지 추적하기 위한 디버그 로그.
        if message.author.bot:
            print(f"[XP DEBUG] SKIP(author_is_bot): author={message.author.id}")
            return
        if not message.guild:
            print(f"[XP DEBUG] SKIP(no_guild/DM): author={message.author.id}")
            return

        guild_id = message.guild.id
        user_id = str(message.author.id)

        # 🛡️ [Redis 전환] 로컬 dict는 Render 콜드 스타트/재시작에 매번 초기화되던 문제가 있었다.
        # Redis SET NX EX 60이 원자적으로 "쿨다운 시작 성공 여부"를 판단해주므로 그걸로 대체하고,
        # Redis 장애 시에만 automod와 동일한 방식으로 로컬 dict로 안전하게 폴백한다.
        cooldown_started = await self._try_start_xp_cooldown(guild_id, user_id)
        if not cooldown_started:
            print(f"[XP DEBUG] SKIP(cooldown): user={user_id} guild={guild_id}")
            return

        # 🛡️ [아키텍처 최적화] Redis Cache-Aside를 타는 KyvoBaseCog.get_guild_settings로 교체.
        # 예전엔 top-level "leveling_settings" 컬럼을 직접 select했는데, 대시보드는 그 컬럼에 절대
        # 안 쓰고 settings JSON 내부(settings.leveling_settings)에만 쓰기 때문에 그 컬럼은 항상 비어있었다.
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        leveling_set = nested_settings.get("leveling_settings") or {}

        blacklisted_channels = leveling_set.get("blacklisted_channels", [])
        if message.channel.id in blacklisted_channels or str(message.channel.id) in blacklisted_channels:
            print(f"[XP DEBUG] SKIP(blacklisted_channel): channel={message.channel.id} guild={guild_id}")
            return

        xp_rate = float(leveling_set.get("xp_rate", 1.0))
        print(f"[XP DEBUG] GRANT: user={user_id} guild={guild_id} xp_rate={xp_rate}")

        user_data = await self.bot.get_user_data(user_id, str(guild_id))
        current_xp = user_data.get("xp", 0)
        current_level = user_data.get("level", 1)

        xp_gained = int(random.randint(15, 25) * xp_rate)
        new_xp = current_xp + xp_gained
        xp_needed = self.calculate_xp_for_next_level(current_level)

        leveled_up = False
        while new_xp >= xp_needed:
            new_xp -= xp_needed
            current_level += 1
            xp_needed = self.calculate_xp_for_next_level(current_level)
            leveled_up = True

        user_data["xp"] = new_xp
        user_data["level"] = current_level

        # 🛡️ [정직성 수정] 예전엔 레벨업 축하 메시지/역할 지급을 먼저 하고 나서 저장을 시도했다 -
        # 저장이 실패하면 유저는 레벨업했다고 믿고 보상 역할까지 받았는데 실제 DB엔 반영이 안 되는
        # 상황이 가능했다. 이제 저장 성공을 먼저 확인한 뒤에만 축하/보상을 진행한다.
        save_ok = await self.bot.save_user_data(user_id, str(guild_id), user_data)
        if not save_ok:
            print(f"[XP][ERROR] Failed to persist XP for user={user_id} guild={guild_id}", flush=True)
            return

        if leveled_up:
            try:
                alert_embed = discord.Embed(
                    title="🎉 LEVEL UP!",
                    description=f"Congratulations {message.author.mention}! You advanced to **Level {current_level}**.",
                    color=discord.Color.gold()
                )
                await message.channel.send(embed=alert_embed, delete_after=10.0)
            except discord.Forbidden:
                pass

            role_rewards = leveling_set.get("role_rewards", {})
            target_role_id = role_rewards.get(str(current_level))
            if target_role_id:
                reward_role = message.guild.get_role(int(target_role_id))
                if reward_role:
                    try:
                        await message.author.add_roles(reward_role, reason=f"[KYVO REWARD]")
                    except discord.Forbidden:
                        pass

    @app_commands.command(name="level", description="Generate your customized server rank card.")
    async def level(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id = str(interaction.user.id)
        guild_id = interaction.guild_id

        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        leveling_set = nested_settings.get("leveling_settings") or {}

        def clean_hex_color(hex_str, fallback):
            if not hex_str: return fallback
            clean = str(hex_str).strip()
            if not clean.startswith('#'): clean = f"#{clean}"
            return clean if len(clean) in [4, 7, 9] else fallback

        card_color = clean_hex_color(leveling_set.get("card_color"), "#5865F2")
        card_bg_color = clean_hex_color(leveling_set.get("card_bg_color"), "#1E1F22")
        overlay_opacity = float(leveling_set.get("overlay_opacity", 0.6))
        background_url = str(leveling_set.get("background_url", "")).strip()

        user_data = await self.bot.get_user_data(user_id, str(guild_id))
        current_xp = user_data.get("xp", 0)
        current_level = user_data.get("level", 1)
        xp_needed = self.calculate_xp_for_next_level(current_level)

        actual_rank = 1
        try:
            # 🛡️ guild_id 필터 추가 - users가 서버별 구조로 바뀌면서 이제 실제로 스코프가 가능해졌다.
            # 예전엔 필터가 없어서 랭킹 하나 보여주는 데 전체 서버의 유저를 다 긁어왔었다.
            response = self.bot.supabase.table("users").select("user_id", "xp", "level") \
                .eq("guild_id", str(guild_id)).execute()
            if response.data:
                leaderboard = []
                for user_row in response.data:
                    row_user_id = user_row.get("user_id")
                    if not row_user_id: continue
                    member = interaction.guild.get_member(int(row_user_id))
                    if member is not None and not member.bot:
                        leaderboard.append({
                            "user_id": str(row_user_id),
                            "level": user_row.get("level", 1),
                            "xp": user_row.get("xp", 0)
                        })
                leaderboard.sort(key=lambda x: (x["level"], x["xp"]), reverse=True)
                for index, entry in enumerate(leaderboard):
                    if entry["user_id"] == user_id:
                        actual_rank = index + 1
                        break
        except Exception:
            pass

        base_w, base_h = 920, 240
        card = None

        if background_url and background_url.startswith("http"):
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                async with aiohttp.ClientSession() as session:
                    async with session.get(background_url, headers=headers, timeout=5) as resp:
                        if resp.status == 200:
                            bg_data = await resp.read()
                            card = Image.open(io.BytesIO(bg_data)).convert("RGBA").resize((base_w, base_h))
            except Exception:
                pass

        if card is None:
            card = Image.new("RGBA", (base_w, base_h), color=card_bg_color)

        # ⚡ 1:1 SYNC FIX: 웹 대시보드처럼 사방에 16px 마진을 둔 '인플라이트 내부 캡슐' 상자 렌더링
        overlay = Image.new("RGBA", card.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rounded_rectangle(
            [16, 16, base_w - 16, base_h - 16], 
            radius=12, 
            fill=(15, 15, 26, int(overlay_opacity * 255))
        )
        card = Image.alpha_composite(card.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(card)

        # ⚡ USERNAME POSITION: 상단 여백에 맞춰 위로 올림
        draw.text((240, 52), interaction.user.display_name, fill="#ffffff", font=self.fonts["name"])

        # ⚡ RIGHT-ALIGNED STAT MATRIX SYSTEM: 오른쪽 끝(860px) 기준으로 역산 정렬
        right_edge = 860 
        
        # 1. LEVEL VALUE
        level_str = f"{current_level}"
        level_w = draw.textlength(level_str, font=self.fonts["large"])
        level_x = right_edge - level_w
        draw.text((level_x, 48), level_str, fill=card_color, font=self.fonts["large"])

        # 2. LEVEL LABEL
        level_label_w = draw.textlength("LEVEL ", font=self.fonts["small"])
        level_label_x = level_x - level_label_w - 4
        draw.text((level_label_x, 68), "LEVEL", fill="#b5bac1", font=self.fonts["small"])

        # 3. RANK VALUE
        rank_str = f"#{actual_rank}"
        rank_w = draw.textlength(rank_str, font=self.fonts["large"])
        rank_x = level_label_x - rank_w - 20
        draw.text((rank_x, 48), rank_str, fill="#ffffff", font=self.fonts["large"])

        # 4. RANK LABEL
        rank_label_w = draw.textlength("RANK ", font=self.fonts["small"])
        rank_label_x = rank_x - rank_label_w - 4
        draw.text((rank_label_x, 68), "RANK", fill="#b5bac1", font=self.fonts["small"])

        # ⚡ PROGRESS BAR POSITION: 중간지점(y=118)으로 쾌적하게 밸런스 조정
        bar_x, bar_y, bar_w, bar_h = 240, 118, 620, 24
        progress_ratio = min(current_xp / xp_needed, 1.0)
        current_bar_w = int(bar_w * progress_ratio)

        draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=12, fill="#2b2d31")
        if current_bar_w > 0:
            draw.rounded_rectangle([bar_x, bar_y, bar_x + current_bar_w, bar_y + bar_h], radius=12, fill=card_color)

        # ⚡ XP DATA TEXT POSITION: 게이지 바 바로 하단(y=155)에 배치
        xp_text = f"{current_xp:,} / {xp_needed:,} XP"
        draw.text((245, 155), xp_text, fill="#b5bac1", font=self.fonts["xp"])

        # AVATAR MASK RENDER (45, 45 위치 고수)
        draw.ellipse((42, 42, 198, 198), outline=card_color, width=3)
        avatar_url = interaction.user.display_avatar.with_format("png").url
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(avatar_url, timeout=4) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        avatar_img = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((150, 150))
                        mask = Image.new("L", (150, 150), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse((0, 0, 150, 150), fill=255)
                        card.paste(avatar_img, (45, 45), mask=mask)
        except Exception:
            draw.ellipse((45, 45, 195, 195), fill=card_color)

        with io.BytesIO() as image_binary:
            card.save(image_binary, "PNG")
            image_binary.seek(0)
            file = discord.File(fp=image_binary, filename="rank_card.png")
            await interaction.followup.send(file=file)

async def setup(bot):
    await bot.add_cog(KyvoLeveling(bot))
