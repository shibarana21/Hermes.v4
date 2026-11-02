# J.A.R.V.I.S. — Hermes Chat UI v4

Hermes Agent ke liye J.A.R.V.I.S. style HUD chat interface. Isme 2 hisse hain:

| File | Kaam |
|---|---|
| `index.html`, `sw.js`, `manifest.json`, `icon.svg` | UI (PWA, offline-installable) |
| `jarvis_core.py` | **CORE**: PC par chalne wala companion server. UI serve karta hai, Hermes ka proxy hai (API key PC par hi rehti hai), audit log aur approval rules sambhalta hai, live system data deta hai, aur offline Hindi voice (Whisper STT + Piper TTS) aur wake word chalata hai |
| `capacitor/` | Android app (APK) banane ka setup |

---

## 1. Hermes taiyar karo

`~/.hermes/.env` me:

```env
API_SERVER_ENABLED=true
API_SERVER_KEY=apni-lambi-secret-key
# sirf direct mode ke liye (CORE use kar rahe ho to zaroori nahi):
API_SERVER_CORS_ORIGINS=http://localhost:3000
```

Phir `hermes gateway` chalao. Hermes `http://127.0.0.1:8642/v1` par chalega.

Docs: [Hermes API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)

## 2. CORE chalao (recommended)

```bash
pip install aiohttp psutil                      # minimum
pip install faster-whisper piper-tts numpy      # offline Hindi voice (optional)
python -m piper.download_voices hi_IN-pratham-medium --data-dir ./voices
#   aur voices: hi_IN-priyamvada-medium, hi_IN-rohan-medium

python jarvis_core.py
```

Browser me **http://localhost:8800** kholo. CORE apne aap `~/.hermes/.env` se `API_SERVER_KEY` padh leta hai, isliye browser me key daalne ki zaroorat nahi. Agar key kahin aur hai to `HERMES_API_KEY=... python jarvis_core.py` chalao.

### CORE flags

| Flag | Matlab |
|---|---|
| `--port 8800` | UI/CORE ka port |
| `--hermes http://127.0.0.1:8642` | Hermes ka address |
| `--host 0.0.0.0` | Same Wi-Fi par phone se kholne ke liye |
| `--token MYSECRET` | Har request par token zaroori (`--host 0.0.0.0` ke saath zaroor lagao). UI me Settings → CORE TOKEN me daalo |
| `--https` | Self-signed HTTPS. Phone ke **browser** me mic sirf https par chalta hai |
| `--cors http://origin` | Kisi aur origin ko CORE use karne do (repeatable, `*` = sab). `http://localhost` (Android app) default allowed hai |
| `--whisper small` | STT model: `tiny / base / small / medium / large-v3`. Hindi ke liye `small` theek hai, `medium` zyada sahi par dheema |
| `--voice-dir ./voices` `--voice hi_IN-pratham-medium` | Piper voice |
| `--no-voice` | STT/TTS band (kam RAM) |
| `--wake` | PC ke mic par "hey jarvis" sunna. Iske liye `pip install openwakeword sounddevice` chahiye |
| `--wake-model hey_jarvis` `--wake-threshold 0.5` | Wake word model aur sensitivity |
| `--file-root ~/projects` | FILES tab me sirf is folder ki files read-only dikhengi (repeatable) |
| `--data-dir ~/.jarvis-core` | `audit.jsonl`, `rules.json`, TLS cert yahan save hote hain |

Phone ke browser ke liye: `python jarvis_core.py --host 0.0.0.0 --https --token MYSECRET`, phir phone me `https://PC-IP:8800` kholo. Certificate warning aaye to use accept kar lo.

## 3. Direct mode (bina CORE)

```bash
python -m http.server 3000      # is folder me
```

http://localhost:3000 kholo, phir Settings → CONNECTION = Direct karo. ENDPOINT me `http://localhost:8642/v1` aur API KEY daalo. Key ko **LOCK KEY WITH PIN** se encrypt kar sakte ho (PBKDF2 + AES-GCM). Encrypt karne ke baad key sirf memory me unlock hoti hai.
Is mode me STT/TTS browser wala hota hai, aur SYSTEM, AUDIT aur RULES tabs nahi chalte.

## 4. Android app (Capacitor)

Iske liye Node 20+, Android Studio aur JDK 17+ chahiye.

```bash
cd capacitor
npm install
npm run add        # www/ banata hai + android/ project + mic permission patch
npm run apk        # android/app/build/outputs/apk/debug/app-debug.apk
# ya: npm run open  -> Android Studio me Run
```

App kholo, phir Settings me:
- **CORE URL** = `http://PC-IP:8800` (PC par `python jarvis_core.py --host 0.0.0.0 --token MYSECRET`)
- **CORE TOKEN** = `MYSECRET`

App `http://localhost` se chalta hai, isliye phone ka mic aur CORE ka offline STT dono chalte hain (https ki zaroorat nahi).
UI update karne ke baad `npm run sync` chalao.

---

## Features (v4)

- **Server sessions**: Hermes DB ke sessions (CLI, Telegram aur dusre platforms ke bhi) sidebar me dikhte hain. Fork karo, rename karo (double-click / F2), delete karo, aur SERVER/LOCAL ke beech switch karo
- **Live tool timeline**: thinking, tool start/complete/fail aur har tool ka time
- **Approval cards**: ALLOW ONCE / SESSION / ALWAYS / DENY, saath me **server-side rules** (auto-deny/auto-allow regex, RULES tab)
- **Audit log**: har tool call aur approval ki JSONL file, AUDIT tab me filter aur export
- **Ops dock** (Ctrl+J): SYSTEM (CPU/RAM/disk gauges, graphs, processes), SKILLS, JOBS (cron: run/pause/delete/new), FILES (files touched + diff viewer), AUDIT, RULES
- **Model picker**: provider|model chuno, saath me per-session model lock
- **Profiles**: `/profile name` se dusre Hermes profile par jao
- **Markdown++**: diff coloring, code blocks par RUN (Hermes approval ke saath chalata hai) / SAVE / COPY, aur numeric tables se **auto bar-chart**
- **Offline Hindi voice**: Whisper STT, Piper TTS (agla sentence pehle se tayyar hota hai), VAD auto-stop, aur wake word "hey jarvis"
- **Voice modes**: TAP / CONVO (jawab ke baad mic khud khulta hai) / WAKE
- **Arc reactor** animation, boot sequence, themes, desktop notifications
- **Security**: key PC par rehti hai (CORE), PIN-encrypted key (direct mode), core token, aur file-root sandbox
- **PWA**: install karke offline UI shell chalta hai

## Keyboard shortcuts

| Key | Kaam |
|---|---|
| Enter / Shift+Enter | Bhejo / nayi line |
| Ctrl+K | Nayi session |
| Ctrl+M | Mic |
| Ctrl+J | Ops dock |
| Ctrl+B | Sidebar |
| Ctrl+, | Settings |
| Ctrl+Shift+V | Voice reply on/off |
| Esc | Chal rahe run ko rokna / mic band |
| ↑ (khaali input) | Pichhla message edit |

## Slash commands

`/new` `/clear` `/fork` `/store server|local` `/profile <name>` `/model <name>` `/dock` `/system` `/skills` `/jobs` `/files` `/audit` `/rules` `/chart` `/notify` `/engine auto|core|browser` `/voice` `/mode` `/theme` `/sys <text>` `/export` `/json` `/retry` `/ping` `/config` `/help`

## Limits (sach bata raha hoon)

- APK yahan build nahi hua (Android SDK nahi tha). Setup ready hai, build tumhare PC par hoga
- Wake word sirf us PC ke mic par chalta hai jahan CORE chal raha hai
- Diff tabhi dikhega jab tool output ya jawab me unified diff ho
- Whisper `small` CPU par ek chhote vakya me lagbhag 4 sec leta hai, aur kabhi-kabhi spelling galat karta hai ("बोस", "सिस्तम"). Behtar accuracy ke liye `--whisper medium` lagao
- Arc reactor canvas par bana pseudo-3D hai, asli WebGL nahi
