import test from "node:test";
import assert from "node:assert/strict";
import { buttonsFor, costSummary, filterRows, formatUsd, groupByVersion, nextPollDelay, statusOf, versionButtons } from "../../hallway/static/table-logic.js";

const base = { id: "a", ok_online: false, characters: [], clips: [], publication: null, job: null };
const withClip = { ...base, clips: [{ id: "c1", version: 1 }] };

test("status", () => {
  assert.equal(statusOf(base), "new");
  assert.equal(statusOf({ ...base, job: { state: "running" } }), "running");
  assert.equal(statusOf({ ...base, job: { state: "error" } }), "failed");
  assert.equal(statusOf(withClip), "ready");
  assert.equal(statusOf({ ...withClip, publication: { channel: "public", clip_id: "c1" } }), "public");
});

test("a failed job with an older clip still shows the clip as ready", () => {
  assert.equal(statusOf({ ...withClip, job: { state: "error" } }), "ready");
});

test("public needs the OK online box, per version", () => {
  const v = versionButtons(withClip, withClip.clips[0]);
  assert.equal(v.public.enabled, false);
  assert.match(v.public.reason, /OK online/);
  assert.equal(v.preview.enabled, true);
  assert.equal(versionButtons({ ...withClip, ok_online: true }, withClip.clips[0]).public.enabled, true);
});

test("only the published version shows as active; the others can replace it", () => {
  const d = { ...base, ok_online: true, clips: [{ id: "c1", version: 1 }, { id: "c2", version: 2 }],
    publication: { channel: "public", clip_id: "c2" } };
  assert.equal(versionButtons(d, d.clips[1]).public.active, true);
  assert.equal(versionButtons(d, d.clips[0]).public.active, false);
  assert.equal(versionButtons(d, d.clips[1]).preview.active, false);
});

test("generate and remove are blocked while a job runs", () => {
  const b = buttonsFor({ ...base, job: { state: "queued" } });
  assert.equal(b.generate.enabled, false);
  assert.equal(b.remove.enabled, false);
});

test("generate is blocked when the cap is reached, but not for the free provider", () => {
  const b = buttonsFor(base, { remaining_micro: 0 });
  assert.equal(b.generate.enabled, false);
  assert.match(b.generate.reason, /cap/);
  assert.equal(buttonsFor(base, { remaining_micro: 0, free: true }).generate.enabled, true);
});

test("filter", () => {
  const rows = [base, withClip, { ...withClip, publication: { channel: "preview", clip_id: "c1" } }];
  assert.equal(filterRows(rows, "all").length, 3);
  assert.equal(filterRows(rows, "ready").length, 1);
  assert.equal(filterRows(rows, "preview").length, 1);
});

test("versions are grouped and ordered", () => {
  const g = groupByVersion([{ id: "x", version: 2 }, { id: "y", version: 1 }, { id: "z", version: 2 }, { id: "n" }]);
  assert.deepEqual(g.map((x) => [x.version, x.items.length]), [[1, 2], [2, 2]]);
});

test("cost modal text", () => {
  const est = { text_micro: 1000, image_micro: 33600, video_micro: 810000, max_figures: 4, max_micro: 945400, remaining_micro: 5e6 };
  const s = costSummary(est);
  assert.match(s.lines[0], /\$0\.88/);
  assert.equal(s.allowed, true);
  assert.equal(costSummary({ ...est, remaining_micro: 100 }).allowed, false);
  assert.equal(formatUsd(1000), "$0.001");
});

test("the free provider says it costs nothing", () => {
  const s = costSummary({ free: true, text_micro: 0, image_micro: 0, video_micro: 0, max_figures: 4, max_micro: 0, remaining_micro: 0 });
  assert.equal(s.free, true);
  assert.match(s.lines[0], /costs nothing/);
});

test("polling is faster while a job is active", () => {
  assert.ok(nextPollDelay([{ ...base, job: { state: "running" } }]) < nextPollDelay([base]));
});
