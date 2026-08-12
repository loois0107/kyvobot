import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import io
import os
import aiohttp
from PIL import Image, ImageDraw, ImageFont

class Welcome(KyvoBaseCog):
    # 🛡️ [한글 지원] Font.ttf(Roboto Bold)는 한글 글리프가 아예 없어서(실측 확인: 서로 다른
    # 한글 글자를 그려도 전부 같은 "글리프 없음" 박스로 렌더링됨) 한국어 서버에서 카드 이미지에
    # 한글을 그리면 깨진다. FontKR.otf(Pretendard Bold, SIL OFL 라이선스)를 언어별로 별도 로드해서
    # 해결한다 - 기존 Roboto와 굵기/톤이 비슷하게 맞춰지도록 고른 폰트다.
    FONT_PATHS = {"en": "Font.ttf", "ko": "FontKR.otf"}
    FONT_SIZES = {"title": 36, "sub": 42, "count": 20}

    def __init__(self, bot):
        super().__init__(bot)
        self.fonts: dict[str, dict[str, ImageFont.FreeTypeFont]] = {}
        self.preload_fonts()

    def preload_fonts(self):
        for lang, path in self.FONT_PATHS.items():
            self.fonts[lang] = self._load_font_set(path)

    def _load_font_set(self, path: str) -> dict:
        if os.path.exists(path):
            try:
                return {key: ImageFont.truetype(path, size) for key, size in self.FONT_SIZES.items()}
            except Exception:
                pass
        # 폰트 파일이 없거나 로드에 실패하면 언어별로 각각 안전하게 폴백한다(다른 언어 폰트를
        # 대신 쓰지 않음 - 그것도 결국 그 언어 글리프가 없을 수 있어 동일한 문제가 재발한다).
        default = ImageFont.load_default()
        return {key: default for key in self.FONT_SIZES}

    def get_fonts(self, lang: str) -> dict:
        return self.fonts.get(lang, self.fonts["en"])

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        # 🛡️ [안전망 추가] 이 리스너는 원래 try/except가 전혀 없었다 - channel_id/card_color가
        # (대시보드 UI는 막아도 API를 직접 건드리면) 잘못된 형식으로 저장되면 int()/discord.Color.from_str()
        # 등에서 예외가 나서, 그 순간부터 이 서버의 모든 신규 멤버 웰컴 카드가 조용히 영구 중단됐다
        # (on_member_remove는 처음부터 try/except가 있어서 이 문제가 없었음 - 그것과 동일한 패턴으로 맞춘다).
        try:
            guild_id = member.guild.id

            # 🛡️ [아키텍처 최적화] Redis Cache-Aside를 타는 단일 조회로 통합 - 예전엔 autorole용/welcome_settings용
            # DB 왕복이 멤버 한 명 들어올 때마다 두 번씩 발생했다. guild_settings에 welcome_settings라는 단독
            # 컬럼은 실존하지 않는다 (42703로 확인됨) - autorole_id와 마찬가지로 settings JSON 안에 중첩되어 있다.
            row = await self.get_guild_settings(guild_id)
            nested_settings = row.get("settings") or {}

            autorole_id = nested_settings.get("autorole_id")
            if autorole_id:
                role = member.guild.get_role(int(autorole_id))
                if role:
                    try:
                        await member.add_roles(role, reason="KYVO AUTOMOD: New member autorole injection.")
                    except discord.Forbidden:
                        pass

            welcome_set = nested_settings.get("welcome_settings") or {}
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

            # 🛡️ [한글 지원 + 평문화] 예전엔 "WELCOME TO THE SERVER"/"Operative #..."가 서버 언어와
            # 무관하게 항상 하드코딩된 영어로 그려졌다 - 이미 위에서 불러온 row에 language가 최상위
            # 필드로 있으므로 추가 쿼리 없이 바로 꺼내 쓴다. 폰트도 언어에 맞는 걸 골라야 실제로
            # 화면에 제대로 보인다(get_fonts 참고).
            lang = row.get("language", "en")
            fonts = self.get_fonts(lang)
            welcome_line = await self.get_msg(guild_id, "welcome_card_title")
            count_line = await self.get_msg(guild_id, "welcome_card_count", count=f"{len(member.guild.members):,}")

            draw.text((240, 55), welcome_line, fill="#b5bac1", font=fonts["title"])
            draw.text((240, 100), f"{member.display_name}", fill=card_color, font=fonts["sub"])
            draw.text((240, 160), count_line, fill="#ffffff", font=fonts["count"])

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

                # 🛡️ [i18n 배선] goodbye(on_member_remove)와 동일한 방식으로 서버 언어(guild_settings.language)를
                # 따르도록 get_msg로 교체 - 예전엔 이 임베드만 언어 설정과 무관하게 항상 영어로 하드코딩돼 있었다.
                # (카드 이미지 위에 PIL로 직접 그리는 "WELCOME TO THE SERVER"/"Operative #..." 텍스트는 폰트
                # 렌더링이 걸린 별도 범위라 이번엔 건드리지 않는다 - 3단계에서 별도로 다룰 것)
                embed_title = await self.get_msg(guild_id, "welcome_title")
                embed_desc = await self.get_msg(guild_id, "welcome_desc", mention=member.mention, guild=member.guild.name)

                welcome_embed = discord.Embed(
                    title=embed_title,
                    description=embed_desc,
                    color=discord.Color.from_str(card_color)
                )
                welcome_embed.set_image(url="attachment://welcome_card.png")
                await channel.send(file=file, embed=welcome_embed)
        except Exception as e:
            print(f"[WELCOME ERROR] {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot: return
        guild_id = member.guild.id
        try:
            row = await self.get_guild_settings(guild_id)
            nested_settings = row.get("settings") or {}
            if not nested_settings.get("goodbye_enabled", False):
                return

            channel_id = nested_settings.get("goodbye_channel_id")
            if not channel_id: return

            channel = member.guild.get_channel(int(channel_id))
            if not channel: return

            # 🛡️ [i18n 배선 수리] 죽은 self.bot.locale_manager 대신, 서버 설정 언어(guild_settings.language) 기반
            # KyvoBaseCog.get_msg로 교체 - 예전엔 매번 AttributeError가 나서 이 임베드가 아예 발송된 적이 없었다.
            #
            # 🛡️ [기본 문구도 로케일화] 관리자가 대시보드에서 퇴장 메시지를 안 적었을 때 쓰던 기본값이
            # "Has disconnected from the grid."로 하드코딩돼 있었다 - 서버 언어와 무관하게 항상 영어였고
            # 톤도 이 참에 같이 정리한다.
            default_msg = await self.get_msg(guild_id, "goodbye_default_message")

            # 🛡️ 대시보드에서 필드를 비워서 "기본값으로 리셋"하면 빈 문자열("")이 저장될 수 있다 -
            # .get(key, default)는 키가 존재하면 그 값(빈 문자열)을 그대로 쓰므로, or로 falsy(빈 문자열
            # 포함)까지 기본값으로 대체해야 실제로 리셋처럼 동작한다.
            raw_msg = nested_settings.get("goodbye_message") or default_msg
            formatted_msg = raw_msg.replace("{username}", member.name)\
                                   .replace("{server}", member.guild.name)\
                                   .replace("{member_count}", str(member.guild.member_count))

            title = await self.get_msg(guild_id, "goodbye_title")
            desc = await self.get_msg(
                guild_id, "goodbye_desc",
                member=member.name, guild=member.guild.name, count=f"{member.guild.member_count:,}",
            )
            footer_text = await self.get_msg(guild_id, "goodbye_footer")

            embed = discord.Embed(
                title=title,
                description=f"{desc}\n\n> {formatted_msg}",
                color=discord.Color.from_str("#FF0055"),
                timestamp=discord.utils.utcnow()
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=footer_text)

            await channel.send(embed=embed)
        except Exception as e:
            print(f"[GOODBYE ERROR] {e}")

async def setup(bot):
    await bot.add_cog(Welcome(bot))
