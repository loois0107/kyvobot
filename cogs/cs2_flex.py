import os
import io
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
import aiohttp
from PIL import Image, ImageDraw, ImageFont

from cogs.base import KyvoBaseCog

# 🛡️ [Fail-Fast] 키 없이는 아무 것도 할 수 없다 - 로드 시점에 즉시 실패시켜서 main.py의 기존
# per-extension try/except([CRITICAL LAYER ERROR] 로그)에 걸리게 한다 (tier_verify.py/twitch.py와 동일).
STEAM_API_KEY = os.environ.get("STEAM_API_KEY")
if not STEAM_API_KEY:
    raise RuntimeError(
        "[CS2_FLEX] STEAM_API_KEY environment variable is not set. "
        "Get one from https://steamcommunity.com/dev/apikey before starting the bot."
    )

CS2_APP_ID = 730
CS2_INVENTORY_CONTEXT_ID = 2
CS2_TOP_ITEMS_COUNT = 4  # 카드에 보여줄 개수 = 점수 산정에 반영할 개수 (이상치 방지)
CS2_CACHE_TTL_HOURS = 24
CS2_MAX_NEW_LOOKUPS_PER_RUN = 10
CS2_LOCK_TTL_SECONDS = 60
CS2_HTTP_TIMEOUT_SECONDS = 8.0
CS2_STEAM_REQUEST_PACING_SECONDS = 1.0

KNIFE_GLOVE_BONUS = 50
STATTRAK_BONUS = 2
RARITY_SCORES = {
    "Covert": 8, "Classified": 5, "Restricted": 3,
    "Mil-Spec Grade": 2, "Industrial Grade": 1, "Consumer Grade": 1,
}

STEAM_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def extract_steam_identifier(raw_input: str) -> str:
    """SteamID64 또는 프로필 URL(둘 중 어느 형태로 와도)에서 식별자 부분만 뽑아낸다."""
    raw = raw_input.strip().rstrip("/")
    if "steamcommunity.com/profiles/" in raw:
        return raw.split("steamcommunity.com/profiles/")[-1].split("/")[0]
    if "steamcommunity.com/id/" in raw:
        return raw.split("steamcommunity.com/id/")[-1].split("/")[0]
    return raw


def score_item(rarity: str, is_knife_or_glove: bool, is_stattrak: bool) -> int:
    base = KNIFE_GLOVE_BONUS if is_knife_or_glove else RARITY_SCORES.get(rarity, 0)
    if is_stattrak:
        base += STATTRAK_BONUS
    return base


def build_item_list(descriptions: list[dict], assets: list[dict]) -> list[dict]:
    """assets(보유 수량)과 descriptions(아이템 메타데이터)를 classid+instanceid로 조인한다."""
    desc_by_key = {(d.get("classid"), d.get("instanceid", "0")): d for d in descriptions}
    counts: dict[tuple, int] = {}
    for a in assets:
        key = (a.get("classid"), a.get("instanceid", "0"))
        counts[key] = counts.get(key, 0) + 1

    items = []
    for key, count in counts.items():
        d = desc_by_key.get(key)
        if not d:
            continue
        tags = {t.get("category"): t.get("localized_tag_name") for t in (d.get("tags") or [])}
        rarity = tags.get("Rarity", "")
        item_type = tags.get("Type", "")
        name = d.get("market_hash_name") or d.get("name") or "Unknown Item"
        is_knife_or_glove = item_type in ("Knife", "Gloves") or name.startswith("★")
        is_stattrak = "StatTrak™" in name
        item_score = score_item(rarity, is_knife_or_glove, is_stattrak)
        for _ in range(count):
            items.append({
                "name": name,
                "icon_url": d.get("icon_url", ""),
                "rarity": rarity,
                "score": item_score,
            })
    return items


def compute_ranking(items: list[dict], top_n: int) -> tuple[int, list[dict]]:
    """점수 상위 top_n개만 골라 그 총합을 최종 점수로 삼는다 (이상치 방지 - 아이템 개수만
    많고 낮은 등급인 인벤토리가 소수 고가 아이템 보유자를 이기지 못하게 한다)."""
    top_items = sorted(items, key=lambda i: i["score"], reverse=True)[:top_n]
    total_score = sum(i["score"] for i in top_items)
    return total_score, top_items


class KyvoCS2Flex(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.font_path = "Font.ttf"
        self.fonts = {}
        self._preload_fonts()

    def _preload_fonts(self):
        if os.path.exists(self.font_path):
            try:
                self.fonts["title"] = ImageFont.truetype(self.font_path, 32)
                self.fonts["name"] = ImageFont.truetype(self.font_path, 26)
                self.fonts["score"] = ImageFont.truetype(self.font_path, 20)
                return
            except Exception:
                pass
        default = ImageFont.load_default()
        self.fonts = {"title": default, "name": default, "score": default}

    async def _db_call(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    # ══════════════════════════════════════════════════════════
    #  Steam Web API 헬퍼
    # ══════════════════════════════════════════════════════════
    async def _steam_api_get(self, session: aiohttp.ClientSession, url: str, params: dict) -> dict:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=CS2_HTTP_TIMEOUT_SECONDS)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Steam API {url} failed: {resp.status}")
            return await resp.json()

    async def _resolve_vanity_url(self, session: aiohttp.ClientSession, vanity: str) -> str | None:
        data = await self._steam_api_get(
            session, "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/",
            {"key": STEAM_API_KEY, "vanityurl": vanity},
        )
        resp = data.get("response", {})
        return resp.get("steamid") if resp.get("success") == 1 else None

    async def _get_player_summary(self, session: aiohttp.ClientSession, steam_id64: str) -> dict | None:
        data = await self._steam_api_get(
            session, "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
            {"key": STEAM_API_KEY, "steamids": steam_id64},
        )
        players = data.get("response", {}).get("players", [])
        return players[0] if players else None

    async def _fetch_inventory(self, session: aiohttp.ClientSession, steam_id64: str) -> dict:
        """반환: {"status": "ok", "descriptions": [...], "assets": [...]} 또는
        {"status": "private"/"empty"/"rate_limited"/"error"}. 절대 조용히 삼키지 않는다."""
        url = f"https://steamcommunity.com/inventory/{steam_id64}/{CS2_APP_ID}/{CS2_INVENTORY_CONTEXT_ID}"
        try:
            async with session.get(url, params={"l": "english", "count": 5000}, headers=STEAM_BROWSER_HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=CS2_HTTP_TIMEOUT_SECONDS)) as resp:
                if resp.status == 403:
                    print(f"[CS2][WARN] Inventory fetch for {steam_id64} returned 403 (private inventory)", flush=True)
                    return {"status": "private"}
                if resp.status == 429:
                    print(f"[CS2][WARN] Inventory fetch for {steam_id64} rate limited (429) by Steam", flush=True)
                    return {"status": "rate_limited"}
                if resp.status != 200:
                    print(f"[CS2][WARN] Inventory fetch for {steam_id64} returned status {resp.status}", flush=True)
                    return {"status": "error"}
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            print(f"[CS2][WARN] Inventory fetch timed out for {steam_id64}", flush=True)
            return {"status": "error"}
        except Exception as e:
            print(f"[CS2][ERROR] Inventory fetch failed for {steam_id64}: {type(e).__name__}: {e}", flush=True)
            return {"status": "error"}

        if not data or data.get("success") != 1:
            # 비공개 인벤토리는 403이 아니라 200+success!=1로 오는 경우도 실측으로 확인됨.
            print(f"[CS2][WARN] Inventory fetch for {steam_id64} returned 200 but success!={data.get('success') if data else None} "
                  f"(treated as private)", flush=True)
            return {"status": "private"}

        assets = data.get("assets") or []
        if not assets:
            print(f"[CS2][INFO] Inventory fetch for {steam_id64} succeeded but has no CS2 items (empty)", flush=True)
            return {"status": "empty"}

        return {"status": "ok", "descriptions": data.get("descriptions") or [], "assets": assets}

    # ══════════════════════════════════════════════════════════
    #  캐시 갱신 - Redis는 캐시 저장소가 아니라 "동시 중복조회 방지" 락으로만 쓴다.
    #  실제 스냅샷은 DB에 저장(하루 단위 재사용이라 Redis TTL보다 DB가 적합).
    # ══════════════════════════════════════════════════════════
    async def _refresh_inventory_cache(self, session: aiohttp.ClientSession, steam_id64: str) -> dict:
        lock_key = f"steam:inventory_lock:{steam_id64}"
        try:
            acquired = await self.redis.set(lock_key, "1", ex=CS2_LOCK_TTL_SECONDS, nx=True)
        except Exception as e:
            print(f"[CS2][WARN] Redis lock acquire failed, proceeding without lock: {type(e).__name__}: {e}", flush=True)
            acquired = True

        if not acquired:
            await asyncio.sleep(2.0)
            cached = await self._db_call(
                lambda: self.bot.supabase.table("steam_inventory_cache").select("*").eq("steam_id64", steam_id64).execute()
            )
            return {"status": "ok", **cached.data[0]} if cached.data else {"status": "error"}

        try:
            result = await self._fetch_inventory(session, steam_id64)
            if result["status"] != "ok":
                return result

            items = build_item_list(result["descriptions"], result["assets"])
            score, top_items = compute_ranking(items, CS2_TOP_ITEMS_COUNT)
            payload = {
                "steam_id64": steam_id64, "score": score, "item_count": len(items),
                "top_items": top_items, "cached_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._db_call(
                lambda: self.bot.supabase.table("steam_inventory_cache").upsert(payload, on_conflict="steam_id64").execute()
            )
            return {"status": "ok", **payload}
        finally:
            try:
                await self.redis.delete(lock_key)
            except Exception as e:
                print(f"[CS2][WARN] Redis lock release failed: {type(e).__name__}: {e}", flush=True)

    def _is_cache_fresh(self, row: dict | None) -> bool:
        if not row or not row.get("cached_at"):
            return False
        cached_at = datetime.fromisoformat(row["cached_at"])
        return datetime.now(timezone.utc) - cached_at < timedelta(hours=CS2_CACHE_TTL_HOURS)

    # ══════════════════════════════════════════════════════════
    #  /steam_link
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="steam_link", description="Link your Steam account (self-reported) for CS2 inventory features.")
    @app_commands.describe(steamid="Your SteamID64 or profile URL (steamcommunity.com/id/... or /profiles/...)")
    async def steam_link(self, interaction: discord.Interaction, steamid: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        identifier = extract_steam_identifier(steamid)

        async with aiohttp.ClientSession() as session:
            if identifier.isdigit() and len(identifier) == 17:
                steam_id64 = identifier
            else:
                try:
                    steam_id64 = await self._resolve_vanity_url(session, identifier)
                except Exception as e:
                    print(f"[CS2][ERROR] Vanity URL resolution failed for '{identifier}': {type(e).__name__}: {e}", flush=True)
                    msg = await self.get_msg(guild_id, "cs2_err_lookup_failed")
                    await interaction.followup.send(msg, ephemeral=True)
                    return

            if not steam_id64:
                msg = await self.get_msg(guild_id, "cs2_err_invalid_steamid")
                await interaction.followup.send(msg, ephemeral=True)
                return

            try:
                summary = await self._get_player_summary(session, steam_id64)
            except Exception as e:
                print(f"[CS2][ERROR] GetPlayerSummaries failed for {steam_id64}: {type(e).__name__}: {e}", flush=True)
                msg = await self.get_msg(guild_id, "cs2_err_lookup_failed")
                await interaction.followup.send(msg, ephemeral=True)
                return

        if summary is None:
            msg = await self.get_msg(guild_id, "cs2_err_invalid_steamid")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("steam_links").upsert({
                    "guild_id": str(guild_id), "user_id": str(interaction.user.id), "steam_id64": steam_id64,
                }, on_conflict="guild_id,user_id").execute()
            )
        except Exception as e:
            print(f"[CS2][ERROR] Failed to save steam_links (guild={guild_id}, user={interaction.user.id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "cs2_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "cs2_link_success", persona=summary.get("personaname", steam_id64))
        await interaction.followup.send(msg, ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  /cs2_flex_check
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="cs2_flex_check", description="Find today's CS2 inventory flex champion among linked members.")
    async def cs2_flex_check(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = interaction.guild_id

        try:
            links_res = await self._db_call(
                lambda: self.bot.supabase.table("steam_links").select("*").eq("guild_id", str(guild_id)).execute()
            )
            links = links_res.data or []
        except Exception as e:
            print(f"[CS2][ERROR] Failed to fetch steam_links (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred.")
            return

        present_links = [l for l in links if interaction.guild.get_member(int(l["user_id"])) is not None]
        if not present_links:
            msg = await self.get_msg(guild_id, "cs2_err_no_linked_members")
            await interaction.followup.send(msg)
            return

        results = []
        skipped_private = 0
        new_lookups = 0

        async with aiohttp.ClientSession() as session:
            for link in present_links:
                steam_id64 = link["steam_id64"]
                try:
                    cache_res = await self._db_call(
                        lambda sid=steam_id64: self.bot.supabase.table("steam_inventory_cache").select("*").eq("steam_id64", sid).execute()
                    )
                    row = cache_res.data[0] if cache_res.data else None
                except Exception as e:
                    print(f"[CS2][ERROR] Cache lookup failed for {steam_id64}: {type(e).__name__}: {e}", flush=True)
                    row = None

                if self._is_cache_fresh(row):
                    results.append((link, row))
                    continue

                if new_lookups >= CS2_MAX_NEW_LOOKUPS_PER_RUN:
                    if row:  # 만료됐어도 없는 것보단 이전 스냅샷이라도
                        results.append((link, row))
                    continue

                new_lookups += 1
                if new_lookups > 1:
                    await asyncio.sleep(CS2_STEAM_REQUEST_PACING_SECONDS)

                fresh = await self._refresh_inventory_cache(session, steam_id64)
                if fresh["status"] == "ok":
                    results.append((link, fresh))
                elif fresh["status"] == "private":
                    skipped_private += 1
                elif row:
                    results.append((link, row))

        if not results:
            msg = await self.get_msg(guild_id, "cs2_err_no_valid_inventories")
            await interaction.followup.send(msg)
            return

        winner_link, winner_data = max(results, key=lambda r: r[1]["score"])
        winner_member = interaction.guild.get_member(int(winner_link["user_id"]))

        try:
            card_file = await self._render_flex_card(winner_member, winner_data)
        except Exception as e:
            print(f"[CS2][ERROR] Card rendering failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Failed to render the flex card.")
            return

        title = await self.get_msg(guild_id, "cs2_flex_title")
        description = await self.get_msg(guild_id, "cs2_flex_winner_line", member=winner_member.mention,
                                          score=winner_data["score"], count=winner_data["item_count"])
        embed = discord.Embed(title=title, description=description, color=discord.Color.gold())
        embed.set_image(url="attachment://cs2_flex_card.png")
        if skipped_private:
            footer = await self.get_msg(guild_id, "cs2_flex_footer_skipped_private", count=skipped_private)
            embed.set_footer(text=footer)

        await interaction.followup.send(embed=embed, file=card_file)

    # ══════════════════════════════════════════════════════════
    #  카드 렌더링 - welcome.py의 PIL 다운로드+합성 패턴 재사용
    # ══════════════════════════════════════════════════════════
    async def _render_flex_card(self, member: discord.Member, data: dict) -> discord.File:
        base_w, base_h = 920, 320
        card = Image.new("RGBA", (base_w, base_h), color="#1E1F22")
        draw = ImageDraw.Draw(card)

        draw.text((240, 30), "TODAY'S CS2 FLEX CHAMPION", fill="#FFD700", font=self.fonts["title"])
        draw.text((240, 75), member.display_name, fill="#ffffff", font=self.fonts["name"])
        draw.text((240, 115), f"Score {data['score']} · {data['item_count']} items", fill="#b5bac1", font=self.fonts["score"])

        draw.ellipse((42, 30, 198, 186), outline="#FFD700", width=3)

        async with aiohttp.ClientSession() as session:
            avatar_url = member.display_avatar.with_format("png").url
            try:
                async with session.get(avatar_url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        avatar_img = Image.open(io.BytesIO(avatar_data)).convert("RGBA").resize((150, 150))
                        mask = Image.new("L", (150, 150), 0)
                        ImageDraw.Draw(mask).ellipse((0, 0, 150, 150), fill=255)
                        card.paste(avatar_img, (46, 32), mask=mask)
            except Exception as e:
                print(f"[CS2][WARN] Avatar download failed for {member.id}: {type(e).__name__}: {e}", flush=True)
                draw.ellipse((46, 32, 196, 182), fill="#FFD700")

            top_items = data.get("top_items") or []
            slot_w = base_w // max(len(top_items), 1)
            for i, item in enumerate(top_items[:CS2_TOP_ITEMS_COUNT]):
                x = i * slot_w + 20
                y = 190
                icon_url = item.get("icon_url")
                if icon_url:
                    full_url = f"https://community.cloudflare.steamstatic.com/economy/image/{icon_url}"
                    try:
                        async with session.get(full_url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                            if resp.status == 200:
                                icon_data = await resp.read()
                                icon_img = Image.open(io.BytesIO(icon_data)).convert("RGBA").resize((96, 96))
                                card.paste(icon_img, (x, y), mask=icon_img)
                            else:
                                print(f"[CS2][WARN] Item icon fetch returned status {resp.status} for '{item.get('name')}'", flush=True)
                    except Exception as e:
                        print(f"[CS2][WARN] Item icon download failed for '{item.get('name')}': {type(e).__name__}: {e}", flush=True)
                draw.text((x, y + 100), (item.get("name") or "")[:18], fill="#ffffff", font=self.fonts["score"])

        with io.BytesIO() as image_binary:
            card.save(image_binary, "PNG")
            image_binary.seek(0)
            return discord.File(fp=image_binary, filename="cs2_flex_card.png")


async def setup(bot):
    await bot.add_cog(KyvoCS2Flex(bot))
    print("[⚡ CS2_FLEX] Cog extension setup complete.", flush=True)
