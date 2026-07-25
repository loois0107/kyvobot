import discord
from discord.ext import commands
from cogs.base import KyvoBaseCog
import asyncio
import time

GRACE_PERIOD_SECONDS = 10.0
PENDING_JOIN_CLAIM_TTL = 5  # 원자적 클레임 TTL - 중복 인스턴스/빠른 재입장으로 인한 이중 생성 방지


class KyvoVoice(KyvoBaseCog):
    """Join to Create(임시 음성 채널) 기능. 저장 위치는 settings.voice_settings.trigger_channel_id로
    고정 - 봇(읽기)과 대시보드(쓰기)가 반드시 이 하나의 경로만 거친다."""

    def __init__(self, bot):
        super().__init__(bot)
        # channel_id -> guild_id. Redis가 진실의 원천이고, 이건 매 voice_state_update마다
        # Redis 왕복을 피하기 위한 인메모리 미러(write-through로 항상 같이 갱신).
        self.tracked_channels: dict[int, int] = {}
        self.pending_claim_cache: dict[int, float] = {}
        self._recovered = False

    async def _get_voice_settings(self, guild_id) -> dict:
        """voice_settings를 Redis Cache-Aside(KyvoBaseCog.get_guild_settings)로 읽어온다.
        guild_settings 행은 {..., settings: {voice_settings: {...}}} 구조라 한 단계 더 파고들어야 한다."""
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        return nested_settings.get("voice_settings") or {}

    def _redis_key(self, guild_id: int) -> str:
        return f"kyvo:temp_voice:{guild_id}"

    # ══════════════════════════════════════════════════════════
    #  공용 임시 음성 채널 수명주기 엔진 - Join to Create 전용이 아니다. 다른 cog(예: party.py의
    #  파티 음성 채널)가 자기 소유 redis_key/tracked_dict를 넘겨서 이 메커니즘(추적/유예삭제/
    #  콜드스타트 복구)을 그대로 재사용할 수 있도록 일반화했다 - 소유권(어떤 채널을 왜 만들었는지)은
    #  각 cog가 자기 네임스페이스로 분리해서 갖고, "비면 유예 후 삭제"라는 메커니즘 코드만 공유한다.
    #  언더스코어 없는 이름은 cross-cog 호출을 위한 의도적 공개 API다(custom_commands.py의
    #  enqueue_log와 동일한 컨벤션).
    # ══════════════════════════════════════════════════════════
    async def track_channel(self, redis_key: str, guild_id: int, channel_id: int, tracked_dict: dict) -> None:
        tracked_dict[channel_id] = guild_id
        try:
            await self.redis.sadd(redis_key, str(channel_id))
        except Exception as e:
            print(f"[VOICE][WARN] Redis SADD failed for channel {channel_id} "
                  f"(key={redis_key}): {type(e).__name__}: {e}", flush=True)

    async def untrack_channel(self, redis_key: str, guild_id: int, channel_id: int, tracked_dict: dict) -> None:
        tracked_dict.pop(channel_id, None)
        try:
            await self.redis.srem(redis_key, str(channel_id))
        except Exception as e:
            print(f"[VOICE][WARN] Redis SREM failed for channel {channel_id} "
                  f"(key={redis_key}): {type(e).__name__}: {e}", flush=True)

    async def _track_channel(self, guild_id: int, channel_id: int) -> None:
        await self.track_channel(self._redis_key(guild_id), guild_id, channel_id, self.tracked_channels)

    async def _untrack_channel(self, guild_id: int, channel_id: int) -> None:
        await self.untrack_channel(self._redis_key(guild_id), guild_id, channel_id, self.tracked_channels)

    # ══════════════════════════════════════════════════════════
    #  원자적 클레임 (automod의 try_claim_punishment와 동일한 패턴 재사용)
    # ══════════════════════════════════════════════════════════
    async def _try_claim_pending_join(self, user_id: int) -> bool:
        """True면 이 호출이 채널 생성을 원자적으로 선점한 것(=진행), False면 이미 누가 선점함(=스킵).
        배포 중 인스턴스가 잠깐 겹치거나 유저가 트리거 채널을 빠르게 재입장할 때 채널이 중복
        생성되는 걸 막는다."""
        key = f"kyvo:temp_voice:pending:{user_id}"
        try:
            acquired = await self.redis.set(key, "1", ex=PENDING_JOIN_CLAIM_TTL, nx=True)
            return bool(acquired)
        except Exception as e:
            print(f"[VOICE][WARN] Redis pending-join claim failed, falling back to local: "
                  f"{type(e).__name__}: {e}", flush=True)
            return self._try_claim_pending_join_local(user_id)

    def _try_claim_pending_join_local(self, user_id: int) -> bool:
        now = time.time()
        expiry = self.pending_claim_cache.get(user_id)
        if expiry is not None and now < expiry:
            return False
        self.pending_claim_cache[user_id] = now + PENDING_JOIN_CLAIM_TTL
        return True

    async def _release_pending_join_claim(self, user_id: int) -> None:
        """채널 생성 자체가 실패했을 때 선점을 되돌린다 - 실제로 생성되지 않았으므로
        다음 시도가 곧바로 다시 시도할 수 있어야 한다."""
        key = f"kyvo:temp_voice:pending:{user_id}"
        try:
            await self.redis.delete(key)
        except Exception as e:
            print(f"[VOICE][WARN] Redis pending-join claim release failed: {type(e).__name__}: {e}", flush=True)
        self.pending_claim_cache.pop(user_id, None)

    # ══════════════════════════════════════════════════════════
    #  콜드 스타트 복구
    # ══════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready는 재연결마다 다시 불릴 수 있어, 프로세스 생애주기당 한 번만 복구를 수행한다.
        if self._recovered:
            return
        self._recovered = True

        for guild in self.bot.guilds:
            try:
                channel_ids = await self.redis.smembers(self._redis_key(guild.id))
            except Exception as e:
                print(f"[VOICE][WARN] Redis SMEMBERS failed during cold-start recovery "
                      f"(guild={guild.id}): {type(e).__name__}: {e}", flush=True)
                continue

            for cid_str in channel_ids:
                try:
                    channel_id = int(cid_str)
                except (TypeError, ValueError):
                    continue

                channel = guild.get_channel(channel_id)
                if channel is None:
                    # 봇이 꺼져있는 동안 이미 삭제된 채널 - 유령 Redis 기록만 정리
                    await self._untrack_channel(guild.id, channel_id)
                    print(f"[VOICE][RECOVERY] Stale tracking entry for missing channel {channel_id} "
                          f"cleaned up (guild={guild.id})", flush=True)
                    continue

                self.tracked_channels[channel_id] = guild.id
                print(f"[VOICE][RECOVERY] Reattached tracking for temp channel {channel_id} "
                      f"(guild={guild.id}, members={len(channel.members)})", flush=True)

                if len(channel.members) == 0:
                    asyncio.create_task(self._schedule_deletion(channel))

    # ══════════════════════════════════════════════════════════
    #  실행 엔진
    # ══════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # 1) 트리거 채널에 새로 입장했는지 확인
        if after.channel is not None and (before.channel is None or before.channel.id != after.channel.id):
            await self._maybe_handle_trigger_join(member, after.channel)

        # 2) 추적 중인 임시 채널에서 인원이 빠져 0명이 됐는지 확인
        if before.channel is not None and before.channel.id in self.tracked_channels:
            if len(before.channel.members) == 0:
                asyncio.create_task(self._schedule_deletion(before.channel))

    async def _maybe_handle_trigger_join(self, member: discord.Member, channel: discord.VoiceChannel) -> None:
        guild = member.guild
        voice_settings = await self._get_voice_settings(guild.id)
        trigger_channel_id = voice_settings.get("trigger_channel_id")

        if not trigger_channel_id or str(channel.id) != str(trigger_channel_id):
            return

        claimed = await self._try_claim_pending_join(member.id)
        if not claimed:
            return

        try:
            name = await self.get_msg(guild.id, "voice_temp_channel_name", user=member.display_name)
            new_channel = await guild.create_voice_channel(
                name=name, category=channel.category, reason=f"[KYVO JOIN-TO-CREATE] by {member}"
            )
            await member.move_to(new_channel, reason="[KYVO JOIN-TO-CREATE]")
        except Exception as e:
            print(f"[VOICE][ERROR] Failed to create/move temp channel for user {member.id} "
                  f"(guild={guild.id}): {type(e).__name__}: {e}", flush=True)
            await self._release_pending_join_claim(member.id)
            return

        await self._track_channel(guild.id, new_channel.id)

    async def schedule_deletion(self, channel: discord.VoiceChannel, redis_key: str, tracked_dict: dict,
                                 reason: str = "[KYVO JOIN-TO-CREATE] empty temp channel cleanup") -> None:
        """10초 유예 후 재확인. 그 사이 여러 유예 태스크가 동시에 걸릴 수 있으므로(사람이 빠르게
        들어왔다 나가는 경우 등), 이미 다른 태스크가 먼저 지운 채널을 다시 지우려는 시도는
        discord.NotFound로 안전하게 흡수한다 - 이건 정상적인 경쟁이지 에러가 아니다.
        redis_key/tracked_dict를 파라미터로 받아 다른 cog의 네임스페이스에도 그대로 쓸 수 있다."""
        await asyncio.sleep(GRACE_PERIOD_SECONDS)

        guild = channel.guild
        current = guild.get_channel(channel.id)
        if current is None:
            # 이미 다른 유예 태스크나 수동 조작으로 사라짐 - 추적 정리만 하고 종료
            await self.untrack_channel(redis_key, guild.id, channel.id, tracked_dict)
            return

        if len(current.members) > 0:
            return  # 유예 기간 중 누가 다시 들어옴(또는 애초에 아무도 안 들어왔다가 지금 들어옴) - 아무것도 안 함

        try:
            await current.delete(reason=reason)
        except discord.NotFound:
            # 동시에 걸린 다른 유예 태스크가 먼저 지웠음 - 정상적인 경쟁, 에러 아님
            print(f"[VOICE][INFO] Channel {channel.id} already deleted by a concurrent "
                  f"cleanup task (guild={guild.id})", flush=True)
        except discord.Forbidden:
            print(f"[VOICE][WARN] Missing permission to delete channel {channel.id} "
                  f"(guild={guild.id})", flush=True)
            return  # 권한 문제면 추적을 유지해야 다음 기회에 재시도될 수 있다
        except discord.HTTPException as e:
            print(f"[VOICE][ERROR] Failed to delete channel {channel.id} "
                  f"(guild={guild.id}): {type(e).__name__}: {e}", flush=True)
            return

        await self.untrack_channel(redis_key, guild.id, channel.id, tracked_dict)

    async def _schedule_deletion(self, channel: discord.VoiceChannel) -> None:
        await self.schedule_deletion(channel, self._redis_key(channel.guild.id), self.tracked_channels)


async def setup(bot):
    await bot.add_cog(KyvoVoice(bot))
    print("[⚡ VOICE] Cog extension setup complete.", flush=True)
