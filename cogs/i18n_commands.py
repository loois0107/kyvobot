"""디스코드 슬래시 명령어 이름/설명의 한국어 로컬라이제이션.

cogs/base.py의 get_msg()(guild_settings.language 기반 메시지 본문 번역)와는 완전히 다른 축이다 -
여기는 Discord 명령어 선택 UI(자동완성 목록)에 뜨는 이름/설명을 각 유저의 Discord 클라이언트
언어 설정에 따라 바꾸는 것으로, 서버 단위가 아니라 유저 개인 단위이고 메시지 본문에는 전혀
영향을 주지 않는다.

실제 등록된 명령어 이름(코드에서 name="...")은 전부 영어로 그대로 유지한다 - 여기 있는 "name"은
한국어 클라이언트 유저에게만 보이는 표시 이름(name_localizations)이다.

모든 @app_commands.command/@app_commands.describe가 이미 auto_locale_strings=True(기본값)로
정의돼 있어서 - discord.py가 자동으로 모든 이름/설명/파라미터명을 locale_str로 감싸므로, 기존
데코레이터는 하나도 건드릴 필요 없이 이 조회 테이블 + Translator 등록만으로 끝난다.
"""
from __future__ import annotations

import discord
from discord import app_commands

# qualified_name(예: "eco give", "ticket-admin add-knowledge") -> {"name": ..., "description": ...}
# 그룹 자체도 같은 테이블을 공유한다(그룹의 qualified_name은 그냥 그룹 이름 자체).
COMMAND_KO: dict[str, dict[str, str]] = {
    # ── 그룹 (5) ──
    "shop": {"name": "상점", "description": "서버 커스텀 상점 명령어 모음"},
    "eco": {"name": "이코노미", "description": "관리자용 재화 지급/차감 도구"},
    "cc_add": {"name": "명령어추가", "description": "서버 전용 커스텀 명령어를 생성하거나 수정합니다."},
    "giveaway": {"name": "추첨", "description": "포인트 기반 추첨 이벤트를 생성하고 관리합니다."},
    "ticket-admin": {"name": "티켓관리", "description": "AI 지원 티켓 시스템의 설정과 지식 베이스를 관리합니다."},

    # ── cs2_flex.py ──
    "steam_link": {"name": "스팀연동", "description": "CS2 인벤토리 기능을 위해 스팀 계정을 연동합니다(자진 신고 방식)."},
    "cs2_flex_check": {"name": "cs2플렉스체크", "description": "오늘의 CS2 인벤토리 플렉스 챔피언을 연동된 멤버 중에서 찾습니다."},

    # ── anonymous_reports.py ──
    "anonymous_report": {"name": "익명제보", "description": "서버 관리자에게 익명으로 제보를 보냅니다."},
    "anonymous_unban": {"name": "익명제보차단해제", "description": "유저의 익명 제보 차단을 해제합니다."},

    # ── economy.py ──
    "balance": {"name": "잔액", "description": "현재 지갑 잔액을 확인합니다."},
    "daily": {"name": "출석보상", "description": "일일 출석 보상을 받습니다."},
    "bet": {"name": "베팅", "description": "재화를 걸고 도박을 합니다."},
    "blackjack": {"name": "블랙잭", "description": "하우스를 상대로 블랙잭 한 판을 합니다."},
    "mines": {"name": "마인스", "description": "마인스 게임을 플레이합니다 - 안전한 칸을 열고, 지뢰를 밟기 전에 수익을 회수하세요."},
    "roulette": {"name": "룰렛", "description": "하우스를 상대로 러시안 룰렛을 합니다 - 방아쇠를 당기거나 도망치세요."},
    "shop view": {"name": "보기", "description": "서버 상점에서 구매 가능한 아이템을 둘러봅니다."},
    "shop add": {"name": "추가", "description": "서버 상점에 새 아이템을 추가합니다."},
    "shop buy": {"name": "구매", "description": "서버 상점에서 아이템을 구매합니다."},
    "inventory": {"name": "인벤토리", "description": "내 개인 보관함에 있는 모든 아이템을 확인합니다."},
    "eco give": {"name": "지급", "description": "유저에게 포인트를 지급합니다."},
    "eco take": {"name": "차감", "description": "유저의 포인트를 차감합니다."},

    # ── custom_commands.py ──
    "cc_add text": {"name": "텍스트", "description": "텍스트로 응답하는 커스텀 명령어를 만듭니다. /dashboard에서 더 쉽게 설정할 수 있습니다."},
    "cc_add role_add": {"name": "역할추가", "description": "실행한 사람에게 역할을 부여하는 셀프서비스 커스텀 명령어를 만듭니다. /dashboard에서 더 쉽게 설정할 수 있습니다."},
    "cc_add role_remove": {"name": "역할제거", "description": "실행한 사람의 역할을 제거하는 셀프서비스 커스텀 명령어를 만듭니다. /dashboard에서 더 쉽게 설정할 수 있습니다."},
    "cc_delete": {"name": "명령어삭제", "description": "서버 설정에서 커스텀 명령어를 영구적으로 삭제합니다. /dashboard에서 더 쉽게 설정할 수 있습니다."},
    "cc_list": {"name": "명령어목록", "description": "이 서버의 모든 커스텀 명령어를 나열합니다."},

    # ── scrim.py ──
    "scrim_start": {"name": "내전모집", "description": "밸런스 잡힌 5대5 내전 모집을 시작합니다."},
    "scrim_end": {"name": "내전종료", "description": "진행 중인 내전을 종료하고 전원을 원래 음성 채널로 복귀시킵니다."},

    # ── leveling.py ──
    "level": {"name": "레벨", "description": "나만의 커스텀 서버 랭크 카드를 생성합니다."},

    # ── twitch.py ──
    "twitch_channel_set": {"name": "트위치채널등록", "description": "트위치 채널이 방송을 시작하면 알림을 받습니다(역할 부여도 선택 가능)."},
    "twitch_channel_remove": {"name": "트위치채널삭제", "description": "이 서버에서 트위치 채널 알림을 중단합니다."},

    # ── reaction_roles.py ──
    "reaction_role_add": {"name": "반응역할추가", "description": "메시지의 이모지 반응에 역할을 연결합니다(토글: 반응하면 획득, 반응 취소하면 제거). /dashboard에서 더 쉽게 설정할 수 있습니다."},

    # ── gg_rsvp.py ──
    "gg": {"name": "같이해", "description": "'같이 할 사람?' RSVP 카드를 즉시 게시합니다."},

    # ── tier_verify.py ──
    "riot_region_set": {"name": "라이엇지역설정", "description": "계정 인증을 위해 이 서버의 리그 오브 레전드 지역을 설정합니다."},
    "tier_verify": {"name": "티어인증", "description": "리그 오브 레전드 계정을 인증하고 티어 기반 역할을 받습니다(먼저 /riot_region_set 필요)."},

    # ── giveaway.py ──
    "giveaway points": {"name": "포인트", "description": "당첨자에게 포인트를 지급하는 추첨을 생성합니다."},
    "giveaway role": {"name": "역할", "description": "당첨자에게 역할을 부여하는 추첨을 생성합니다."},
    "giveaway end": {"name": "종료", "description": "진행 중인 추첨을 즉시 종료하고 지금 당첨자를 추첨합니다."},

    # ── party.py ──
    "party_recruit": {"name": "파티모집", "description": "파티 모집 게시글을 엽니다."},
    "party_close": {"name": "파티마감", "description": "모집을 취소하거나, 파티 채널을 조기 종료합니다."},
    "tier_role_set": {"name": "티어역할설정", "description": "파티 모집 자진 신고용으로 티어와 역할을 매핑합니다. /dashboard에서 더 쉽게 설정할 수 있습니다."},
    "tier_set": {"name": "티어설정", "description": "본인의 랭크 티어를 자진 신고합니다."},

    # ── ticket_ai.py ──
    "ticket-admin add-knowledge": {"name": "지식추가", "description": "티켓 지원용으로 AI에게 새로운 정보를 학습시킵니다."},
    "ticket-setup": {"name": "티켓설정", "description": "AI 기반 지원 티켓 패널을 설치합니다."},

    # ── onboarding.py ──
    "dashboard": {"name": "대시보드", "description": "이 서버의 관리자 대시보드 링크를 받습니다."},
}

# (qualified_name, 파라미터의 파이썬 식별자) -> {"name": ..., "description": ...}
# 기존 코드에 @app_commands.describe(...)가 붙어있는 파라미터만 대상 - 설명이 아예 없는
# 파라미터(예: eco give의 target/amount)는 번역할 원문 자체가 없어서 이번 범위에서 제외한다.
PARAMETER_KO: dict[tuple[str, str], dict[str, str]] = {
    ("steam_link", "steamid"): {"name": "스팀아이디", "description": "SteamID64 또는 프로필 URL (steamcommunity.com/id/... 또는 /profiles/...)"},
    ("bet", "amount"): {"name": "금액", "description": "베팅할 재화의 양"},
    ("blackjack", "bet"): {"name": "베팅액", "description": "베팅할 재화의 양"},
    ("mines", "bet"): {"name": "베팅액", "description": "베팅할 재화의 양"},
    ("mines", "danger_level"): {"name": "난이도", "description": "5x5 격자에 숨겨진 지뢰 개수"},
    ("roulette", "bet"): {"name": "베팅액", "description": "베팅할 재화의 양"},
    ("shop buy", "item_name"): {"name": "아이템이름", "description": "구매할 아이템의 정확한 이름"},
    ("gg", "game"): {"name": "게임", "description": "게임 선택 (이 서버에 저장된 프리셋 중에서)"},
    ("gg", "needed_count"): {"name": "필요인원", "description": "본인 포함 총 필요 인원 (2-10명)"},
    ("giveaway points", "prize"): {"name": "경품", "description": "경품 표시 이름"},
    ("giveaway points", "entry_cost"): {"name": "참가비용", "description": "응모에 필요한 포인트"},
    ("giveaway points", "winner_count"): {"name": "당첨인원", "description": "당첨자 수"},
    ("giveaway points", "duration_minutes"): {"name": "진행시간", "description": "추첨이 마감되기까지의 분 (5-10080)"},
    ("giveaway points", "prize_amount"): {"name": "당첨포인트", "description": "당첨자 1인당 지급될 포인트"},
    ("giveaway points", "channel"): {"name": "채널", "description": "추첨을 게시할 채널 (기본값: 현재 채널)"},
    ("giveaway role", "prize"): {"name": "경품", "description": "경품 표시 이름"},
    ("giveaway role", "entry_cost"): {"name": "참가비용", "description": "응모에 필요한 포인트"},
    ("giveaway role", "winner_count"): {"name": "당첨인원", "description": "당첨자 수"},
    ("giveaway role", "duration_minutes"): {"name": "진행시간", "description": "추첨이 마감되기까지의 분 (5-10080)"},
    ("giveaway role", "prize_role"): {"name": "당첨역할", "description": "당첨자에게 부여될 역할"},
    ("giveaway role", "channel"): {"name": "채널", "description": "추첨을 게시할 채널 (기본값: 현재 채널)"},
    ("giveaway end", "giveaway"): {"name": "추첨", "description": "조기 종료할 진행 중인 추첨"},
    ("party_recruit", "game"): {"name": "게임", "description": "선택 - 모집 카드 디자인에 쓸 저장된 게임 프리셋 선택"},
    ("party_recruit", "min_tier"): {"name": "최소티어", "description": "선택 - 카드에 참고용으로만 표시되며 강제되지 않음"},
    ("tier_role_set", "tier"): {"name": "티어", "description": "매핑할 티어"},
    ("tier_role_set", "role"): {"name": "역할", "description": "이 티어를 자진 신고하면 부여될 역할"},
    ("tier_set", "tier"): {"name": "티어", "description": "현재 랭크 티어"},
    ("reaction_role_add", "message_id"): {"name": "메시지아이디", "description": "반응을 걸 메시지의 ID (반드시 이 채널에 있어야 함)"},
    ("reaction_role_add", "emoji"): {"name": "이모지", "description": "멤버들이 반응할 이모지"},
    ("reaction_role_add", "role"): {"name": "역할", "description": "반응 시 부여되고 반응 취소 시 제거되는 역할"},
    ("ticket-admin add-knowledge", "content"): {"name": "내용", "description": "AI에게 학습시킬 실제 가이드라인, 서버 규칙, FAQ 텍스트"},
    ("riot_region_set", "region"): {"name": "지역", "description": "이 서버 플레이어들이 플레이하는 LoL 플랫폼 지역"},
    ("twitch_channel_set", "streamer"): {"name": "스트리머", "description": "트위치 로그인 이름 (twitch.tv/<이 부분>)"},
    ("twitch_channel_set", "channel"): {"name": "채널", "description": "라이브 알림을 게시할 채널"},
    ("twitch_channel_set", "member"): {"name": "멤버", "description": "라이브 중 역할을 부여할 디스코드 멤버 (role을 설정했다면 필수)"},
    ("twitch_channel_set", "role"): {"name": "역할", "description": "라이브 중 부여할 역할 (선택)"},
    ("twitch_channel_remove", "streamer"): {"name": "스트리머", "description": "삭제할 트위치 로그인 이름"},

    # ── economy.py (shop add / eco give / eco take) ──
    ("shop add", "name"): {"name": "이름", "description": "상점에 표시될 아이템 이름"},
    ("shop add", "price"): {"name": "가격", "description": "이 서버 화폐 기준 아이템 가격"},
    ("shop add", "description"): {"name": "설명", "description": "아이템을 설명하는 짧은 한 줄"},
    ("eco give", "target"): {"name": "대상", "description": "포인트를 지급할 멤버"},
    ("eco give", "amount"): {"name": "수량", "description": "지급할 포인트 수"},
    ("eco take", "target"): {"name": "대상", "description": "포인트를 차감할 멤버"},
    ("eco take", "amount"): {"name": "수량", "description": "차감할 포인트 수"},

    # ── custom_commands.py (cc_add text / role_add / role_remove / cc_delete) ──
    ("cc_add text", "name"): {"name": "이름", "description": "슬래시(/) 없이 입력하는 명령어 이름"},
    ("cc_add text", "response"): {"name": "응답내용", "description": "이 명령어가 실행됐을 때 답할 텍스트"},
    ("cc_add role_add", "name"): {"name": "이름", "description": "슬래시(/) 없이 입력하는 명령어 이름"},
    ("cc_add role_add", "role"): {"name": "역할", "description": "실행 시 부여할 역할"},
    ("cc_add role_remove", "name"): {"name": "이름", "description": "슬래시(/) 없이 입력하는 명령어 이름"},
    ("cc_add role_remove", "role"): {"name": "역할", "description": "실행 시 제거할 역할"},
    ("cc_delete", "name"): {"name": "이름", "description": "삭제할 커스텀 명령어의 이름"},

    # ── anonymous_reports.py (anonymous_unban) ──
    ("anonymous_unban", "user"): {"name": "유저", "description": "익명 제보 차단을 해제할 멤버"},
}


class KoreanCommandTranslator(app_commands.Translator):
    """discord.Locale.korean 유저에게만 이름/설명 로컬라이제이션을 제공한다.

    다른 모든 로케일엔 None을 반환해서(기본 동작) 그 유저들은 원래의 영어 name/description을
    그대로 본다 - tree.sync()가 모든 Locale에 대해 이 메서드를 호출하므로, 여기서 처리 안 하는
    로케일은 자동으로 폴백된다.
    """

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        if locale != discord.Locale.korean:
            return None

        location = context.location
        Loc = app_commands.TranslationContextLocation

        if location in (Loc.command_name, Loc.group_name):
            entry = COMMAND_KO.get(context.data.qualified_name)
            return entry["name"] if entry else None

        if location in (Loc.command_description, Loc.group_description):
            entry = COMMAND_KO.get(context.data.qualified_name)
            return entry["description"] if entry else None

        if location == Loc.parameter_name:
            key = (context.data.command.qualified_name, context.data.name)
            entry = PARAMETER_KO.get(key)
            return entry["name"] if entry else None

        if location == Loc.parameter_description:
            key = (context.data.command.qualified_name, context.data.name)
            entry = PARAMETER_KO.get(key)
            return entry["description"] if entry else None

        return None
