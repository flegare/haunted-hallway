// Pure logic for the one-table console. No DOM, no network: testable with `node --test`.

export const STATUSES = ["new", "queued", "running", "failed", "ready", "preview", "public"];

export function statusOf(d) {
  if (d.publication) return d.publication.channel;
  const state = d.job && d.job.state;
  if (state === "queued" || state === "running") return state;
  if (d.clips.length > 0) return "ready";
  if (state === "error") return "failed";
  return "new";
}

export const STATUS_LABEL = {
  new: "New", queued: "Queued", running: "Working", failed: "Failed",
  ready: "Ready", preview: "Preview", public: "Public",
};

export function filterRows(rows, filter) {
  if (!filter || filter === "all") return rows;
  return rows.filter((d) => statusOf(d) === filter);
}

export function formatUsd(micro) {
  if (!Number.isFinite(micro)) return "?";
  return "$" + (micro / 1e6).toFixed(micro < 1e5 ? 3 : 2);
}

// Row-level buttons: make a clip, delete the drawing.
export function buttonsFor(d, spend) {
  const busy = ["queued", "running"].includes(statusOf(d));
  const out = {
    generate: { enabled: !busy, reason: busy ? "A clip is already being made for this drawing." : "" },
    remove: { enabled: !busy, reason: busy ? "Wait for the running job to finish." : "" },
  };
  if (spend && spend.remaining_micro <= 0 && !spend.free) {
    out.generate = { enabled: false, reason: "The spend cap is reached. Raise it in .env and restart." };
  }
  return out;
}

// Buttons of one clip version. A drawing has one published clip at a time:
// publishing another version replaces the first.
export function versionButtons(d, clip) {
  const pub = d.publication && d.publication.clip_id === clip.id ? d.publication.channel : null;
  return {
    preview: { enabled: true, active: pub === "preview", reason: "" },
    public: {
      enabled: d.ok_online, active: pub === "public",
      reason: d.ok_online ? "" : "Tick \"OK online\" first: the parent agreed to a public showing.",
    },
  };
}

// Group characters or clips by version: [{version: 1, items: [...]}, ...] in order.
export function groupByVersion(items) {
  const map = new Map();
  for (const it of items) {
    const v = it.version || 1;
    if (!map.has(v)) map.set(v, []);
    map.get(v).push(it);
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0]).map(([version, list]) => ({ version, items: list }));
}

// Text for the modal shown before every generation.
export function costSummary(est) {
  if (est.free) {
    return { lines: ["This is the free fake provider. It makes a fake clip and costs nothing."], allowed: true, typical: 0, free: true };
  }
  const typical = est.text_micro + 2 * est.image_micro + est.video_micro;
  return {
    lines: [
      `Typical clip with 2 figures: about ${formatUsd(typical)} (estimate).`,
      `Worst case with ${est.max_figures} figures: about ${formatUsd(est.max_micro)}.`,
      `Budget left: ${formatUsd(est.remaining_micro)}.`,
    ],
    allowed: est.remaining_micro >= est.max_micro, typical, free: false,
  };
}

export function nextPollDelay(rows) {
  const active = rows.some((d) => ["queued", "running"].includes(statusOf(d)));
  return active ? 1500 : 6000;
}
