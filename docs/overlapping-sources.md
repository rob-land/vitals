# Handling overlapping data from multiple devices

**Status: implemented and live.** What's built:

- **Done — trust model.** Plugins declare per-metric `SENSOR_QUALITY`
  (dedicated instruments rate "high"); `DeviceManager.source_trust(metric)`
  turns that plus a per-device user preference into a `{device_id: rank}`
  map.
- **Done — source-resolved aggregation.** `Store.aggregate(...,
  source_trust=…)` resolves each time bucket to a single source (highest
  rank, then most samples) and applies the op to only that source's
  values — additive metrics no longer double-count and point metrics no
  longer blend across sources. It can also flag dropped sources that
  materially disagree (`discrepancy_threshold`).
- **Done — dashboard.** The Dashboard passes the trust map, so steps,
  energy, water, weight and heart rate are now source-resolved.
- **Done — UI surfacing.** The dashboard's steps and heart-rate cards
  name the source they resolved to and show a warning when a dropped
  source materially disagrees. The device detail page shows a "Preferred
  source" switch per contested metric (persisted to
  `settings["preferred_metrics"]`, which `source_trust` honours).

The rest of this note is the original scoping that the implementation
follows.

When more than one device reports the same metric over the same time —
e.g. heart rate from both a Pebble and a smart ring, or steps from a
watch and a ring — Vitals needs a defined way to present one coherent
picture.

## Current behaviour (the problem)

`store.aggregate(type, op, bucket)` selects every sample of a type in
the window and applies one op (`sum`/`avg`/`min`/`max`) across **all of
them, regardless of source**. So today:

- **Additive metrics** — `step_count`, `active_energy`,
  `dietary_energy`, `water_intake` — are **summed across sources**. Two
  devices that both count steps ⇒ the dashboard shows ~double the real
  step count. This is a latent correctness bug that goes live the moment
  a second additive source exists.
- **Point metrics** — `heart_rate` is `avg`/`min`/`max` per bucket
  **blended across sources**. With a Pebble *and* the ring both logging
  HR (the current setup), the resting-HR tiles and the HR chart mix a
  wrist-PPG watch reading with a ring reading; the daily `max` can be a
  spurious spike from either.

Raw records are always kept per-source (every sample carries
`source_id` → device), so this is purely a *presentation/aggregation*
decision and is fully reversible.

## The key distinction: metric classes

The right rule depends on what kind of quantity it is:

1. **Additive / cumulative** (`step_count`, `distance`,
   `active_energy`, `dietary_energy`, `water_intake`). Summing across
   sources double-counts. Two devices measuring the same walk are *not*
   two walks.
2. **Point-in-time observations** (`heart_rate`, `oxygen_saturation`,
   `body_temperature`, `body_weight`, blood pressure). Overlapping
   readings are redundant or contradictory, never additive — you must
   pick or reconcile, not add.
3. **Episodes / intervals** (`sleep_episode`, `workout`). Two devices
   describe the *same* night or session differently; merging their stage
   timelines produces nonsense.

A one-size aggregate can't be correct across all three.

## Options

### A. Source priority (trust ranking), per metric
Rank devices per metric; show the highest-ranked source that has data,
suppress the rest (still stored, just not counted). Best refinement:
**primary-with-fallback** — use the primary where it has data, fall back
to a secondary only in gaps, so coverage doesn't suffer.
- **Pros:** no double-counting; one clean number; respects sensor
  quality (a dedicated ring/chest strap beats a watch's wrist PPG for
  HR; a real BP cuff beats a ring's "BP").
- **Cons:** needs a trust model and a tie-breaker; a bad primary hides a
  better-covered secondary unless fallback is implemented.

### B. Averaging / fusion
Combine overlapping readings — e.g. a trust-weighted average of HR in the
same minute.
- **Pros:** uses all data; smooths noise among *equal-quality* sensors.
- **Cons:** averaging a good sensor with a bad one drags the good one
  down; meaningless for additive metrics (you don't average two step
  totals); actively harmful for the ring's fabricated metrics (averaging
  a real value with a fake one launders the fake one). As trust weights
  skew, a weighted average degenerates into "use the good one" anyway —
  i.e. Option A with extra steps.

### C. Show every source separately
Plot each device as its own series; let the user see both. The Timeline
already tags each record's source.
- **Pros:** honest, zero data loss, no hidden assumptions. Ideal for the
  Timeline and for a detailed HR chart.
- **Cons:** doesn't answer the Dashboard's "one number" questions
  (today's steps, resting HR); clutters at-a-glance views.

### D. Discrepancy flagging
Detect when two sources materially disagree at the same time and surface
a badge/annotation. This is an **overlay** on A/C, not a resolution by
itself.
- **Pros:** catches real problems — bad fit, device not worn, sensor
  drift/fault — that a silent pick or average would hide.
- **Cons:** needs per-metric thresholds; no value on its own without a
  base display strategy.

## Recommendation: composite, by metric class

- **Additive metrics:** never sum across sources. Pick **one primary
  source per metric per day** (highest-trust source with data), with
  optional fallback to a secondary for periods the primary is missing.
  This is the non-negotiable correctness fix — it stops double-counting.
  (Within a single source, interval deltas still sum; the rule is
  strictly *cross-source*.)
- **Point metrics:** trust-rank and show the primary series; optionally
  overlay secondaries faintly on the detail chart. Averaging is
  acceptable **only among same-quality sources**, never across tiers.
  Resting-HR / min / max tiles read from the primary source only.
- **Episodes:** choose one source per night/session (the device actually
  worn / most reliable for that metric); don't merge stage timelines.
- **Everywhere:** keep Option D as an overlay — when two *trusted*
  sources disagree beyond a threshold (say HR > ~15 bpm at the same
  minute, or additive totals differing > ~25%), annotate it rather than
  silently hiding one.
- **Fabricated metrics** (the cheap ring's BP / glucose / lipids / uric
  acid): give them a very low or zero trust so they never override a
  real measurement, and never average them into one.

## A trust model

A per-`(device, metric)` trust score drives A and D:

- **Defaults from device + sensor class.** Encode sensor-quality hints
  in the plugin (e.g. a `SENSOR_QUALITY = {"heart_rate": "high", …}`
  capability on `Device`), seeded from what each sensor really is: a
  dedicated ring/strap HR > watch wrist HR > phone; a real cuff/scale >
  a ring's estimate.
- **User override.** A per-device "prefer this device for: heart rate,
  steps, …" setting in the device detail page (mirrors the monitoring
  toggle already there), so the user has the final say.
- **Fabricated-metric floor.** Estimated pseudo-medical metrics default
  to the lowest tier regardless of device.

## Implementation touch-points (for when we build it)

- **`store.aggregate`** grows a source-resolution mode: for additive
  metrics, resolve to a single source per bucket (primary + fallback)
  instead of summing all; expose the chosen source in the result.
- **Trust config:** plugin-declared per-metric quality + a per-device
  user override persisted in the device registry settings.
- **Dashboard:** primary series + optional secondary overlay;
  discrepancy badges on tiles.
- **Timeline:** already source-tagged; add discrepancy annotations and
  optionally collapse duplicate point-readings.

## Guiding principles

1. Additive metrics: **choose, never sum** across sources.
2. Point metrics: **trust-rank; don't blend quality tiers.**
3. Never average or reconcile fabricated metrics — exclude low-trust
   sources for those entirely.
4. Make provenance visible and **flag disagreement** instead of silently
   hiding a source.
5. Keep all raw records (already true) — resolution stays a
   presentation-layer choice, configurable and reversible.
