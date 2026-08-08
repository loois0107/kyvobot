import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import time
import random
import io
import os
from PIL import Image, ImageDraw, ImageFont, ImageOps
import aiohttp

# 🔗 cogs/onboarding.py와 동일한 패턴 - 대시보드 도메인을 환경변수로 주입받아 딥링크를 만든다.
DASHBOARD_BASE_URL = (os.getenv("DASHBOARD_BASE_URL") or "").rstrip("/")
# 🛡️ discord.py는 Button(url=...)의 스킴을 검사하지 않는다 - 잘못된 값(스킴 누락 등)을 그대로
# Discord API에 보내면 followup.send()가 HTTPException으로 터져서 랭크카드 이미지 전송 자체가
# 실패한다. DASHBOARD_BASE_URL은 개발자가 설정하는 값이라 위험은 낮지만, 미리 걸러서 잘못
# 설정됐을 때도 최소한 이미지는 정상 전송되게(버튼만 빠짐) 한다.
DASHBOARD_BASE_URL_VALID = DASHBOARD_BASE_URL.startswith(("http://", "https://"))


def clean_hex_color(hex_str, fallback):
    if not hex_str:
        return fallback
    clean = str(hex_str).strip()
    if not clean.startswith('#'):
        clean = f"#{clean}"
    return clean if len(clean) in [4, 7, 9] else fallback


def fit_card_background(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Crops+resizes `image` to `size` preserving its source aspect ratio - the same
    "cover" behavior as the dashboard preview's CSS `background-size: cover` (see
    app/(app)/profile/[guildId]/page.tsx). A bare `.resize(size)` instead stretches/squishes
    the source to fit, ignoring its aspect ratio, which is what produced the distorted cards."""
    return ImageOps.fit(image, size, method=Image.LANCZOS)


def resolve_card_settings(leveling_set: dict, user_override: dict | None) -> dict:
    """서버 기본값(leveling_set)과 개인 대시보드 오버라이드(user_override)를 필드 단위로
    합친다 - 오버라이드에 값이 있으면(None이 아니면) 그걸 쓰고, 없으면 서버 기본값,
    그마저 없으면 하드코딩된 최종 폴백. user_override가 None/빈 dict여도 안전하게 동작한다."""
    user_override = user_override or {}
    return {
        "card_color": clean_hex_color(
            user_override.get("card_color") or leveling_set.get("card_color"), "#5865F2"),
        "card_bg_color": clean_hex_color(
            user_override.get("card_bg_color") or leveling_set.get("card_bg_color"), "#1E1F22"),
        "overlay_opacity": float(
            user_override.get("overlay_opacity")
            if user_override.get("overlay_opacity") is not None
            else leveling_set.get("overlay_opacity", 0.6)),
        "background_url": str(
            user_override.get("background_url") or leveling_set.get("background_url", "")).strip(),
    }


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
                title = await self.get_msg(guild_id, "level_up_title")
                desc = await self.get_msg(guild_id, "level_up_desc", mention=message.author.mention, level=current_level)
                alert_embed = discord.Embed(
                    title=title,
                    description=desc,
                    color=discord.Color.gold()
                )
                await message.channel.send(embed=alert_embed, delete_after=10.0)
            except discord.Forbidden:
                pass

            role_rewards = leveling_set.get("role_rewards", {})
            target_role_id = role_rewards.get(str(current_level))
            if target_role_id:
                reward_role = message.guild.get_role(int(target_role_id))
                if reward_role is None:
                    print(f"[LEVEL_REWARD][WARN] Configured reward role {target_role_id} no longer exists "
                          f"(guild={guild_id}, level={current_level})", flush=True)
                else:
                    bot_member = message.guild.me
                    if bot_member is not None and bot_member.top_role <= reward_role:
                        print(f"[LEVEL_REWARD][WARN] Role hierarchy: bot's top role '{bot_member.top_role}' "
                              f"is not above reward role '{reward_role.name}' "
                              f"(guild={guild_id}, level={current_level})", flush=True)
                    else:
                        try:
                            await message.author.add_roles(reward_role, reason="[KYVO REWARD]")
                        except discord.Forbidden:
                            print(f"[LEVEL_REWARD][ERROR] Forbidden while granting reward role "
                                  f"'{reward_role.name}' to user={user_id} "
                                  f"(guild={guild_id}, level={current_level})", flush=True)
                        except discord.HTTPException as e:
                            print(f"[LEVEL_REWARD][ERROR] HTTPException while granting reward role "
                                  f"'{reward_role.name}': {type(e).__name__}: {e} "
                                  f"(guild={guild_id}, level={current_level})", flush=True)

    @app_commands.command(name="level", description="Generate your customized server rank card.")
    async def level(self, interaction: discord.Interaction):
        await interaction.response.defer()
        user_id = str(interaction.user.id)
        guild_id = interaction.guild_id

        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        leveling_set = nested_settings.get("leveling_settings") or {}

        # 🛡️ 개인 대시보드 오버라이드 - 필드 단위로 서버 기본값보다 우선한다. 행이 없거나
        # DB 조회가 실패해도(장애 포함) 서버 기본값으로 조용히 폴백하지, 카드 생성 자체를 막지 않는다.
        user_override = None
        try:
            override_res = self.bot.supabase.table("user_card_overrides").select("*") \
                .eq("guild_id", str(guild_id)).eq("user_id", user_id).execute()
            if override_res.data:
                user_override = override_res.data[0]
        except Exception as e:
            print(f"[RANK_CARD][WARN] Failed to fetch user_card_overrides (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)

        card_settings = resolve_card_settings(leveling_set, user_override)
        card_color = card_settings["card_color"]
        card_bg_color = card_settings["card_bg_color"]
        overlay_opacity = card_settings["overlay_opacity"]
        background_url = card_settings["background_url"]

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
                            card = fit_card_background(Image.open(io.BytesIO(bg_data)).convert("RGBA"), (base_w, base_h))
                        else:
                            print(f"[RANK_CARD][WARN] background_url fetch returned status {resp.status}, "
                                  f"falling back to solid color (guild={guild_id}, user={user_id}, url={background_url})", flush=True)
            except Exception as e:
                print(f"[RANK_CARD][WARN] background_url fetch/parse failed, falling back to solid color: "
                      f"{type(e).__name__}: {e} (guild={guild_id}, user={user_id}, url={background_url})", flush=True)

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

        # 🔗 개인 대시보드(/profile/{guild_id})로 가는 URL 버튼 - 링크 스타일이라 클릭해도
        # 디스코드 클라이언트가 바로 브라우저를 여는 것만으로 끝나고, 봇 서버로 인터랙션이
        # 오지 않는다(custom_id 기반 버튼과 달리 콜백 등록이 필요 없음). /level은 관리자가
        # 아닌 일반 멤버도 매번 쓰는 명령어라, 관리자만 열 수 있는 /dashboard 사이드바/헤더
        # 진입점과 달리 실제 타겟 유저층(랭크카드를 꾸미고 싶은 본인)에게 직접 닿는 유일한 경로다.
        # 🛡️ send_kwargs에서 아예 키를 빼야 한다 - discord.py의 followup.send는 view=None을
        # "view 없음"이 아니라 진짜 View 객체처럼 처리하려다 크래시한다(내부적으로 MISSING
        # 센티널과 비교하지 None과 비교하지 않음). DASHBOARD_BASE_URL 미설정 시엔 view 자체를
        # 아예 안 넘겨야 기존 동작(파일만 전송)이 그대로 유지된다.
        send_kwargs = {}
        if DASHBOARD_BASE_URL and DASHBOARD_BASE_URL_VALID:
            profile_button_label = await self.get_msg(guild_id, "rank_card_profile_button")
            leaderboard_button_label = await self.get_msg(guild_id, "rank_card_leaderboard_button")
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label=profile_button_label,
                style=discord.ButtonStyle.link,
                url=f"{DASHBOARD_BASE_URL}/profile/{guild_id}",
            ))
            # 🔗 개인 대시보드의 세 번째 탭(리더보드)으로 바로 연결 - /leaderboard 커맨드가
            # 따로 없어서, 실사용자가 매번 실행하는 /level에 함께 붙이는 게 가장 눈에 띈다.
            view.add_item(discord.ui.Button(
                label=leaderboard_button_label,
                style=discord.ButtonStyle.link,
                url=f"{DASHBOARD_BASE_URL}/profile/{guild_id}/leaderboard",
            ))
            send_kwargs["view"] = view

        with io.BytesIO() as image_binary:
            card.save(image_binary, "PNG")
            image_binary.seek(0)
            file = discord.File(fp=image_binary, filename="rank_card.png")
            await interaction.followup.send(file=file, **send_kwargs)

async def setup(bot):
    await bot.add_cog(KyvoLeveling(bot))
