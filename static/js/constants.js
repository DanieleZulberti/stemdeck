import { t, TRANSLATIONS } from "./i18n.js";

// Fallback list used before /api/config responds. Kept in sync with
// STEM_NAMES in app/core/config.py — the API is the canonical source.
export let STEM_NAMES = ["vocals", "drums", "bass", "guitar", "piano", "other"];
export let TRACK_NAMES = ["original", ...STEM_NAMES];

// Stems from the on-demand lead/backing vocal split (#275, EXTRA_STEM_NAMES
// in app/core/config.py). Not folded into STEM_NAMES/TRACK_NAMES -- most jobs
// never run this split -- but the player/mixer render them as real lanes,
// swapped in for "vocals", via effectiveStemOrder() below.
export let EXTRA_STEM_NAMES = ["lead_vocals", "backing_vocals"];

export async function syncStemNamesFromAPI() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) return;
    const data = await res.json();
    if (Array.isArray(data.stem_names) && data.stem_names.length > 0) {
      STEM_NAMES = data.stem_names;
      TRACK_NAMES = ["original", ...STEM_NAMES];
    }
    if (Array.isArray(data.extra_stem_names) && data.extra_stem_names.length > 0) {
      EXTRA_STEM_NAMES = data.extra_stem_names;
    }
  } catch (e) {
    console.warn("[constants] failed to sync stem names from API:", e);
  }
}

// The lane order for a specific job: STEM_NAMES with "vocals" replaced by
// lead_vocals + backing_vocals when a job's on-demand split (#275) has
// produced both. `presentNames` is the Set of stem names the job actually
// has (typically from job.stems). Order matters -- callers use this both to
// decide what to render and in what sequence (waveform stacking, mixer rows).
export function effectiveStemOrder(presentNames) {
  const splitDone = presentNames.has("lead_vocals") && presentNames.has("backing_vocals");
  return STEM_NAMES.flatMap((n) => (n === "vocals" && splitDone ? EXTRA_STEM_NAMES : [n]));
}

// A Proxy, not a plain object, so every lookup resolves through the CURRENT
// language live -- a plain object would freeze these labels in whatever
// language was active the moment this module first loaded. Callers keep their
// existing `STEM_DISPLAY[name] || name` fallback pattern unchanged: an
// unrecognized name (not one of ours) still yields undefined, same as a plain
// object would, rather than the i18n engine's own missing-key fallback
// (which returns the raw dictionary key string -- wrong here).
export const STEM_DISPLAY = new Proxy({}, {
  get(_target, prop) {
    if (typeof prop !== "string") return undefined;
    const key = `stem.${prop}`;
    return key in TRANSLATIONS.en ? t(key) : undefined;
  },
});

// FL Studio-style channel palette: saturated but slightly dusty, designed
// to read well on a dark background.
export const SEMANTIC_COLORS = {
  mixture: "#a8b0bd",
  unknown_audio: "#88a8c8",
  vocals: "#e85f6f",
  lead_vocals: "#e8748a",
  backing_vocals: "#c98fe0",
  choir: "#d77aa8",
  drums: "#e89048",
  bass: "#e8b848",
  instrument: "#78a8b8",
  guitar: "#88d878",
  lead_guitar: "#9ae68b",
  rhythm_guitar: "#69bd68",
  keys: "#aa8fe0",
  piano: "#b88fe8",
  keyboard: "#9f88dc",
};

export const SEMANTIC_PARENTS = {
  lead_vocals: "vocals",
  backing_vocals: "vocals",
  choir: "vocals",
  guitar: "instrument",
  lead_guitar: "guitar",
  rhythm_guitar: "guitar",
  bass: "instrument",
  drums: "instrument",
  keys: "instrument",
  piano: "keys",
  keyboard: "keys",
};

export const ARTIFACT_COLORS = {
  audio: "#88a8c8",
  midi: "#62b8e8",
  json: "#8f9aa6",
  text: "#b58bd8",
};

/**
 * Resolve the visual color for an artifact semantic type. Colors are derived
 * presentation metadata: they are never persisted in FlowDocument/FlowRun.
 */
export function semanticColor(semanticType, legacyName = null, artifactType = "audio") {
  let current = semanticType || null;
  const seen = new Set();
  while (current && !seen.has(current)) {
    seen.add(current);
    if (SEMANTIC_COLORS[current]) return SEMANTIC_COLORS[current];
    current = SEMANTIC_PARENTS[current] || null;
  }
  if (legacyName === "other") return SEMANTIC_COLORS.unknown_audio;
  if (legacyName === "original") return SEMANTIC_COLORS.mixture;
  if (legacyName && SEMANTIC_COLORS[legacyName]) return SEMANTIC_COLORS[legacyName];
  return ARTIFACT_COLORS[artifactType] || "#a0a0a0";
}

// Backward-compatible legacy stem-name lookup. New Flow code should prefer
// semanticColor(semanticType, name, artifactType).
export const STEM_COLORS = {
  vocals: SEMANTIC_COLORS.vocals,
  drums: SEMANTIC_COLORS.drums,
  bass: SEMANTIC_COLORS.bass,
  guitar: SEMANTIC_COLORS.guitar,
  piano: SEMANTIC_COLORS.piano,
  other: SEMANTIC_COLORS.unknown_audio,
  original: SEMANTIC_COLORS.mixture,
  lead_vocals: SEMANTIC_COLORS.lead_vocals,
  backing_vocals: SEMANTIC_COLORS.backing_vocals,
};

export const PROGRESS_COLOR = "#3a3a3a";

export const LOOP_DEFAULT_START_FRAC = 0.25;
export const LOOP_DEFAULT_END_FRAC = 0.5;

export const LANE_VOLUME_MAX = 2;