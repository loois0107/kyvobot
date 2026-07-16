# locales/__init__.py
import json
import os

# 현재 __init__.py 파일이 있는 절대 디렉토리 경로 확보
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOCALES = {}

# 1. 영어 로케일 파일 로드
try:
    with open(os.path.join(BASE_DIR, "en.json"), "r", encoding="utf-8") as f:
        LOCALES["en"] = json.load(f)
except Exception as e:
    print(f"[LOCALES][ERROR] Failed to load en.json: {e}")
    LOCALES["en"] = {}

# 2. 한국어 로케일 파일 로드
try:
    with open(os.path.join(BASE_DIR, "ko.json"), "r", encoding="utf-8") as f:
        LOCALES["ko"] = json.load(f)
except Exception as e:
    print(f"[LOCALES][ERROR] Failed to load ko.json: {e}")
    LOCALES["ko"] = {}


def get_locale_message(lang: str, key: str) -> str:
    """
    안전하게 다국어 번역 메시지를 가져옵니다. 
    요청 언어 ➔ 영어 폴백 ➔ 그것도 없으면 키 자체 반환
    """
    lang_dict = LOCALES.get(lang, LOCALES["en"])
    return lang_dict.get(key, LOCALES["en"].get(key, key))


def check_locale_integrity():
    """en/ko 로케일 간의 정합성을 검사하여 누락된 키나 양방향 오타를 잡아냅니다."""
    en_keys = set(LOCALES.get("en", {}).keys())
    
    # ⚡ [클로드 확인 1] 영어 키 자체가 비어 있거나 로드가 실패한 경우를 차단
    if not en_keys:
        print("[LOCALES][ERROR] en.json is empty or failed to load. Base language rules are missing!", flush=True)
        return

    for lang, d in LOCALES.items():
        if lang == "en":
            continue
            
        current_keys = set(d.keys())
        
        # ⚡ [클로드 확인 2] 대칭 차집합(^) 연산으로 양방향 불일치(ko에만 있거나 en에만 있는 키)를 모두 감지
        mismatch = en_keys ^ current_keys
        if mismatch:
            print(
                f"[LOCALES][WARN] Mismatched keys discovered between 'en' and '{lang}' ({len(mismatch)} keys): {sorted(mismatch)}",
                flush=True
            )

# 봇 구동 시 자동 정합성 체크
check_locale_integrity()