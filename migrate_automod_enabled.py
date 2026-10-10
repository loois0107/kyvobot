"""1회성 마이그레이션: AUTOMOD_ENABLED_DEFAULT를 True에서 False로 바꾸기 전에, 지금까지
"automod_settings.enabled" 키가 아예 없어서 그 기본값 폴백에 의존하고 있던 모든 길드에
enabled: true를 명시적으로 박아 넣는다 - 이 백필이 끝나야 기본값을 바꿔도 기존에 보호받던
길드들이 조용히 무방비 상태가 되지 않는다(cogs/automod.py 37-41줄 주석 및 관련 조사 참고).

이미 automod_settings.enabled 키가 있는 행(값이 true든 false든)은 절대 건드리지 않는다 -
그 길드는 대시보드 automod 설정 페이지를 한 번이라도 저장해본 적이 있어서 이미 명시적인
값을 갖고 있다는 뜻이다.

settings 컬럼의 다른 키(party_settings, voice_settings 등)는 절대 건드리지 않는다 -
automod_settings 하나만, 그 안에서도 enabled 키 하나만 추가한다.

기본값은 dry-run(아무것도 수정 안 함, 대상 개수만 집계) - 실제로 수정하려면 --execute를
명시해야 한다. "행 하나가 마이그레이션 대상인지" 판단 로직과 "업데이트된 settings를
만드는" 로직을 순수 함수로 분리해뒀다(discord.py/supabase 객체 없이도 단위 테스트 가능).

사용법:
    py migrate_automod_enabled.py                # dry-run (기본, 안전) - 대상 개수만 출력
    py migrate_automod_enabled.py --execute       # 실제 반영

환경변수: SUPABASE_URL, SUPABASE_KEY (main.py와 동일한 변수명 - service role 권한 필요,
guild_settings 전체 행을 읽고 쓰기 위함).
"""
import argparse
import os

from supabase import create_client

TARGET = "target"
ALREADY_HAS = "already_has"


def classify_settings(settings: dict | None) -> str:
    """이 행이 백필 대상인지 판단하는 순수 함수.
    - already_has: settings.automod_settings가 dict이고 그 안에 "enabled" 키가 있음(값 무관) -
      대시보드에서 이미 명시적으로 저장해본 행이라 절대 안 건드림.
    - target: automod_settings가 없거나, dict가 아니거나, dict인데 enabled 키가 없음.
    """
    settings = settings or {}
    automod_settings = settings.get("automod_settings")
    if isinstance(automod_settings, dict) and "enabled" in automod_settings:
        return ALREADY_HAS
    return TARGET


def build_updated_settings(settings: dict | None) -> dict:
    """classify_settings가 target으로 판정한 행에 대해서만 호출한다. automod_settings의
    다른 필드(spam_limit 등, 직접 DB 편집 등으로 이미 일부 들어있을 가능성)는 그대로 보존하고
    enabled: true만 추가한다. settings의 다른 모듈 키도 전부 그대로 보존한다."""
    settings = dict(settings or {})
    automod_settings = dict(settings.get("automod_settings") or {})
    automod_settings["enabled"] = True
    settings["automod_settings"] = automod_settings
    return settings


def fetch_all_guild_settings(supabase) -> list[dict]:
    """guild_settings 테이블 전체를 guild_id/settings만 페이지네이션으로 다 끌어온다."""
    rows = []
    page_size = 1000
    offset = 0
    while True:
        res = (
            supabase.table("guild_settings")
            .select("guild_id, settings")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def run(execute: bool) -> None:
    supabase_url = os.environ["SUPABASE_URL"]
    supabase_key = os.environ["SUPABASE_KEY"]
    supabase = create_client(supabase_url, supabase_key)

    rows = fetch_all_guild_settings(supabase)
    targets = [r for r in rows if classify_settings(r.get("settings")) == TARGET]
    already_has = len(rows) - len(targets)

    print(f"전체 guild_settings 행: {len(rows)}개")
    print(f"이미 enabled 키가 있어서 건드리지 않을 행: {already_has}개")
    print(f"백필 대상(enabled 키 없음) 행: {len(targets)}개")

    if not execute:
        print("\n[DRY-RUN] 실제 수정 없음. 반영하려면 --execute를 붙여서 다시 실행하세요.")
        return

    if not targets:
        print("\n백필 대상이 없어서 아무 것도 하지 않습니다.")
        return

    print(f"\n[EXECUTE] {len(targets)}개 행에 automod_settings.enabled=true를 반영합니다...")
    updated = 0
    failed = []
    for row in targets:
        guild_id = row["guild_id"]
        new_settings = build_updated_settings(row.get("settings"))
        try:
            supabase.table("guild_settings").update({"settings": new_settings}).eq(
                "guild_id", guild_id
            ).execute()
            updated += 1
        except Exception as e:
            failed.append((guild_id, str(e)))

    print(f"완료: {updated}개 성공, {len(failed)}개 실패.")
    for guild_id, err in failed:
        print(f"  실패: guild_id={guild_id}: {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="실제로 DB를 수정한다(기본은 dry-run).")
    args = parser.parse_args()
    run(execute=args.execute)
