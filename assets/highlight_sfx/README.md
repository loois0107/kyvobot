Crowd-cheer stingers for the `/highlight` command's kill sound effects.

`cogs/highlight.py` picks one at random per kill event (see `SFX_POOL`).

## crowd_cheer_1.wav / crowd_cheer_3.wav

Source: Mixkit (https://mixkit.co/free-sound-effects/), Sound Effects Free License
(commercial use allowed, no attribution required) - IDs 462 ("Huge crowd cheering victory")
and 363 ("Stadium chaotic loud applause, drums and chants"). Trimmed to ~2.7s, `aecho`
reverb (`0.8:0.85:60|150|300:0.35|0.22|0.12`), `loudnorm=I=-13:TP=-1.0:LRA=11`. These are
"instant burst" stingers - kill time = file start, no lead-in (`SFX_LEAD_MS` default 0).

## crowd_cheer_2.wav

Source: Pixabay (https://pixabay.com/sound-effects/people-free-crowd-cheering-sounds-03-strong-cheering-i-116189/),
Pixabay Content License (commercial use allowed, no attribution required). Trimmed 3-16s
(13s) from the original. Unlike the other two, this one is a "tension builds, then bursts"
arc, so it needs a lead-in - `SFX_LEAD_MS["crowd_cheer_2.wav"] = 6000` places the file so its
final loud jump (see below) lands exactly at the kill moment, not the file start.

Processing chain (order matters - see the two lessons below):
1. `loudnorm=I=-18:TP=-3:LRA=7` - flattens the source's own natural dynamics into a
   consistent baseline *before* shaping it, so our envelope (next step) is the dominant
   shaping factor instead of fighting the source's own contour.
2. `afade=t=in:st=0:d=0.03` - 30ms declick fade-in.
3. `asetnsamples=n=64:p=0,volume=eval=frame:volume='pow(10,(...)/20)'` - the crescendo
   envelope itself: quiet 0-1.5s, gentle +5dB rise 1.5-4.5s, a deliberate **big +9dB jump
   4.5-6.0s** (this is the "burst"), then holds +14dB from 6.0s on.
4. `aecho=0.8:0.85:60|150|300:0.35|0.22|0.12` - same reverb as the other two.
5. `afade=t=out:st=12.5:d=0.5` - fadeout.
6. `volume=<precise dB>` - a *calculated* flat gain (not `alimiter`!) that pulls the true
   peak down to exactly -6.0dBFS, measured via `astats` on the un-gained output and solved
   for algebraically. See lesson 2 below for why this isn't `alimiter`.

Because this file already carries its own big dynamic swing, it's exempted from the
render-time `SFX_MIX_GAIN_DB` boost (`SFX_MIX_GAIN_DB_OVERRIDE["crowd_cheer_2.wav"] = 0.0`)
- adding another +6dB on top of an already-hot asset just made the render-time `alimiter`
clamp it back down, erasing the jump.

## crowd_cheer_4.wav

Source: same Pixabay 116189 track as `crowd_cheer_2.wav`, but this time the **full**
25.03s original (not a 3-16s excerpt) - built to be a genuinely continuous background
bed instead of a short stinger, so the crowd is audible from clip t=0 and swells at
the kill rather than starting from silence (validated in an earlier prototyping round
this same session; this is that design ported into a static asset instead of a
per-render filter chain).

Processing chain:
1. Trim to 23.0s, then **`loudnorm=I=-18:TP=-3:LRA=1` followed by two cascaded
   `acompressor` stages** (`threshold=-24dB:ratio=20:attack=1:release=50:makeup=1`,
   then `threshold=-20dB:ratio=20:...`) - this source has ~50dB of its own natural
   dynamic range (true silence at t=0 rising to a brief natural peak around t=7-8.5s,
   then declining for the rest of its length). A single `loudnorm` pass (as used for
   `crowd_cheer_2.wav`) was nowhere near aggressive enough to flatten that - measured
   with this file's actual raw material, RMS still swung from -19dB to -38dB across
   the span after loudnorm alone. Stacking a tight-LRA loudnorm with two hard
   compressors crushed it to within ~±2-3dB almost everywhere from t=0.2s to t=22s -
   see lesson 3 below.
2. 30ms declick fade-in, same as `crowd_cheer_2.wav`.
3. Measure this flattened bed's own RMS at t=3-5s as a reference level, then apply a
   **dB-anchored gain envelope** built directly against that reference (not the old
   amplitude-linear ramp): flat at -28.7dB(baseline, 0-14.0s) -> dB-linear ramp up to
   -9.3dB(peak) over 14.0-14.8s -> hold at -9.3dB until the fadeout. -28.7/-9.3dB are
   the actual measured baseline/peak levels a previous prototyping round confirmed
   sound right for this exact balance - not arbitrary. Same `asetnsamples=n=64:p=0`
   anti-crackle chunking as the technique below.
4. Same reverb as the other three (`aecho=0.8:0.85:60|150|300:0.35|0.22|0.12`).
5. `afade=t=out:st=21.5:d=1.5` - the source's own natural material starts collapsing
   on its own past ~t=22s anyway (see lesson 3), so the fade is timed to finish before
   that natural cliff rather than fight it.
6. **No final peak-targeted `volume=Xdb` calibration** (unlike `crowd_cheer_2.wav`) -
   see lesson 4 below for why that step was actively wrong for this asset and was
   dropped after measuring its effect.

`SFX_LEAD_MS["crowd_cheer_4.wav"] = 14800` (14.0s baseline + 0.8s ramp - the point the
swell *completes*, same convention as `crowd_cheer_2.wav`). Exempted from
`SFX_MIX_GAIN_DB` like `crowd_cheer_2.wav` (`SFX_MIX_GAIN_DB_OVERRIDE = 0.0`) since it
already peaks near 0dBFS on its own.

**Known accepted limitation**: like `crowd_cheer_2.wav`, if the kill happens more than
~14.8s into the clip, the lead-time clamps to 0 and the bed starts from clip t=0 but
the swell now lands *late* relative to the kill instead of exactly on it. And even
within the normal case, the bed's own natural runway is short (23s file) - if the
kill happens very early and the post-kill commentary tail runs long, the bed will
fade out (by design, not a cutoff) before the tail finishes. Both are consequences of
using a bounded static asset instead of live per-render synthesis; not fixed here.

### Three more hard-won lessons (crowd_cheer_4.wav)

3. **A single `loudnorm` pass does not fully flatten a source with a huge (~50dB)
   natural dynamic range - a much more aggressive chain is needed, and it's worth
   directly measuring the result before trusting it.** The first attempt at this
   asset used the exact same `loudnorm=I=-18:TP=-3:LRA=7` as `crowd_cheer_2.wav` and
   trusted it to produce a flat bed; measuring the actual output afterward showed a
   massive residual contour (the source's own quiet-start/mid-peak/late-decline arc
   still clearly visible, RMS swinging ~19dB across the span) that completely broke
   the "flat baseline, flat hold" design - the baseline region measured as low as
   -38dB in spots instead of the intended -28.7dB. Tightening `LRA` to 1 and adding
   two cascaded hard `acompressor` stages afterward brought the swing down to ~2-3dB,
   which is what actually shipped. Lesson: never assume a normalizer fully flattened
   something just because it ran without error - measure the actual RMS contour at
   several points before building anything on top of the assumption.
4. **A final "calibrate flat gain to hit a target peak dBFS" step (the technique used
   for `crowd_cheer_2.wav`, see lesson 2) is wrong when the goal is to hit *specific
   absolute RMS reference levels* rather than just "don't clip while preserving
   shape".** Adding that step here shifted the whole envelope by a constant amount
   (measured: about -8dB off from the intended -28.7/-9.3dB targets), because the
   calibration target (a peak dBFS value) was unrelated to the envelope's own
   already-correct absolute RMS design. Dropped that step entirely for this asset -
   the envelope's own dB-anchored design already produces a safe peak (~-0.5dBFS)
   with no separate calibration needed. Lesson: the "measure peak, solve for exact
   flat gain" technique is for *preserving a shape while capping level*; it's the
   wrong tool when the shape itself is defined by fixed absolute levels.

### Two hard-won lessons (don't repeat these)

1. **`volume=eval=frame` with a continuous time-expression causes audible crackle.** It
   recomputes gain once per *audio frame*, not per sample, so a "smooth" ramp is actually
   applied as a staircase of discrete jumps - each frame boundary is an audible click, and
   loudnorm's own internal framing made the frames here ~150-200ms, i.e. clicking 5-6
   times/sec. Confirmed via spectrogram: periodic broadband vertical spikes up to 80kHz+
   (impossible for real crowd audio) appeared starting exactly at this filter and nowhere
   before it. Fix: prepend `asetnsamples=n=64:p=0` right before the `volume` filter to force
   tiny (~1.3ms) frames, making the staircase steps too small to hear. Verified clean via
   the same spectrogram check afterward.
2. **`alimiter`'s `limit=` is not a reliable/precise ceiling for dense, loud program
   material** (it undershot/overshot the configured value by several dB in testing here,
   both standalone and chained after other filters) - and worse, when it *does* engage
   hard, it compresses the loud parts more than the quiet parts, flattening a
   deliberately-designed dynamic jump right when you need it most (this is what silently
   killed the "burst" feel in earlier attempts, even though the envelope math was correct).
   Fix used here: don't use `alimiter` for final ceiling control on a file with an
   intentional dynamic arc. Instead measure the actual peak with `astats` on the ungained
   output and apply a single precise `volume=Xdb` to hit a target peak exactly - this is a
   flat linear gain, so it preserves 100% of the relative shape you designed.
