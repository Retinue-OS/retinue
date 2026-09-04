// Headless replay of the scripted day: prints the narration and the dashboard
// sections at a few points. Runs under Node (node replay.mjs) or Deno (deno run replay.mjs).
await import('./backends.js'); await import('./engine.js'); await import('./simulation.js');
const { hhmm } = globalThis.AttentionUtil;
const engine = new globalThis.AttentionEngine();
const sim = new globalThis.Simulation(engine, { beatHold: 0 });
const checkpoints = (globalThis.process?.argv?.slice(2) || globalThis.Deno?.args || []).map(Number);
const snap = (label) => {
  const s = engine.sections();
  const row = (i) => `      ${i.title} [${engine.level(i)} · imp ${i.importance} · ${engine.urgencyShort(i)} · ${engine.deliveryShort(i)}]`;
  console.log(`\n== ${label} · ${hhmm(engine.now)} · mode ${s.mode.name} ==`);
  console.log('   NOW'); s.now.forEach((i) => console.log(row(i)));
  console.log('   NEXT'); s.next.forEach((i) => console.log(row(i)));
  console.log('   HELD'); s.held.forEach((i) => console.log(row(i)));
  console.log('   WAITING'); s.waiting.forEach((i) => console.log(row(i)));
};
let printed = 0;
const flush = () => { for (; printed < sim.feed.length; printed++) { const f = sim.feed[printed]; console.log(`${hhmm(f.t)} ${f.who.padEnd(8)} ${f.text}`); } };
for (const c of checkpoints.length ? checkpoints : [8 * 60, 12 * 60 + 5, 16 * 60 + 40, 20 * 60 + 35, 1440]) { sim.advanceTo(c, false); flush(); snap('state'); }
console.log('\nstats', JSON.stringify(engine.stats()));
console.log('learned', engine.profile.learned.map((l) => `${hhmm(l.at)} ${l.text}`));
