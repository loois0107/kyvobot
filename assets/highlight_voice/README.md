Static voice-line pools for `/highlight`'s 3-stage kill-buildup chain (이상감지 ->
감정격상 -> 킬폭발). `cogs/highlight.py` picks one file at random from each pool per
render (`BUILDUP1_POOL`/`BUILDUP2_POOL`/`HYPE_POOL`/`SUB_POOL`) - same glob+
`random.choice` pattern as `assets/highlight_sfx`'s SFX pool.

All lines are pure emotional/tension expressions with **zero factual claims** about
what's on screen (no "entering", "using a skill", "teamfight", etc.) - this is
deliberate, so the same pool works for literally any clip regardless of what's
actually happening. Only the real-time-synthesized main-caster line (built from
`cogs.highlight._generate_commentary`'s AI-generated, fact-checked text) references
actual killer/victim names.

Generated via ElevenLabs `eleven_v3` with `[excited][shouts]`/`[impressed]` delivery
tags (see `_build_highlight_voice_pool.py`, a one-time offline build script - already
run, not meant to be re-run unless the pools need to change).

## Pools

- **buildup1_\*.wav** (1단계, 이상감지) - anchored to kill - 2.0s
  - `buildup1_a.wav`: "어어?!" (LCK_Main_caster)
  - `buildup1_b.wav`: "어?! 뭔가...?!" (LCK_Main_caster)
- **buildup2_\*.wav** (2단계, 감정격상) - anchored to kill - 1.0s
  - `buildup2_a.wav`: "어어?! 분위기가?!" (LCK_Main_caster)
  - `buildup2_b.wav`: "잠시만요! 잠시만요!" (LCK_Main_caster)
  - `buildup2_c.wav`: "어어?! 조심해야죠!" (LCK_Main_caster)
  - `buildup2_d.wav`: "기류가 심상치 않은데요?!" (LCK_Main_caster)
- **hype_\*.wav** - plays right after the main line ends (sequential, no anchor needed)
  - `hype_a.wav`: "와아아아악!! 미쳤다!!" (LCK_Hype_Reaction)
  - `hype_b.wav`: "우와아!! 대박이다!!" (LCK_Hype_Reaction)
  - `hype_c.wav`: "미쳤어요 진짜!!" (LCK_Hype_Reaction)
- **sub_\*.wav** - plays right after hype ends (sequential, no anchor needed)
  - `sub_a.wav`: "아니, 이건 진짜 대담한 판단이에요!!" (Lck_Sub_Analyst)
  - `sub_b.wav`: "완전히 상황을 뒤집어버렸네요!!" (Lck_Sub_Analyst)
  - `sub_c.wav`: "이걸 해내네요, 진짜!!" (Lck_Sub_Analyst)

## Why buildup1/buildup2 need a measured emphasis point and hype/sub don't

Buildup1/buildup2 each get anchored so their own internal emphasis (the loudest
instant, measured offline via a 0.08s-window sliding-RMS scan - see
`BUILDUP_PEAK_T` in `cogs/highlight.py`) lands exactly at their target time
relative to the kill. Hype/sub are just played back-to-back after the main line
(and after each other) - they aren't aligned to an external target, so no
emphasis-point measurement is needed for them, only their own duration (probed at
render time, same as any other file).

## Scheduling

`cogs.highlight.plan_stages()` (pure function, unit-testable) places buildup1 at
kill-2.0s and buildup2 at kill-1.0s using their measured emphasis points, and
guarantees at least `STAGE2_STAGE3_MIN_GAP_SEC` (0.4s) of breathing room before the
main line starts at the kill. If there isn't room (very short clips, or a long
buildup2 pick landing right after a long buildup1 pick), it compresses buildup2
earlier, then buildup1 earlier still, and drops a stage entirely only if there's
truly no room left - never overlaps.
