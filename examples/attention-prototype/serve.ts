// Serve the prototype and proxy /transcribe to the self-hosted stt service, the
// way the gateway's /conversations/transcribe does — so voice input in the
// prototype is transcribed by your own Whisper, never by a browser vendor's cloud.
//
//   STT_URL=http://localhost:8100/transcribe STT_TOKEN=… deno run --allow-net --allow-read --allow-env serve.ts
//
// The page detects the endpoint (HEAD /transcribe) and routes recordings to it.
import { serveDir } from "jsr:@std/http/file-server";

const STT_URL = Deno.env.get("STT_URL") ?? "http://localhost:8100/transcribe";
const STT_TOKEN = Deno.env.get("STT_TOKEN") ?? "";
const PORT = Number(Deno.env.get("PORT") ?? "8000");
const ROOT = new URL(".", import.meta.url).pathname;

Deno.serve({ port: PORT }, async (req) => {
  const url = new URL(req.url);
  if (url.pathname === "/transcribe") {
    if (req.method === "HEAD" || req.method === "OPTIONS") return new Response(null, { status: 204 });
    if (req.method !== "POST") return new Response("POST the audio bytes", { status: 405 });
    const target = new URL(STT_URL);
    const lang = url.searchParams.get("lang");
    if (lang) target.searchParams.set("lang", lang);
    const headers = new Headers({ "content-type": req.headers.get("content-type") ?? "application/octet-stream" });
    if (STT_TOKEN) headers.set("authorization", `Bearer ${STT_TOKEN}`);
    try {
      const upstream = await fetch(target, { method: "POST", headers, body: await req.arrayBuffer() });
      return new Response(await upstream.text(), { status: upstream.status, headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" } });
    } catch (e) {
      return new Response(JSON.stringify({ error: `stt service unreachable at ${STT_URL}: ${e}` }), { status: 502, headers: { "content-type": "application/json" } });
    }
  }
  return serveDir(req, { fsRoot: ROOT, urlRoot: "", quiet: true });
});
console.log(`attention prototype on http://localhost:${PORT}/ — voice transcribed by ${STT_URL}`);
