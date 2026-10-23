// Voice activity detection: speech ends a phrase on time even with a keyboard clicking next to the microphone.
import test from "node:test";
import assert from "node:assert/strict";
import { Vad, features } from "../../../web/js/core/vad.js";

const SR = 16000, F = 320;
let seed = 1;
const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647 * 2 - 1; };
// 20 ms frames of a signal described by gen(t) → sample
function frames(sec, gen, t0 = 0) {
  const out = [];
  for (let f = 0; f < Math.round(sec * SR / F); f++) {
    const pcm = new Float32Array(F);
    for (let i = 0; i < F; i++) { const t = t0 + (f * F + i) / SR; pcm[i] = gen(t); }
    out.push(pcm);
  }
  return out;
}
const room = () => rnd() * 0.003;
// voiced speech: 120 Hz with harmonics, in 160 ms syllables with 60 ms gaps
const speech = (t) => {
  const on = (t % 0.22) < 0.16;
  let v = 0;
  for (let h = 1; h <= 6; h++) v += Math.sin(2 * Math.PI * 120 * h * t) / h;
  return room() + (on ? 0.12 * v : 0);
};
// keyboard: a 15 ms broadband click every 180 ms, as loud as the voice
const typing = (t) => room() + ((t % 0.18) < 0.015 ? 0.35 * rnd() * Math.exp(-((t % 0.18) / 0.004)) : 0);
const fan = (t) => room() + 0.02 * Math.sin(2 * Math.PI * 90 * t) + 0.004 * rnd();

function run(vad, list, speaking = false) {
  const st = { lp: 0 }, ev = [];
  list.forEach((pcm, i) => { const r = vad.push(features(pcm, st), speaking); if (r) ev.push([r, i * 20]); });
  return ev;
}

test("a phrase starts with the voice and ends after the pause", () => {
  const v = new Vad();
  const ev = run(v, [...frames(1, room), ...frames(1.5, speech, 1), ...frames(2, room, 2.5)]);
  assert.equal(ev[0][0], "start");
  assert.ok(ev[0][1] >= 1000 && ev[0][1] < 1200, `start at ${ev[0][1]}`);
  assert.equal(ev[1][0], "end");
  assert.ok(ev[1][1] >= 2500 + 1200 && ev[1][1] < 2500 + 1600, `end at ${ev[1][1]}`);
});

test("typing never starts a phrase", () => {
  assert.deepEqual(run(new Vad(), [...frames(1, room), ...frames(6, typing, 1)]), []);
});

test("typing after the words does not keep the phrase open", () => {
  const ev = run(new Vad(), [...frames(1, room), ...frames(1.2, speech, 1), ...frames(6, typing, 2.2)]);
  assert.deepEqual(ev.map((e) => e[0]), ["start", "end"]);
  assert.ok(ev[1][1] < 2200 + 1700, `ended at ${ev[1][1]} ms (speech ended at 2200)`);
});

test("a steady fan is learned within seconds; speech over it still starts and ends", () => {
  const v = new Vad();
  const ev = run(v, [...frames(8, fan), ...frames(1.5, (t) => fan(t) + speech(t) * 1.2, 8), ...frames(3, fan, 9.5)]);
  const after = ev.filter((e) => e[1] >= 7000);
  assert.deepEqual(after.map((e) => e[0]), ["start", "end"], JSON.stringify(ev));
  assert.ok(after[1][1] < 9500 + 1700);
});

test("a long recording is cut at the limit", () => {
  const v = new Vad({ maxMs: 12000 });
  const ev = run(v, frames(15, speech));
  assert.deepEqual(ev.map((e) => e[0]).slice(0, 2), ["start", "max"]);
});

test("while JARVIS speaks the person can still cut in", () => {
  const ev = run(new Vad(), [...frames(0.5, room), ...frames(1, (t) => speech(t) * 2, 0.5)], true);
  assert.equal(ev[0]?.[0], "start");
});
