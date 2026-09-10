Static voice-line pools for `/highlight`'s 2-stage kill-reaction structure (전면 재설계 -
이전의 "이상감지 -> 감정격상 -> 킬폭발" 빌드업 체인은 완전히 대체됐다. `buildup1_*.wav`/
`buildup2_*.wav` 파일은 디스크에는 남아있지만 이제 코드에서 참조되지 않는다).

## 새 구조

- **1단계 (킬 순간)**: Main + Hype + Sub 세 목소리가 `kill_t`에 완전히 동시에 "감탄사+
  닉네임" 계열의 짧은 샤우팅을 외친다.
- **2단계 (리액션, 1단계 종료 후 살짝 겹치며 순서대로 진행)**: Hype(굵고 강함, 사실 서술)
  -> Sub(얇음, 의문형 감탄) -> Main(빠르고 중간 톤, 짧은 감탄).

닉네임이 실제로 필요한 자리는 정확히 두 곳뿐이다 - 1단계의 Main(닉네임 샤우팅)과 2단계의
Hype(사실 서술, "누가 누구를 처치했는지"). 이 둘만 렌더당 ElevenLabs 실시간 호출로
합성하고(`cogs.highlight.KyvoHighlight._synthesize_voice_line`, 렌더당 정확히 2회), 나머지
네 자리(1단계 Hype/Sub, 2단계 Sub/Main)는 닉네임이 필요 없는 순수 감정 표현이라 이 폴더의
정적 풀에서 고른다(`random.choice`, 기존 SFX 풀과 같은 glob 패턴).

**기술적 판단 - 왜 1단계도 실시간 TTS 하나를 더 쓰는지**: 사용자 요구사항이 "1단계 샤우팅에
실제 닉네임이 들어가야 한다"였고, 닉네임은 정적 파일로 미리 구워둘 수 없다(매 킬마다 다름).
선택지는 (a) 1단계에서 닉네임을 포기하고 순수 감탄사만 쓰거나, (b) 셋 중 하나(Main)만
실시간 TTS로 "감탄사+닉네임"을 외치게 하는 것 - (b)를 택했다. Main 하나만 실시간으로 유지하면
렌더당 TTS 호출이 기존 1회에서 2회로만 늘고(2단계 Hype의 사실 서술은 원래도 실시간이었음),
Hype/Sub 둘 다 실시간으로 만드는 것보다 훨씬 저렴하면서도 "닉네임이 실제로 들린다"는
요구사항은 그대로 충족된다. 문자 수 자체도 짧은 외침이라("우와아아아악!! {killer}~~!!")
비용 부담은 크지 않다(실측: assets 빌드 6줄 합쳐 126자, ElevenLabs free tier 10000자/기간).

## Pools

### 1단계 (킬 순간 동시 샤우팅)

- **`hype_*.wav`** (LCK_Hype_Reaction) - 예전엔 "메인이 끝난 뒤 순차 재생"되는 역할이었는데,
  내용 자체가 이미 짧은 순수 폭발형 감탄사라 그대로 재사용한다.
  - `hype_a.wav`: "와아아아악!! 미쳤어요!!"
  - `hype_b.wav`: "우와아!! 대박이에요!!"
  - `hype_c.wav`: "미쳤어요 진짜!!"

  **버그 수정 이력**: `hype_a.wav`/`hype_b.wav`는 원래 각각 "미쳤다!!"/"대박이다!!"로 반말체
  녹음돼 있었다 - `cogs.highlight.SYSTEM_PROMPT`를 존댓말 전용으로 다시 쓴 라운드에서
  `hype_b.wav`만 TODO로 표시하고 넘어갔었는데, `hype_a.wav`도 같은 문제였다는 게 나중에
  확인됨(`hype_c.wav`만 우연히 존댓말이라 프로덕션에서 2/3 확률로 반말이 실제로 나가고
  있었음에도 한동안 못 잡았음). 감탄사 프리픽스("와아아아악!!"/"우와아!!")는 그대로 두고
  종결어미만 존댓말로 다시 녹음해서 고쳤다.
- **`sub_shout_*.wav`** (Lck_Sub_Analyst, 신규) - 닉네임 없는 순수 폭발형 감탄사. 기존
  `sub_*.wav`(분석가 멘트)는 이 역할에 안 맞아서 새로 녹음.
  - `sub_shout_a.wav`: "우와아아아!!"
  - `sub_shout_b.wav`: "허어어!!"
- Main은 정적 풀이 아니라 `MAIN_SHOUT_TEMPLATE`("우와아아아악!! {killer}~~!!")을 실시간
  TTS로 합성 - 유일하게 닉네임이 들어가는 1단계 목소리.

### 2단계 (리액션 체인)

- Hype는 정적 풀이 아니라 `cogs.highlight._generate_commentary`(GPT 실시간 생성, "누가
  누구를 처치했는지" 사실 서술)를 실시간 TTS로 합성 - 조사 검증(`_i_or_ga`/`_eul_or_reul`,
  `_commentary_names_killer`)도 이번 라운드에 Main에서 Hype로 그대로 옮겨왔다.
- **`sub_question_*.wav`** (Lck_Sub_Analyst, 신규) - 의문형 감탄, 존댓말.
  - `sub_question_a.wav`: "진짜 돌았는데요??!!"
  - `sub_question_b.wav`: "이게 실화예요??!!"
- **`main_react_*.wav`** (LCK_Main_caster, 신규) - 아주 짧은 감탄, 상황 무관.
  - `main_react_a.wav`: "와...."
  - `main_react_b.wav`: "허....."

모두 ElevenLabs `eleven_v3`, `[excited][shouts]`(샤우팅류) 또는 `[impressed]`(main_react)
태그로 생성. 예전 빌드업 풀들(`buildup1_*.wav`/`buildup2_*.wav`, `BUILDUP_PEAK_T` 기반
실측 강조지점 정렬)은 이번 재설계로 완전히 대체됐다 - 파일은 지우지 않았지만 코드가 더는
참조하지 않는다.

## Scheduling

`cogs.highlight.plan_stage2_chain()`(순수 함수, 테스트 가능)이 2단계 타이밍을 계산한다:

```
hype_start = stage1_end + STAGE1_STAGE2_GAP_SEC
sub_start  = hype_start + hype_fact_duration * STAGE2_OVERLAP_RATIO
main_start = sub_start  + sub_question_duration * STAGE2_OVERLAP_RATIO
```

`STAGE2_OVERLAP_RATIO`(현재 0.75)는 "앞 목소리가 75% 지점까지 왔을 때 다음 목소리가
끼어든다"는 뜻 - 완전 동시(뭉개짐)도 완전 순차(지루함)도 아닌 중간 지점을 노린 값이다.
정확히 "얼마나 겹쳐야 자연스러운가"는 결국 사람이 들어봐야 판단할 수 있는 영역이라, 이
상수 하나만 바꾸면 쉽게 다시 튜닝할 수 있게 남겨뒀다.

`stage1_end`는 1단계 세 목소리(Main 샤우팅/Hype/Sub) 중 가장 길게 끝나는 것의 종료
시각이다 - 셋 다 `kill_t`에서 동시에 시작하므로 `kill_t + max(세 duration)`.
