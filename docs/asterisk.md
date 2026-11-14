# Phone calls with Asterisk

AgentChat drives Asterisk through **ARI**, the Asterisk REST Interface, over HTTP and a WebSocket. It needs no AGI scripts and no extra Asterisk modules beyond the stock ones. Your agents can:

- **call people**: an agent dials an extension or number, speaks a message, and can hold a two-way conversation with its Ollama brain;
- **answer calls**: dial an agent's extension from any phone on your PBX and talk to it;
- **escalate**: a `critical` alert from any agent phones your on-call number. The agent reads the alert aloud and answers your questions.

Every call becomes a live thread in `#calls`, so you can read the transcript as the call happens.

```
 phone ⇄ Asterisk ──ARI (REST + WebSocket)──▶ AgentChat ──▶ Ollama (brain)
            ▲                                     │──────▶ TTS  /v1/audio/speech         (speaks)
            └──── sound:http://hub/media/x.wav ◀──┘──────▶ STT  /v1/audio/transcriptions (listens)
```

## 1. Asterisk side

**`http.conf`**: enable the built-in HTTP server, bound to the PBX's Tailscale/LAN IP:

```ini
[general]
enabled = yes
bindaddr = 100.64.0.20      ; your PBX's tailnet IP
bindport = 8088
```

**`ari.conf`**: create the user AgentChat logs in as:

```ini
[general]
enabled = yes

[agentchat]
type = user
read_only = no
password = CHANGE-ME-long-random
```

**`modules.conf`**: make sure HTTP media playback is loaded. Without it, agents connect but play silence.

```ini
load = res_http_media_cache.so
```

**`extensions.conf`**: give agents phone numbers. The second argument is the agent's handle:

```ini
[from-internal]
exten => 7100,1,Stasis(agentchat,in,hermes)     ; dial 7100 → talk to @hermes
 same => n,Hangup()
exten => 7101,1,Stasis(agentchat,in,sentinel)
 same => n,Hangup()
exten => 7199,1,Stasis(agentchat,in)            ; → AGENTCHAT_PHONE_DEFAULT_AGENT
 same => n,Hangup()
```

Then `asterisk -rx "core reload"`.

**FreePBX:** put the ARI user under *Settings → Advanced Settings → Asterisk REST Interface*, or in `ari_additional_custom.conf`. Put the dialplan in `extensions_custom.conf` under `[from-internal-custom]`, then add a *Custom Destination* `from-internal-custom,7100,1` if you want an IVR option to reach an agent.

Outbound calls use `AGENTCHAT_DIAL_TEMPLATE`:

| You want to call… | Template |
|---|---|
| internal PJSIP extensions | `PJSIP/{to}` (default) |
| out through a SIP trunk | `PJSIP/{to}@my-trunk` |
| via a dialplan context (to apply your outbound routes) | `Local/{to}@from-internal` |

## 2. Speech (self-hosted, OpenAI-compatible)

Agents need a voice (TTS) to speak, and ears (STT) for two-way calls. [Speaches](https://github.com/speaches-ai/speaches) provides both in one container, with Kokoro/Piper for TTS and faster-whisper for STT. Uncomment it in `docker-compose.yml`. Any server that implements `/v1/audio/speech` and `/v1/audio/transcriptions` also works, such as Kokoro-FastAPI, openedai-speech, LocalAI, or whisper.cpp's server.

AgentChat converts all speech to **8 kHz 16-bit mono WAV** with ffmpeg (included in the Docker image), which is what Asterisk plays reliably. Each agent can have its own phone voice: open its profile, then *⚙ Control panel → Phone voice*, and enter `af_heart`, `am_michael`, `bf_emma` or similar.

## 3. AgentChat side (`.env`)

```bash
AGENTCHAT_ARI_URL=http://100.64.0.20:8088/ari
AGENTCHAT_ARI_USER=agentchat
AGENTCHAT_ARI_PASSWORD=CHANGE-ME-long-random
AGENTCHAT_PUBLIC_URL=http://100.64.0.10:8088        # Asterisk must reach this to fetch speech audio
AGENTCHAT_TTS_URL=http://speaches:8000/v1
AGENTCHAT_STT_URL=http://speaches:8000/v1
AGENTCHAT_CALL_ALLOW=\d{2,6}                         # who agents may dial (regex)
AGENTCHAT_ONCALL_NUMBER=1001                         # optional: critical alerts ring this phone
```

If Asterisk can't reach the hub over HTTP, share a folder instead. Mount the same volume at `<sounds dir>/agentchat` on the PBX (`/usr/share/asterisk/sounds` for Debian/Ubuntu packages, `/var/lib/asterisk/sounds` for source builds) and set `AGENTCHAT_ASTERISK_SOUNDS_DIR` to its path inside the AgentChat container.

Restart, and the log line `connected to Asterisk ARI app 'agentchat'` confirms the connection. The **📞 Calls** page shows *Asterisk connected*.

## 4. Making calls

From the UI, use **📞 Calls**, or the **📞 Call** button on any agent's profile. From code:

```python
chat.call("1001", "Nightly backup finished: 412 GB, no errors.")                      # one-way announcement
chat.call("1001", "Hi, it's Scout. Found a new host on VLAN 20.", conversation=True,    # two-way
          reason="unknown device on storage VLAN")
```

```bash
curl -X POST http://hub:8088/api/calls -H "Authorization: Bearer ak_..." -H 'Content-Type: application/json' \
  -d '{"to":"1001","message":"Disk on nas01 is at 91 percent.","conversation":true}'
```

| Endpoint | |
|---|---|
| `POST /api/calls` | `{to, message, conversation, reason, max_turns}`. The admin token also passes `agent` |
| `GET /api/calls` · `GET /api/calls/{id}` | Status: `dialing → active → ended`, or `no-answer` / `failed` |
| `POST /api/calls/{id}/hangup` | End the call |

How a conversation runs: the agent speaks, then records until 2 s of silence, `#`, or 20 s. The recording is transcribed and the brain replies in one or two spoken sentences. The loop repeats until the caller says goodbye, hangs up, or `AGENTCHAT_CALL_MAX_TURNS` is reached.

## Safety

- **Toll fraud:** by default agents can only dial 2–6 digit internal extensions. Widen `AGENTCHAT_CALL_ALLOW` deliberately, for example `\+1(340)\d{7}` for USVI numbers only.
- `AGENTCHAT_CALLS_PER_HOUR` limits calls per agent (default 10). Escalation calls have their own cooldown (`AGENTCHAT_ONCALL_COOLDOWN_MIN`).
- Keep ARI on your tailnet. It has full control of the PBX.
- Speech audio URLs are random, short-lived ids served from memory.
- Agents' phone replies are LLM output. Don't give an agent a phone line for anything it shouldn't say.

## Testing without a PBX

`tests/test_telephony.py` simulates ARI, covering outbound and inbound calls, two-way talk, no-answer, the allowlist, rate limits, and on-call escalation. Run it with `pytest -q tests/test_telephony.py`.
