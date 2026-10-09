"""
서버(cogs/dor.py의 handle_upload_webhook)로 클립을 업로드.

통신 규격은 서버 코드를 직접 읽어서 맞춘 것(cogs/dor.py:269-396):
  POST {server_url}/internal/dor/upload
  Authorization: Bearer <token>
  multipart/form-data, 필드 3개:
    - video          : 영상 파일 바이너리 (filename 포함)
    - clip_events    : {"clipData": {...}, "eventData": [...]} 를 JSON 문자열로
    - basic_game_data: 세션 공용 basicGameData 객체를 JSON 문자열로
  응답: 200 = 성공({"status":"ok","candidate_id":...}), 401 = 토큰 문제,
        422 = 길드에 platform_region 미설정, 400 = 요청 형식 오류, 500 = 서버쪽 저장 실패.
  401/422/400은 재시도해도 결과가 달라지지 않는 요청 자체의 문제이므로 재시도하지 않고
  바로 실패 처리한다. 네트워크 오류/5xx/타임아웃만 재시도 대상이다.
"""
import os
import time

import requests

RETRYABLE_STATUS = {500, 502, 503, 504}
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SEC = (2, 5, 10)
UPLOAD_TIMEOUT_SEC = 120

# 영상 파일이 아직 DOR 쪽에서 쓰여지는 중일 수 있으므로, 업로드 전에 크기가
# 안정화됐는지 짧게 확인한다.
STABILITY_CHECK_INTERVAL_SEC = 1.0
STABILITY_MAX_WAIT_SEC = 30


class UploadError(Exception):
    """재시도를 다 써도 실패했거나, 재시도해도 소용없는 요청 자체의 문제."""


def wait_until_stable(path: str, logger) -> bool:
    deadline = time.monotonic() + STABILITY_MAX_WAIT_SEC
    last_size = -1
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(STABILITY_CHECK_INTERVAL_SEC)
            continue
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(STABILITY_CHECK_INTERVAL_SEC)
    logger.warning(f"파일 크기가 안정화되지 않았지만 계속 진행합니다: {path}")
    return os.path.isfile(path)


def upload_clip(server_url: str, token: str, candidate, logger) -> dict:
    """성공 시 서버 응답 JSON(dict)을 반환. 실패 시 UploadError를 던진다."""
    import json

    wait_until_stable(candidate.video_path, logger)

    clip_events_json = json.dumps(
        {"clipData": candidate.clip_data, "eventData": candidate.event_data}
    )
    basic_game_data_json = json.dumps(candidate.basic_game_data)
    url = f"{server_url}/internal/dor/upload"
    headers = {"Authorization": f"Bearer {token}"}

    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with open(candidate.video_path, "rb") as video_f:
                files = {
                    "video": (candidate.video_filename, video_f, "video/mp4"),
                    "clip_events": (None, clip_events_json, "application/json"),
                    "basic_game_data": (None, basic_game_data_json, "application/json"),
                }
                resp = requests.post(
                    url, headers=headers, files=files, timeout=UPLOAD_TIMEOUT_SEC
                )
        except requests.RequestException as e:
            last_exc = e
            logger.warning(
                f"업로드 네트워크 오류 (시도 {attempt}/{MAX_ATTEMPTS}, {candidate.video_filename}): "
                f"{type(e).__name__}: {e}"
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SEC[min(attempt - 1, len(RETRY_BACKOFF_SEC) - 1)])
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError:
                return {"status": "ok"}

        if resp.status_code not in RETRYABLE_STATUS:
            raise UploadError(
                f"서버가 이 요청을 거부함 (status={resp.status_code}, body={resp.text[:200]}) - "
                f"재시도하지 않음"
            )

        logger.warning(
            f"업로드 실패 (시도 {attempt}/{MAX_ATTEMPTS}, status={resp.status_code}, "
            f"{candidate.video_filename})"
        )
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_BACKOFF_SEC[min(attempt - 1, len(RETRY_BACKOFF_SEC) - 1)])

    raise UploadError(f"재시도 {MAX_ATTEMPTS}회 모두 실패: {last_exc}")
