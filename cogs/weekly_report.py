import discord
from discord.ext import commands, tasks
from cogs.base import KyvoBaseCog
import asyncio
import itertools
from collections import Counter
from datetime import datetime, timezone, timedelta

WEEKLY_REPORT_CHECK_INTERVAL_SECONDS = 300  # 5분 - 주 1회 이벤트라 초 단위 정밀도가 필요 없다
WEEKLY_REPORT_WINDOW_DAYS = 7
WEEKLY_REPORT_HOUR_KST = 9  # 월요일 09:00

# 🛡️ 이 시스템엔 길드별 타임존 개념이 아예 없다(전체 코드베이스가 UTC 고정) - "월요일 아침"을
# 위해 이번엔 KST를 하드코딩한다(사용자 승인). 나중에 다른 타임존 서버 지원이 필요해지면 그때
# guild_settings에 타임존 필드를 추가하는 방향으로 확장하면 된다.
KST = timezone(timedelta(hours=9))


def get_current_week_boundary_kst(now_utc: datetime) -> datetime:
    """now_utc 기준 '가장 최근에 지난 월요일 09:00 KST' 시각을 UTC로 반환한다. 아직 이번 주
    월요일 09:00 KST가 안 지났으면(월요일 00:00~08:59 KST 구간) 지난주 경계를 반환한다 -
    "이미 지난 경계 하나"만 알면 되므로 항상 가장 최근 것을 돌려준다."""
    now_kst = now_utc.astimezone(KST)
    days_since_monday = now_kst.weekday()  # 월요일=0
    this_monday_date = (now_kst - timedelta(days=days_since_monday)).date()
    this_monday_9am_kst = datetime(
        this_monday_date.year, this_monday_date.month, this_monday_date.day,
        WEEKLY_REPORT_HOUR_KST, 0, 0, tzinfo=KST,
    )
    boundary = this_monday_9am_kst if now_kst >= this_monday_9am_kst else this_monday_9am_kst - timedelta(days=7)
    return boundary.astimezone(timezone.utc)


def should_send_weekly_report(now_utc: datetime, last_sent_at_iso: str | None) -> bool:
    """마지막 발송 시각이 '가장 최근에 지난 월요일 09:00 KST 경계'보다 이전이면(=이번 주 몫을
    아직 안 보냈으면) True. 한 번도 안 보냈으면(None) 당연히 True."""
    boundary = get_current_week_boundary_kst(now_utc)
    if last_sent_at_iso is None:
        return True
    last_sent = datetime.fromisoformat(last_sent_at_iso)
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    return last_sent < boundary


def get_report_window(boundary_utc: datetime) -> tuple[datetime, datetime]:
    """리포트가 다루는 기간: 이번 경계 시각으로부터 지난 7일."""
    return boundary_utc - timedelta(days=WEEKLY_REPORT_WINDOW_DAYS), boundary_utc


def compute_party_king(participant_rows: list[dict]) -> list[tuple[str, int]]:
    """participant_rows: [{"user_id": ...}, ...] (이미 기간/길드로 필터링된 것으로 가정).
    참여 횟수가 가장 많은 유저(들)을 (user_id, count) 튜플로 반환 - 동점이면 전부 반환."""
    counts = Counter(r["user_id"] for r in participant_rows)
    if not counts:
        return []
    top = max(counts.values())
    return sorted((uid, c) for uid, c in counts.items() if c == top)


def compute_popular_game(recruitment_rows: list[dict]) -> list[tuple[str, int]]:
    """recruitment_rows: [{"selected_game": ...}, ...]. selected_game이 비어있는 행은 집계에서
    제외한다(게임을 아예 지정 안 한 모집은 '인기 게임' 후보가 될 수 없음)."""
    counts = Counter(r["selected_game"] for r in recruitment_rows if r.get("selected_game"))
    if not counts:
        return []
    top = max(counts.values())
    return sorted((g, c) for g, c in counts.items() if c == top)


def compute_duo_combo(participants_by_recruitment: dict) -> list[tuple[tuple[str, str], int]]:
    """recruitment_id -> [user_id, ...] 매핑을 받아, 같은 파티에 함께 있었던 모든 2인 조합의
    등장 횟수를 센다. 페어는 (min, max) 정규화라 순서 무관하게 합산된다. 동점이면 전부 반환."""
    counts = Counter()
    for uids in participants_by_recruitment.values():
        unique_uids = sorted(set(uids))
        for a, b in itertools.combinations(unique_uids, 2):
            counts[(a, b)] += 1
    if not counts:
        return []
    top = max(counts.values())
    return sorted((pair, c) for pair, c in counts.items() if c == top)


class KyvoWeeklyReport(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.check_weekly_reports.start()

    async def cog_unload(self):
        self.check_weekly_reports.cancel()

    async def _db_call(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    # ══════════════════════════════════════════════════════════
    #  집계 - party_recruitments/party_participants만 쓴다(정식 파티로 범위 한정, gg/scrim 제외).
    # ══════════════════════════════════════════════════════════
    async def _fetch_recruitments_in_window(self, guild_id: str, since_iso: str, until_iso: str) -> list[dict]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments").select("id, selected_game")
                        .eq("guild_id", guild_id).gte("created_at", since_iso).lt("created_at", until_iso).execute()
            )
            return res.data or []
        except Exception as e:
            print(f"[WEEKLY_REPORT][ERROR] Failed to fetch recruitments (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return []

    async def _fetch_participants_for_recruitments(self, recruitment_ids: list) -> list[dict]:
        if not recruitment_ids:
            return []
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_participants").select("recruitment_id, user_id")
                        .in_("recruitment_id", recruitment_ids).execute()
            )
            return res.data or []
        except Exception as e:
            print(f"[WEEKLY_REPORT][ERROR] Failed to fetch participants: {type(e).__name__}: {e}", flush=True)
            return []

    async def build_report_embed(self, guild_id: str, boundary_utc: datetime) -> discord.Embed:
        since, until = get_report_window(boundary_utc)
        recruitments = await self._fetch_recruitments_in_window(guild_id, since.isoformat(), until.isoformat())
        recruitment_ids = [r["id"] for r in recruitments]
        participants = await self._fetch_participants_for_recruitments(recruitment_ids)

        title = await self.get_msg(int(guild_id), "weekly_report_title")

        if not recruitments and not participants:
            desc = await self.get_msg(int(guild_id), "weekly_report_quiet_desc")
            return discord.Embed(title=title, description=desc, color=discord.Color.greyple())

        participants_by_recruitment: dict = {}
        for p in participants:
            participants_by_recruitment.setdefault(p["recruitment_id"], []).append(p["user_id"])

        king = compute_party_king(participants)
        game = compute_popular_game(recruitments)
        duo = compute_duo_combo(participants_by_recruitment)

        no_data = await self.get_msg(int(guild_id), "weekly_report_no_data")

        king_label = await self.get_msg(int(guild_id), "weekly_report_field_party_king")
        if king:
            king_value = ", ".join(f"<@{uid}> ({c}회)" for uid, c in king)
        else:
            king_value = no_data

        game_label = await self.get_msg(int(guild_id), "weekly_report_field_popular_game")
        if game:
            game_value = ", ".join(f"**{g}** ({c}회)" for g, c in game)
        else:
            game_value = no_data

        duo_label = await self.get_msg(int(guild_id), "weekly_report_field_duo_combo")
        if duo:
            duo_value = ", ".join(f"<@{a}> ↔ <@{b}> ({c}회)" for (a, b), c in duo)
        else:
            duo_value = no_data

        embed = discord.Embed(title=title, color=discord.Color.gold(), timestamp=boundary_utc)
        embed.add_field(name=king_label, value=king_value, inline=False)
        embed.add_field(name=game_label, value=game_value, inline=False)
        embed.add_field(name=duo_label, value=duo_value, inline=False)
        return embed

    # ══════════════════════════════════════════════════════════
    #  스케줄 - 기존 30초 폴링 인프라(giveaway/party/gg_rsvp/scrim)와 같은 정신이지만, 대상이
    #  개별 행이 아니라 "길드마다 이번 주 몫을 이미 보냈는가"라 상태를 guild_settings JSON에
    #  저장한다(last_sent_at). 봇이 월요일 09:00 KST에 꺼져있었어도 재시작 후 다음 틱에서
    #  그대로 캐치업된다(기존 절대시각 비교 패턴과 동일한 콜드스타트 안전성).
    # ══════════════════════════════════════════════════════════
    @tasks.loop(seconds=WEEKLY_REPORT_CHECK_INTERVAL_SECONDS)
    async def check_weekly_reports(self):
        now_utc = datetime.now(timezone.utc)
        boundary = get_current_week_boundary_kst(now_utc)

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("guild_settings").select("guild_id, settings").execute()
            )
            rows = res.data or []
        except Exception as e:
            print(f"[WEEKLY_REPORT][ERROR] Failed to fetch guild_settings: {type(e).__name__}: {e}", flush=True)
            return

        for row in rows:
            settings = row.get("settings") or {}
            report_settings = settings.get("weekly_report_settings") or {}
            if not report_settings.get("enabled"):
                continue
            channel_id = report_settings.get("channel_id")
            if not channel_id:
                continue
            if not should_send_weekly_report(now_utc, report_settings.get("last_sent_at")):
                continue

            await self._send_weekly_report(row["guild_id"], channel_id, settings, boundary, now_utc)

    @check_weekly_reports.before_loop
    async def before_check_weekly_reports(self):
        await self.bot.wait_until_ready()

    async def _send_weekly_report(self, guild_id: str, channel_id: str, current_settings: dict,
                                   boundary: datetime, sent_at: datetime) -> None:
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            print(f"[WEEKLY_REPORT][WARN] Configured channel {channel_id} not in cache (guild={guild_id}), skipping this tick.", flush=True)
            return

        embed = await self.build_report_embed(guild_id, boundary)
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[WEEKLY_REPORT][ERROR] Failed to post weekly report (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return

        # 🛡️ 발송 성공 후에만 last_sent_at을 갱신한다 - 발송이 실패했는데 갱신해버리면 이번 주
        # 몫을 영영 못 보내고 넘어가게 된다.
        updated_settings = {**current_settings, "weekly_report_settings": {
            **(current_settings.get("weekly_report_settings") or {}), "last_sent_at": sent_at.isoformat(),
        }}
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("guild_settings")
                        .update({"settings": updated_settings}).eq("guild_id", guild_id).execute()
            )
        except Exception as e:
            print(f"[WEEKLY_REPORT][ERROR] Failed to record last_sent_at (guild={guild_id}): {type(e).__name__}: {e}", flush=True)


async def setup(bot):
    cog = KyvoWeeklyReport(bot)
    await bot.add_cog(cog)
    print("[⚡ WEEKLY_REPORT] Cog extension setup complete.", flush=True)
