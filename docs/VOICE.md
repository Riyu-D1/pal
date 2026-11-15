# Voice — TTS voice notes both ways + Discord "calls"

pal can speak now. Three pieces:

1. **Voice notes out** — chat replies (and job summaries) can include a
   native Discord voice message when the owner turns them on.
2. **Voice notes in** — an audio attachment the owner sends in DM is
   transcribed (Whisper) and answered as a normal chat turn: text reply
   plus a voice note of the reply.
3. **"Calls"** — bots can't ring a DM, so `call_owner(message, urgent)`
   works around it: pal @mentions the owner, DMs a join link, joins a
   private guild voice channel, waits ~2 min for the owner, speaks the
   alert, then holds a short two-way conversation when receive is
   available (see *Receive/DAVE status* below).

## Requirements on the host

```sh
sudo apt install ffmpeg libopus0        # audio decode/encode to OGG/Opus
# calls only — voice notes need nothing else:
.venv/bin/pip install -r requirements-voice.txt
```

Base `requirements.txt` stays untouched; the voice extras are optional
and only the call path imports them.

## TTS provider chain

`synthesize()` walks `PA_TTS_ORDER` (default
`gemini,fish,azure,cloudflare`), skipping providers with no key and
cooling down ones that error or rate-limit (same shape as pa.router).
Whatever comes back — mp3, wav, raw PCM — is normalised through ffmpeg
to **OGG/Opus 48 kHz mono** (Discord's voice-message shape), with the
decoded PCM reused for `duration_secs` and the waveform preview.

| provider | env needed | notes |
|---|---|---|
| `gemini` | `GEMINI_API_KEY` (already used by the router) | `PA_TTS_GEMINI_MODEL` default `gemini-3.8-flash-tts`, `PA_TTS_GEMINI_VOICE` default `Kore`. Returns 24 kHz raw PCM — ffmpeg converts. Model names change; override via env if yours differs. |
| `fish` | `FISH_API_KEY` | `PA_TTS_FISH_MODEL` default `s2.1-pro-free`; optional `PA_TTS_FISH_VOICE` reference-id for a cloned voice. |
| `azure` | `AZURE_SPEECH_KEY` + `AZURE_SPEECH_REGION` | F0 free tier; `PA_TTS_AZURE_VOICE` default `en-GB-SoniaNeural`, `PA_TTS_AZURE_FORMAT` default `audio-24khz-96kbitrate-mono-mp3`. |
| `cloudflare` | `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` | `@cf/deepgram/aura-2-en`; `PA_TTS_CF_SPEAKER` default `luna`. Last resort — most mechanical voice. |

Quick check (reports which provider answered + duration, writes the
ogg to a temp path — never prints keys):

```sh
.venv/bin/python -m pa tts-check "hello there, this is pal"
```

## Voice notes out

- Tool: `send_voice_note(text)` — pal can choose to speak any reply.
- Default replies: `/voice on|off` (persisted in `pa_settings`; falls
  back to `PA_VOICE_NOTES`). When on, every chat reply and job summary
  also goes out as a voice note.
- Wire format: message flag `IS_VOICE_MESSAGE` (1<<13), one `audio/ogg`
  attachment carrying `duration_secs` + base64 `waveform` — a native
  playable voice message, not a file attachment.

## Voice notes in

Any `audio/*` attachment (or `.ogg/.mp3/.wav/.m4a/.opus/.flac`) the
owner sends in DM is downloaded from the Discord CDN and transcribed:

1. Groq `whisper-large-v3-turbo` (`GROQ_API_KEY`,
   `GROQ_WHISPER_BASE`, `PA_WHISPER_MODEL`)
2. Cloudflare `@cf/openai/whisper-large-v3-turbo` (fallback;
   `PA_CF_WHISPER_MODEL`)

The transcript becomes the user message in normal chat history (so the
turn behaves exactly like a typed one); the reply is posted as
`🎤 <transcript>` + text **plus** a voice note of the reply. Owner
DM only — guild audio attachments are ignored.

## Calls (`call_owner`, `/call test`)

Setup:

1. **A guild** — any server the owner controls (a private "pal" server
   is fine). Invite pal there; the bot needs **Connect** + **Speak**
   on the call channel, and **Manage Channels** on the guild if
   `PA_CALL_CHANNEL_ID` is unset (so it can create the private
   `pal-call` channel — visible only to the owner and the bot).
   Invite URL shape (fill in the app id):
   `https://discord.com/oauth2/authorize?client_id=<APP_ID>&permissions=3149824&integration_type=0&scope=bot+applications.commands`
   (`3149824` = Connect + Speak + Manage Channels.)
2. **Intents** — the call client uses `guilds` + `guild_voice_states`
   (privileged — tick "Server Members Intent"/"Voice States" in the
   dev portal if required; voice state events flow through regardless,
   but the toggle is safest).
3. **Env** — `PA_CALL_GUILD_ID` is required. `PA_CALL_CHANNEL_ID`
   optional; unset → find-or-create a private `pal-call` voice channel.
   `PA_CALL_MAX_PER_HOUR` (3), `PA_CALL_JOIN_SECONDS` (120),
   `PA_CALL_LISTEN_SECONDS` (30), `PA_CALL_MAX_SECONDS` (600),
   `PA_CALL_VAD` (`auto` | `webrtc` | `energy` — see below),
   `PA_CALL_TTS_ORDER` (`cloudflare,azure,fish,gemini` — see below),
   `PA_CALL_BARGE_IN` (`on` | `off`),
   `PA_VOICE_CACHE_DIR` (default `<PA_DATA_DIR>/voice-cache`),
   `PA_QUIET_HOURS` (`23-07`, `PA_TIMEZONE` local — only `urgent`
   calls ring inside).
4. **Voice extras** — `pip install -r requirements-voice.txt` (now
   includes `webrtcvad-wheels` for turn-taking).

Flow: `@owner` mention + DM (`📞 pal is calling — join <#channel>`) →
join channel → wait `PA_CALL_JOIN_SECONDS` for the owner (voice-state
events) → speak the alert → the **conversation loop** in `pa/callconv.py`
takes over until the owner leaves, says bye, or `PA_CALL_MAX_SECONDS`
hits. A short summary (turns + transcripts) lands in the owner DM.

### The conversation loop — what makes it feel like a call

Everything below lives in `pa/callconv.py` (Discord-free, fake-tested);
`pa/call.py` wires it to the voice client.

- **Turn-taking / endpointing.** A pump feeds the sink's 20 ms frames
  into an `Endpointer`. VAD is `webrtcvad` when installed
  (`PA_CALL_VAD=auto` default; ~50 KB C extension, GMM — discriminates
  steady fan/keyboard/TV noise that trips a raw energy gate; trivially
  real-time on a 1-4 core VM, unlike silero/onnx at ~100 MB of deps).
  `energy` forces the adaptive energy gate, `webrtc` forces WebRTC
  (warns + falls back when the wheel is missing). An utterance ends
  after ~650 ms of silence (850 ms if the burst was short — probably
  mid-thought), needs ≥300 ms of voiced audio to count, prebuffers
  ~400 ms so onsets aren't clipped, and is capped at 45 s.
- **Noise / hallucination filter.** Groq Whisper runs `verbose_json`;
  transcripts are dropped when empty, a known hallucination
  ("Thank you.", "you", "music", bare "hmm/ok/yeah"), `no_speech_prob`
  ≥ 0.6, or `avg_logprob` ≤ -1.0. Coughs and background chatter never
  reach the chat engine.
- **Barge-in.** While pal speaks, sustained owner speech stops
  playback immediately (`vc.stop()`) and dumps the remaining reply
  chunks — nothing is queued behind it. The trigger is deliberately
  hard to hit so noise can't chop playback into stutter: it needs
  ≥500 ms of VAD-voiced audio inside a 700 ms sliding window, each
  voiced frame louder than an adaptive noise floor (EMA of quiet
  frames), and it can never fire during the first ~400 ms of a clip
  (connect noise / speaker echo). `PA_CALL_BARGE_IN=off` disables it.
  While pal is thinking, a new utterance cancels the in-flight turn:
  one "current turn" with a generation counter; stale results are
  discarded; the old and new audio are merged (continuation) and
  answered once. Every barge-in and every playback stop is logged at
  INFO with its reason.
- **Fast ack.** ~0.6-0.9 s after end-of-speech a cached filler plays
  ("Mm-hm", "One sec", "Let me check", varied — never repeated
  back-to-back). The filler bank is warmed once when the call starts
  **through a persistent disk cache** (`PA_VOICE_CACHE_DIR`,
  `<provider>-<voice>-<sha(text)>.ogg`) — each clip is synthesised
  once ever and reused across calls, so a free-tier daily quota
  survives more than a couple of calls. Turns running >4 s get a
  "still pulling that up…" line at most every 8 s.
- **Pipelined replies.** The reply is split at sentence boundaries
  (markdown/URLs/code stripped) into ≤240-char chunks; chunk *n*+1
  synthesises while chunk *n* plays, so first audio lands after one
  synthesis instead of the whole reply. Call-mode chat turns get a
  spoken-style system hint: 1-2 short sentences, no lists/links/
  markdown/emoji, follow-up questions when useful — the model does
  the "is it already answered / should I keep going" judgement.
- **Wrap-up / idle.** "thanks" / "that's it" / anything ending in
  "bye" ends the call politely ("say bye to sam" and "thanks — one
  more thing" do **not**). ~20 s of silence earns one "anything
  else?" nudge (or "still there?" if nothing was said yet), then
  ~15 s more ends it.
- **One voice per call.** In-call TTS walks `PA_CALL_TTS_ORDER`
  (default `cloudflare,azure,fish,gemini` — Cloudflare's daily neuron
  budget and low latency suit a realtime call; voice notes keep
  `PA_TTS_ORDER`). The first usable provider is locked for the whole
  call — fillers and replies can't mix voices mid-call — and only a
  hard failure advances the lock, permanently.
- **Degradation.** If `say()` fails a chunk is skipped, not the call;
  the next provider in the call order takes over (and keeps it).
  DAVE/recv failures keep the deaf-DM + `call-diag` path (below).
- **Logs.** One line per turn:
  `call turn: heard='…' ack=620ms first_audio=1340ms interrupted=False`
  — including dropped utterances
  (`dropped=noise|stt_empty|short`) so silence is always explainable;
  `call barge-in:` reports voiced ms and rms vs the noise floor, and
  every `call playback stop:` carries its reason.

Owner command: `/call test` (or `call test`) — a full end-to-end ring.

### Live test script (owner)

After deploying, `/call test` and walk this list:

1. **Basic turn** — ask "do you have access to my email?": an ack
   ("mm-hm"/"one sec") should land ~1 s after you stop talking, then a
   short spoken answer with a follow-up question.
2. **Barge-in** — while pal is mid-answer, start talking over it for
   ~1 s. Playback should stop instantly; pal answers the *new*
   question, not the queued one (nothing answers stale text later).
3. **Interrupt while thinking** — ask something, then before the reply
   starts, add "actually — also check X". One merged answer, not two.
4. **Noise** — cough, tap the desk, rustle. Nothing should happen.
5. **Short answer already done** — if pal answers "yes", it should not
   ramble a second paragraph; follow-up question at most.
6. **Wrap-up** — say "thanks, that's it" then "bye": pal signs off and
   leaves. (Also try "say bye to sam" mid-call — it should NOT hang up.)
7. **Idle** — stay silent ~20 s: one "anything else?" then pal ends the
   call after another ~15 s.
8. **Logs** — one `call turn:` line per utterance; if pal reports it
   can't hear, `python -m pa call-diag 20` while speaking.

## Receive/DAVE status — read this

Since March 2026 Discord **requires DAVE end-to-end encryption** on all
voice connections. The voice gateway/UDP/Opus/DAVE layer is *not*
hand-rolled: `pa/call.py` drives **discord.py ≥ 2.7 + `davey`** (the
maintained DAVE binding) on its own asyncio loop in a daemon thread —
a second gateway session under the same bot token, which Discord
allows. Speaking works out of that box.

**Receiving** owner audio is the fragile half, and it needs **two**
private-API shims in `pa/call.py` (community approach, used in the
wild — both wrapped so any upstream change demotes calls to one-way
(alert spoken, owner replies with a voice note or DM) rather than
crashing; degradation is reported in the call summary):

1. `discord-ext-voice-recv` supplies the RTP→PCM path but predates
   DAVE — inbound packets arrive still-encrypted. The shim wraps
   `PacketDecoder._decode_packet` to route each payload through
   discord.py's `dave_session.decrypt()` before the Opus decoder sees
   it, substituting Opus silence until a per-user decryptor exists
   (MLS commits lag a speaker's first frames — drops retry every
   packet and self-heal; warnings are rate-limited).
2. **The part that actually broke two-way audio:** discord.py 2.7.1
   processes the MLS gateway traffic (proposals/commits/welcomes) but
   only keeps bookkeeping — its own comment admits "the session
   should be signaled too". In `davey`, per-user decryptors are built
   *only* inside `prepare_transition()`/`execute_transition()`, and
   `session.ready` stays false without them — so every `decrypt()`
   raised `NoDecryptorForUser` forever (and outbound wasn't
   DAVE-encrypted either). The shim also wraps
   `DiscordVoiceWebSocket.received_message`/`received_binary_message`
   to feed ops 21/22/29/30 into the session, and stashes MLS binary
   frames that arrive before the session exists (discord.py drops
   those silently; the server never resends).

If packets flow but nothing ever decrypts, pal now *says* it can't
hear the owner and DMs them the fallback instead of waiting silently.

### `python -m pa call-diag`

Joins the call channel for N seconds and logs the full receive state
every 2 s — the quick check for "pal can't hear me" on the host:

```
PA_CALL_CHANNEL_ID=<vc id> python -m pa call-diag 20
# prints each tick + a final JSON snapshot:
#   listening / ssrc_map (who's speaking)
#   session: ready, status, epoch, can_encrypt, user_ids
#   missing_decryptors: mapped speakers the session can't decrypt
#   rx counters: ok / pending / not_ready / no_user / fail / last_error
```

Healthy after a `/call test`: `listening: true`, `ready: true`,
`missing_decryptors: []`, and `rx.ok` climbing while the owner talks.
`ok_users` empty with `pending`/`not_ready` growing = the DAVE shim
didn't apply (check versions: discord.py ≥2.7, davey ≥0.2.0,
voice-recv ≥0.5.0a0).

Unit tests mock the voice lib; a live two-way call is verified per
deploy with `/call test`.

## Env recap

```
PA_VOICE_NOTES=false                  # default for /voice
PA_TTS_ORDER=gemini,fish,azure,cloudflare
PA_TTS_GEMINI_MODEL=gemini-3.8-flash-tts   PA_TTS_GEMINI_VOICE=Kore
FISH_API_KEY=      PA_TTS_FISH_MODEL=s2.1-pro-free   PA_TTS_FISH_VOICE=
AZURE_SPEECH_KEY=  AZURE_SPEECH_REGION=  PA_TTS_AZURE_VOICE=en-GB-SoniaNeural
PA_TTS_AZURE_FORMAT=audio-24khz-96kbitrate-mono-mp3
PA_TTS_CF_SPEAKER=luna   CLOUDFLARE_AI_BASE=https://api.cloudflare.com/client/v4
GROQ_WHISPER_BASE=https://api.groq.com/openai/v1
PA_WHISPER_MODEL=whisper-large-v3-turbo
PA_CF_WHISPER_MODEL=@cf/openai/whisper-large-v3-turbo
PA_FFMPEG=ffmpeg
PA_CALL_GUILD_ID=   PA_CALL_CHANNEL_ID=
PA_CALL_MAX_PER_HOUR=3  PA_CALL_JOIN_SECONDS=120
PA_CALL_LISTEN_SECONDS=30  PA_CALL_MAX_SECONDS=600  PA_CALL_VAD=auto
PA_CALL_TTS_ORDER=cloudflare,azure,fish,gemini
PA_CALL_BARGE_IN=on
PA_VOICE_CACHE_DIR=            # default <PA_DATA_DIR>/voice-cache
PA_QUIET_HOURS=23-07
```

## Deploy on the VM (systemd user service `pa`)

```sh
sudo apt install -y ffmpeg libopus0        # on the host running pal
cd <repo> && git pull
.venv/bin/pip install -r requirements-voice.txt
# add the env vars above to your service env file (never commit, never echo)
systemctl --user restart pa
# verify:
.venv/bin/python -m pa tts-check "voice check one two"
.venv/bin/python -m pa call-diag 20   # during/after a call attempt
# in Discord:  /voice on   then   /call test — say something; you
# should hear pal transcribe+answer. If pal says it can't hear you,
# run call-diag while speaking and check rx.ok climbs.
```
## Voice health + usage

`/status` reports each TTS/STT provider — configured, missing key, or
cooling down — plus today's per-provider counts from the `voice_usage`
table (`tts:<provider>`, `stt:<provider>`). When every configured TTS
provider is cooling at once the owner gets one deduped DM alert.

## Call latency work

Pipeline calls no longer run the full chat agent per turn: a streamed
call brain (router `call` tier, ~600-token prompt, no tool catalogue)
starts TTS on the first ~80-char clause while the model still
generates; `[[ASK_PAL: q]]` escapes to the full engine in background
(8 s, then "I'll DM you that"). `PA_CALL_ENGINE=auto` prefers Gemini
Live (server VAD, fast read-only tools, resumption reconnect) and
falls back to the pipeline. Playback is one in-process PCM queue —
zero ffmpeg at play time. DAVE receive strips RTP padding before
decrypt and passes Opus silence through; fillers are capped at 2 per
turn. Call TTS locks one provider per call in azure → cloudflare →
gemini order with per-provider daily char budgets (Gemini TTS stays
for voice notes). `/call stats` shows last-call + 7-day p50/p90;
`python -m pa call-bench` benchmarks both engines. See also docs/FEATURES.md.
