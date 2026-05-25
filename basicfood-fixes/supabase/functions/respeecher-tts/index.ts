// Generate Ukrainian TTS via Respeecher Public Realtime API and upload WAVs to Storage.
// Endpoint: POST /v1/public/tts/ua-rt/tts/sse  (NDJSON stream of float32 PCM chunks).
// We convert float32 → int16 PCM and wrap in a 24kHz mono WAV container.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.95.0";
import { requireInternalCaller } from "../_shared/auth.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

const BASE = "https://api.respeecher.com/v1/public/tts/ua-rt";
const VOICE_ID = "olesia-media"; // warm female Ukrainian narrator
const SAMPLE_RATE = 24000;

const LINES = [
  { id: 1, text: "BASIC.FOOD — натуральні смаколики для вашого улюбленця." },
  { id: 2, text: "Тепер замовити ще простіше: новий сайт BASIC.FOOD.SHOP і зручний Telegram-бот." },
  { id: 3, text: "Обирай улюблені ласощі, додавай у кошик і оформлюй замовлення лише за тридцять секунд." },
  { id: 4, text: "Тільки чесний склад, натуральне м'ясо і швидка доставка по всій Україні." },
  { id: 5, text: "Заходь на BASIC.FOOD.SHOP або відкривай нашого бота в Telegram — твій вихованець скаже дякую!" },
];

function f32leToInt16(buf: Uint8Array): Uint8Array {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const n = Math.floor(buf.length / 4);
  const out = new Uint8Array(n * 2);
  const dvOut = new DataView(out.buffer);
  for (let i = 0; i < n; i++) {
    let s = dv.getFloat32(i * 4, true);
    if (s > 1) s = 1;
    else if (s < -1) s = -1;
    dvOut.setInt16(i * 2, Math.round(s * 32767), true);
  }
  return out;
}

function pcmToWav(pcm: Uint8Array, sampleRate: number): Uint8Array {
  const numChannels = 1;
  const bps = 16;
  const byteRate = sampleRate * numChannels * (bps / 8);
  const blockAlign = numChannels * (bps / 8);
  const dataSize = pcm.length;
  const buf = new ArrayBuffer(44 + dataSize);
  const dv = new DataView(buf);
  const w = (o: number, s: string) => {
    for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i));
  };
  w(0, "RIFF");
  dv.setUint32(4, 36 + dataSize, true);
  w(8, "WAVE");
  w(12, "fmt ");
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true);
  dv.setUint16(22, numChannels, true);
  dv.setUint32(24, sampleRate, true);
  dv.setUint32(28, byteRate, true);
  dv.setUint16(32, blockAlign, true);
  dv.setUint16(34, bps, true);
  w(36, "data");
  dv.setUint32(40, dataSize, true);
  new Uint8Array(buf, 44).set(pcm);
  return new Uint8Array(buf);
}

async function ttsToWav(apiKey: string, text: string): Promise<Uint8Array> {
  const r = await fetch(`${BASE}/tts/sse`, {
    method: "POST",
    headers: {
      "X-API-Key": apiKey,
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      transcript: text,
      voice: { id: VOICE_ID },
      output_format: { container: "raw", sample_rate: SAMPLE_RATE },
    }),
  });
  if (!r.ok || !r.body) throw new Error(`tts [${r.status}]: ${await r.text().catch(() => "")}`);

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  const parts: Uint8Array[] = [];
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) !== -1) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      try {
        const obj = JSON.parse(line);
        if (obj.error) throw new Error(`tts payload: ${JSON.stringify(obj.error)}`);
        if (obj.type === "chunk" && obj.data) {
          const bin = atob(obj.data);
          const arr = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          parts.push(arr);
        }
      } catch (e) {
        if ((e as Error).message?.startsWith("tts payload")) throw e;
      }
    }
  }
  if (parts.length === 0) throw new Error("no chunks");
  const total = parts.reduce((n, p) => n + p.length, 0);
  const f32 = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    f32.set(p, off);
    off += p.length;
  }
  const pcm16 = f32leToInt16(f32);
  return pcmToWav(pcm16, SAMPLE_RATE);
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  const gate = await requireInternalCaller(req);
  if (!gate.ok) return gate.response;

  try {
    const apiKey = Deno.env.get("RESPEECHER_API_KEY");
    if (!apiKey) throw new Error("RESPEECHER_API_KEY not set");

    const supabase = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
    );

    const BUCKET = "voiceover";
    const { data: buckets } = await supabase.storage.listBuckets();
    if (!buckets?.find((b) => b.name === BUCKET)) {
      await supabase.storage.createBucket(BUCKET, { public: true });
    }

    const results: { id: number; url: string; bytes: number }[] = [];
    for (const line of LINES) {
      console.log(`tts vo-${line.id}: "${line.text}"`);
      const wav = await ttsToWav(apiKey, line.text);
      const path = `vo-${line.id}.wav`;
      const { error: upErr } = await supabase.storage.from(BUCKET).upload(path, wav, {
        contentType: "audio/wav",
        upsert: true,
      });
      if (upErr) throw new Error(`upload ${path}: ${upErr.message}`);
      const { data: pub } = supabase.storage.from(BUCKET).getPublicUrl(path);
      results.push({ id: line.id, url: pub.publicUrl, bytes: wav.length });
    }

    return new Response(
      JSON.stringify({
        ok: true,
        voice: { id: VOICE_ID, name: "Олеся: медіа" },
        files: results,
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } },
    );
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error(msg);
    return new Response(JSON.stringify({ ok: false, error: msg }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
