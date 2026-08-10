# xgroxy

**Turn your X Premium+ / SuperGrok subscription into a local OpenAI-compatible API.**

One file. Zero dependencies. No npm. No Docker. No build step. Just Python.

If you pay for Grok through X Premium+ or SuperGrok, you already have access to
Grok 4.5 — but only through the web app or the `grok` CLI. `xgroxy` unlocks that
same subscription as a plain OpenAI-compatible HTTP endpoint, so any tool that
speaks OpenAI (OpenCode, Claude Code, Cursor, Continue, LangChain, curl...) can
use Grok for free, no API credits required.

```text
your OpenAI-compatible tool
        │  POST /v1/chat/completions  (OpenAI format)
        ▼
   xgroxy  (localhost:8788)          ◄── one Python file
        │  Bearer <your X OAuth token>
        ▼
   api.x.ai/v1/chat/completions      ◄── powered by your X Premium+ sub
```

## What you get

| Feature | Details |
|---------|---------|
| OpenAI-compatible API | `POST /v1/chat/completions`, `GET /v1/models`, `GET /health` |
| Streaming | SSE, token-by-token, with thinking (`reasoning_content`) |
| Tool calling | `tools` / `tool_calls` pass through — coding agents run tools locally |
| Zero-dependency login | OAuth device flow — no `grok` binary needed |
| Auto token refresh | Access tokens die after ~6 h; `xgroxy` refreshes them for you |
| Security | Token stored `0600`, optional API key on the local endpoint |

## Requirements

- **Python 3.8+** (standard library only — that's it)
- An **X Premium+ or SuperGrok** subscription (the same one that unlocks the `grok` CLI)

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/smhanov/xgroxy/main/xgroxy -o xgroxy
chmod +x xgroxy
```

*(No pip, no npm, no lockfile. The whole program is one file you can read.)*

## Setup with any coding agent

Don't want to type commands? Copy this prompt into your coding agent
(Claude Code, OpenCode, Codex, Cursor, Continue, ...) and let it set
everything up on this machine:

```text
Set up xgroxy on this machine:

1. Install the single-file program (Python 3.8+ is the only requirement):
   curl -fsSL https://raw.githubusercontent.com/smhanov/xgroxy/main/xgroxy -o ~/bin/xgroxy
   chmod +x ~/bin/xgroxy
   (create ~/bin and add it to PATH if needed)

2. Run `xgroxy login`. It prints a URL and a one-time code. Show them to
   me and WAIT while I authorize in my browser — do not continue until I
   confirm. If I already have a ~/.grok/auth.json (grok CLI), skip this
   step entirely.

3. Start the server: `xgroxy serve` (background it or leave a terminal).

4. Verify it works, then report the result:
   curl -s http://127.0.0.1:8788/health        # expect {"status":"ok",...}
   curl -s http://127.0.0.1:8788/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model":"grok-4.5","messages":[{"role":"user","content":"Reply with exactly: OK"}]}'

Notes: requires an X Premium+ or SuperGrok subscription. Never print or
commit the token. If login is needed, always pause and ask me to authorize.
```

## Quick start

### 1. Sign in

```bash
./xgroxy login
```

This starts the OAuth **device flow** — it prints a link and a code:

```
 1. Open this URL in your browser (must be logged into your X account):
    https://accounts.x.ai/oauth2/device?user_code=VRH2-NKD2

 2. Enter code:  VRH2-NKD2
    Waiting for authorization...
```

Approve it in the browser and you're signed in. Your token is stored in
`~/.xgroxy/auth.json` (`chmod 600`). Already use the `grok` CLI?
`xgroxy` will happily reuse your existing `~/.grok/auth.json` — skip to step 2.

### 2. Start the server

```bash
./xgroxy serve
```

```
xgroxy 0.1.0 — Grok 4.5 on http://127.0.0.1:8788
signed in as you@example.com · token expires in 5h 52m
```

### 3. Use it

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-4.5","messages":[{"role":"user","content":"Say hello in one line"}]}'
```

Or with the OpenAI SDK:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8788/v1", api_key="xgroxy")
r = client.chat.completions.create(model="grok-4.5", messages=[{"role":"user","content":"hi"}])
print(r.choices[0].message.content)
```

Or with a coding agent (tools run on *your* machine):

```bash
# OpenCode
opencode run "list the files here" --model xai/grok-4.5   # baseURL → http://127.0.0.1:8788/v1
# Claude Code
claude --model grok-4.5                                    # ANTHROPIC_BASE_URL → http://127.0.0.1:8788/v1
```

## How the magic works

Your X Premium+ subscription includes OAuth tokens that work directly against
xAI's API — no API credits, no `x.ai` billing. `xgroxy` is the plumbing:

1. **`login`** runs xAI's OAuth *device flow* (the same one the `grok` CLI uses):
   request a device code → you authorize in the browser → `xgroxy` polls and saves
   the token. No client secrets, no redirect URLs, nothing to configure.
2. **`serve`** is a tiny HTTP server. Every request gets an access token
   (`Authorization: Bearer`), forwards the OpenAI body to `api.x.ai`, and streams
   the response back.
3. **Token refresh** is automatic: access tokens expire after ~6 hours, so
   `xgroxy` refreshes them using the rotating refresh token — proactively when
   they're close to expiring, and reactively if the API ever returns 401.

## Full CLI

```
xgroxy login [--auth-file PATH]     sign in (OAuth device flow)
xgroxy serve [--host H] [--port P]  start the API server (default 127.0.0.1:8788)
            [--api-key KEY]         require this Bearer key on local requests
            [--auth-file PATH]      use a specific token file
            [--model M]             default model (default grok-4.5)
xgroxy status [--auth-file PATH]    show who you're signed in as + token expiry
xgroxy token [--auth-file PATH]     print the raw access token (debugging)
xgroxy --version
```

Environment variable: `XGROXY_AUTH_FILE` (same as `--auth-file`).
`XGROXY_API_KEY` is read too (same as `--api-key`).

## Wiring it into your tools

### OpenCode

```json
// ~/.config/opencode/opencode.json
{
  "provider": {
    "xai": { "options": { "baseURL": "http://127.0.0.1:8788/v1", "apiKey": "xgroxy" } }
  }
}
```

```bash
opencode run "what's in this repo?" --model xai/grok-4.5
```

### Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8788/v1
export ANTHROPIC_AUTH_TOKEN=xgroxy
export ANTHROPIC_MODEL=grok-4.5
claude
```

### Anything else

Any OpenAI-compatible client: set `base_url` (or `OPENAI_BASE_URL`) to
`http://127.0.0.1:8788/v1`, model `grok-4.5`, any API key.

## Security notes

- By default the server binds **127.0.0.1 only** — nothing else on your network
  can reach it.
- If you bind `--host 0.0.0.0` (LAN, Tailscale, a VM), **set `--api-key`** so
  strangers can't burn your subscription:
  `./xgroxy serve --host 0.0.0.0 --api-key sk-something-long`
- Your token file is written with `0600` permissions. Never commit it, never
  paste it anywhere. `xgroxy` never logs or echoes tokens.
- This is an unofficial project. It uses the same OAuth flow and API surface as
  the official `grok` CLI — nothing is bypassed, cracked, or scraped.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `401 ... token expired` | Run `./xgroxy login` again (or check `./xgroxy status`) |
| `No auth file found` | You haven't logged in: `./xgroxy login` |
| `address already in use` | Another server is on 8788: `./xgroxy serve --port 8790` |
| Browser says *code invalid/expired* | Device codes last 30 min — just run `login` again |
| Model name errors | Some X plans expose only `grok-4.5`; try `--model grok-4.5` |
| It works, then stops after hours | Access token expired — that's normal, refresh is automatic. If it *keeps* failing, your X session was revoked; re-run `login`. |

## FAQ

**Do I need API credits at x.ai?** No. This rides your subscription's OAuth, same
as the `grok` CLI.

**Does it run tools on my machine?** No. `xgroxy` is pure HTTP plumbing — it
never executes anything. Tool *definitions* pass through; a coding agent on the
other side decides what to run, locally.

**Will I get banned?** It's the same OAuth flow the official CLI uses. Standard
fair-use rules apply — don't hammer it with thousands of parallel requests.

**Can two machines use it?** Yes — run `serve` on one machine (e.g. a home
server) and point clients at it over Tailscale/LAN, with `--api-key` set.

**Windows?** It's stdlib-only Python — `python xgroxy serve` works. The `curl`
one-liner install is Unix; on Windows download the file and run it with Python.

## License

MIT
