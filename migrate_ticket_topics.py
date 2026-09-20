"""1회성 마이그레이션: cogs/ticket_ai.py의 topic 마커 게이트(kyvo_ticket:*) 도입 이전에 만들어진
티켓 채널들은 topic이 비어있어 새 게이트를 통과하지 못한다 - 이 스크립트가 그런 채널들을 찾아
topic에 "kyvo_ticket:migrated:{timestamp}" 마커를 채워 넣는다(원래 만든 유저 ID는 알 방법이
없어 "migrated"로 표시해 신규 생성분과 구분한다).

기본값은 dry-run(아무것도 수정 안 함, 대상 개수만 집계) - 실제로 수정하려면 --execute를 명시해야
한다. 로그인/길드 순회 로직과 "채널 하나가 마이그레이션 대상인지" 판단 로직을 분리해뒀다
(classify_channel은 순수 함수라 discord.py 객체 없이도 단위 테스트 가능).

사용법:
    py migrate_ticket_topics.py                 # dry-run (기본, 안전)
    py migrate_ticket_topics.py --execute        # 실제 반영
    py migrate_ticket_topics.py --execute --delay 2.0   # 채널당 edit 간격(초) 조절, 기본 1.0초
"""
import argparse
import asyncio
import os
import time

import discord

MARKER_PREFIX = "kyvo_ticket:"
NOT_TICKET = "not_ticket"
ALREADY_MARKED = "already_marked"
TARGET = "target"


def classify_channel(name: str, topic: str | None) -> str:
    """채널 하나를 세 가지로 분류하는 순수 함수(테스트 가능, discord.py 객체 불필요).
    - not_ticket: 이름이 ticket-으로 안 시작하거나, 🚨-로 시작하는(스태프 에스컬레이션된) 채널
    - already_marked: 이미 topic에 kyvo_ticket: 마커가 있음(오늘 이후 새로 만들어졌거나 이미
      마이그레이션됨) - 재실행해도 중복 처리 안 되게 하는 핵심(멱등성)
    - target: 마이그레이션이 필요한 채널
    """
    if not name.startswith("ticket-") or name.startswith("🚨-"):
        return NOT_TICKET
    if (topic or "").startswith(MARKER_PREFIX):
        return ALREADY_MARKED
    return TARGET


async def run_migration(guilds, execute: bool, delay_sec: float = 1.0) -> dict:
    """guilds(discord.Guild 순회 가능 객체, 실전에서는 client.guilds)를 돌면서 분류·처리한다.
    execute=False면(dry-run) channel.edit을 절대 호출하지 않는다 - 이 함수 하나가 dry-run/실행
    양쪽 경로를 다 담당하되, 실제 쓰기 여부는 이 파라미터 하나로만 갈린다(로직 두 벌 유지 안 함,
    dry-run이 실제 순회/분류 로직과 어긋날 위험 자체를 없앤다).

    채널 하나 실패해도(예: 권한 부족, 순간적 API 오류) 그 채널만 실패로 기록하고 나머지는 계속
    진행한다 - 부분 실패가 이미 처리된 채널들에 영향을 주지 않는다(각 edit은 독립적인 API 호출).
    """
    report = {
        "guild_count": 0, "channel_scanned": 0,
        "not_ticket": 0, "already_marked": 0, "target": 0,
        "migrated": 0, "failed": [],  # [(guild_id, guild_name, channel_id, channel_name, error_str)]
    }

    for guild in guilds:
        report["guild_count"] += 1
        for channel in guild.text_channels:
            report["channel_scanned"] += 1
            cls = classify_channel(channel.name, channel.topic)

            if cls == NOT_TICKET:
                report["not_ticket"] += 1
                continue
            if cls == ALREADY_MARKED:
                report["already_marked"] += 1
                continue

            report["target"] += 1
            action = "EXECUTE" if execute else "DRY-RUN"
            print(f"  [{action}] {guild.name}({guild.id}) / #{channel.name}({channel.id}) - topic={channel.topic!r}", flush=True)

            if not execute:
                continue

            try:
                new_topic = f"{MARKER_PREFIX}migrated:{int(time.time())}"
                await channel.edit(topic=new_topic, reason="Kyvo ticket topic marker backfill migration")
                report["migrated"] += 1
                print(f"    -> 마이그레이션 완료 (새 topic={new_topic!r})", flush=True)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print(f"    !! 실패, 계속 진행: {err}", flush=True)
                report["failed"].append((guild.id, guild.name, channel.id, channel.name, err))

            # 🛡️ [Rate limit 완화] discord.py 자체도 429를 자동으로 기다렸다 재시도하지만, 대량의
            # edit을 연속으로 쏘면 그 백오프에 계속 걸려 오히려 느려지고 API에도 부담을 준다 -
            # 채널마다 명시적으로 쉬어가며 애초에 429를 덜 유발하게 한다. 서로 다른 채널이라
            # per-channel 레이트리밋 버킷과는 무관하고, 이건 순수히 전역/라우트 단위 배려용.
            await asyncio.sleep(delay_sec)

    return report


def print_report(report: dict, execute: bool) -> None:
    mode = "실제 실행" if execute else "DRY-RUN(미리보기)"
    print(f"\n=== {mode} 결과 요약 ===")
    print(f"순회한 서버 수: {report['guild_count']}")
    print(f"스캔한 텍스트 채널 수: {report['channel_scanned']}")
    print(f"무관한 채널(ticket- 아님/이미 에스컬레이션됨): {report['not_ticket']}")
    print(f"이미 마커 있음(스킵): {report['already_marked']}")
    print(f"마이그레이션 대상: {report['target']}")
    if execute:
        print(f"실제로 마이그레이션 완료: {report['migrated']}")
        print(f"실패: {len(report['failed'])}")
        for guild_id, guild_name, channel_id, channel_name, err in report["failed"]:
            print(f"  - {guild_name}({guild_id}) / #{channel_name}({channel_id}): {err}")
    else:
        print("(dry-run이라 실제 수정은 없었습니다 - --execute로 재실행하면 위 대상 개수만큼 반영됩니다)")


async def main_async(execute: bool, delay_sec: float) -> None:
    token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN(또는 BOT_TOKEN/TOKEN) 환경변수가 없습니다.")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            print(f"[로그인 완료] {client.user} - 접근 가능한 서버 {len(client.guilds)}개")
            report = await run_migration(client.guilds, execute=execute, delay_sec=delay_sec)
            print_report(report, execute)
        finally:
            await client.close()

    await client.start(token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="실제로 topic을 수정한다(기본은 dry-run).")
    parser.add_argument("--delay", type=float, default=1.0, help="채널 edit 사이 대기 시간(초), 기본 1.0")
    args = parser.parse_args()

    if args.execute:
        print("⚠️  --execute 모드입니다. 실제로 채널 topic을 수정합니다. 5초 후 시작합니다 (Ctrl+C로 취소)...")
        time.sleep(5)

    asyncio.run(main_async(execute=args.execute, delay_sec=args.delay))
