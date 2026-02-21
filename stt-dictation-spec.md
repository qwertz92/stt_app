# STT-Dictation für Windows 11 – Requirements & Design Research (SRS + Pflichtenheft light)

Version: 0.3  
Datum: 2026-02-16  
Zielgruppe: Du selbst + ein Coding-Agent (z.B. Codex) für MVP-Implementierung und spätere Produktisierung

> **Note:** This is a legacy bilingual design document. For current user-facing documentation, see [README.md](README.md) and [docs/](docs/).

---

## 0) Begriffsklärung: Lastenheft vs. Pflichtenheft (kurz)

- **Lastenheft** = *Was* soll das System leisten? (Anforderungen aus “Kundensicht”)
- **Pflichtenheft** = *Wie* wird es umgesetzt? (Technische Umsetzung, Architektur, Komponenten, Entscheidungen)

Für dein Vorhaben ist am praktischsten ein kombiniertes Dokument:

- **SRS (Software Requirements Specification)** = sauberes, implementierbares “Was” + Abnahmekriterien  
- **High-Level Design (HLD)** = “Wie” auf hoher Ebene, inkl. Technologie-Optionen, Risiken, Roadmap

Dieses Dokument ist genau so ein Hybrid (SRS + Pflichtenheft-light).

---

## 1) Problem & Vision

Du willst eine Windows-Desktop-Diktier-App, die sich wie Windows Voice Typing verhält (kleines Overlay, Mikrofon automatisch, Ausgabe direkt dort, wo der Cursor steht), aber mit **besserer Erkennungsqualität** und **mehrsprachigem / code-switching** Verhalten. Zusätzlich soll sie flexibel sein:

- lokal (kein Cloud-Upload) **oder**
- via API zu einem Provider mit Top-Modellen (OpenAI, Azure, Deepgram, AssemblyAI, Google, …)
- zwei Modi:
  - **Streaming**: laufende Teil-Transkription (low latency)
  - **Batch**: erst aufnehmen, dann transkribieren und einfügen (besserer Kontext, potentiell genauer)

---

## 2) Machbarkeits-Check (die “kritischen” Punkte)

### 2.1 “Per Tastendruck starten” (global)

**Ja, grundsätzlich möglich**, aber die Details sind entscheidend:

- Windows bietet **RegisterHotKey()** (verlässlich, sauber) – allerdings sind **Win-Taste-Kombinationen systemreserviert** und können (je nach Kombination/Windows-Version) gar nicht registrierbar sein oder kollidieren.  
  Quellen: Windows-Key-Kombos sind system-reserviert (Raymond Chen / Microsoft).

**Konsequenz für dein Ziel “wie Win+H”**:

- **Win+H selbst** ist in Windows für Voice Typing belegt (Microsoft Support). Es ist realistisch **nicht zuverlässig** ersetzbar, ohne Nebenwirkungen/Workarounds.  
  Workarounds wären z.B. Low-Level Keyboard Hook (WH_KEYBOARD_LL) + Event “schlucken”, aber das ist:
  - konfliktanfällig (andere Tools wie Window-Manager/Launcher hooken auch)
  - sicherheitsseitig sensibel (Keylogger-ähnliches Verhalten; AV/EDR-Risiko)

**Empfehlung**: Für MVP eine **konfigurierbare Hotkey-Kombi ohne Win-Taste** (z.B. Ctrl+Alt+Space) + optional später “Win+H remap” via AHK/PowerToys oder mit eigener Hook als “Advanced Feature”.

### 2.2 “Text dort einfügen, wo Cursor ist”

**Ja**, aber es gibt eine große Spannweite an “wie nativ”:

**MVP-robust (in 1–3 Tagen machbar):**

- Text per **Clipboard + Ctrl+V** einfügen (SendInput).  
  Vorteile: funktioniert in sehr vielen Apps, wenig Komplexität.  
  Nachteile:
  - verändert kurzzeitig Clipboard (man kann es zwar sichern & restore, aber es ist trotzdem intrusive)
  - funktioniert nicht in allen Feldern (z.B. Passwort/secure input), und **UIPI** kann Injektion in elevated windows verhindern

**Langfristig “nativ wie ein Input Method”:**

- Implementierung als **TSF Text Service (TIP) / IME-artige Integration** über Text Services Framework.  
  Vorteil: “echtes” Texteinspeisen am Caret, Komposition, Kandidatenfenster etc., sauberer OS-Weg.  
  Nachteile: COM/Win32-Komplexität, deutlich mehr Aufwand, security-review-relevant.

Quellen: TSF-Dokumentation (Microsoft Learn). UIAccess/UAC Policies für UI-Interaktion mit höher privilegierten Fenstern.

### 2.3 “Mehrsprachig / code-switching”

Das ist lösbar – aber die Engine-Wahl entscheidet, wie gut:

- Lokale Whisper-Familie kann *teilweise* code-switching, aber “perfekt multilingual während eines Satzes” ist nicht garantiert.
- Cloud-Provider haben teils explizite Features (z.B. Deepgram “Multilingual Codeswitching”).

---

## 3) Anforderungen (SRS)

### 3.1 MVP (Prototyp) – “Basic Features die wirklich funktionieren”

**MVP-Ziel**: Du kannst in *fast jedem* Textfeld drücken → sprechen → Transkript erscheint am Cursor.

**Funktionale Anforderungen**

1. **Global Hotkey** (konfigurierbar)
   - Default: Ctrl+Alt+Space (oder ähnlich)  
   - Startet/stoppt Aufnahme (“Push-to-talk” optional)
2. **Overlay UI**
   - kleines always-on-top Fenster (Status: Listening / Processing / Error)
   - Anzeige des letzten Transkripts (oder live, wenn später Streaming)
3. **Audio Capture (Mikrofon)**
   - Auto-Select default input device
   - 16 kHz oder 24 kHz mono pipeline (je nach Engine)
4. **Batch-Modus (MVP)**
   - Start: aufnehmen bis Stop oder bis Silence (VAD)
   - Danach: Transkribieren
5. **Text Insertion**
   - Standard: Clipboard-sicherer Paste (Clipboard speichern → setzen → Ctrl+V → restore)
6. **Basic Settings**
   - Engine-Auswahl: “Local” vs “Remote API” (Remote zunächst optional, kann Dummy sein)
   - Sprache: Auto/Deutsch/Englisch (Auto bevorzugt)
7. **Logging (lokal)**
   - Debug-Logdatei + optional UI “copy diagnostics”

**Nichtfunktionale Anforderungen**

- Ziel-Latenz MVP (Batch):
  - Ende der Aufnahme → Text eingefügt: **< 3–6s** bei “kleinem bis mittelgroßem” Modell (abhängig von HW)
- Stabilität: Crash darf keinen Input-Device “locken”, keine endlosen Hooks
- Datenschutz: Audio wird nicht gespeichert (Default), außer Debug-Option “save last WAV”

**Akzeptanzkriterien MVP**

- In Notepad, Word, Browser-Textfeld, Slack/Teams: Hotkey → sprechen → Text erscheint korrekt im Feld.
- Bei Netzwerk-Disconnect (falls Remote): sauberer Fehler ohne Freeze.
- UI reagiert, Stop funktioniert sofort.

### 3.2 “Enhanced / Full” (Roadmap-Features)

**Streaming / Live**

- Live-Partial-Results, die sich laufend verbessern
- VAD/Turn Detection wählbar (client vs server)
- “Commit” der finalen Äußerung

**Multilingual**

- Auto language ID
- Optional “multi” / code-switching Mode (Provider/Engine-abhängig)

**Provider-Plugin-System**

- Einheitliche Transcriber-Schnittstelle (Local/Remote)
- Provider: OpenAI Realtime + Audio API, Azure Speech, Deepgram, AssemblyAI, Google STT

**Text-Insertion Advanced**

- “Insert without clipboard” (wenn möglich via UIA TextPattern) – best effort
- Voll-integriert via TSF TIP/IME (Langfrist-Ziel)

**Usability**

- Toggle “Push-to-talk” vs “toggle mode”
- Hotkey-Konflikt-Erkennung
- “Hold-to-talk” (press = record, release = stop)

**Performance**

- GPU acceleration falls vorhanden
- Quantization profile (int8/float16)

**Security / Enterprise**

- Signierter Installer, auto-update channel
- Optional “no keyboard hooks” Mode (EDR-freundlich)
- Proxy/MTLS für Remote

---

## 4) Research: Technologie-Optionen (und harte Tradeoffs)

### 4.1 Hotkey & Input Capture

**Option A: RegisterHotKey (empfohlen für MVP)**

- stabil, wenig “Security Smell”
- Einschränkung: Win-Taste-Kombos oft reserviert

**Option B: Low-level Keyboard Hook (Advanced)**

- kann theoretisch Win+H “sehen”
- ist konfliktanfällig und security-relevant (Keylogger-Pattern)
- in Enterprise-Umgebung potentiell EDR Alarm/Block

### 4.2 Text Injection

**Option A (MVP): Clipboard + Ctrl+V**

- maximal pragmatisch
- Limit: Clipboard side effects, secure fields, UIPI/elevated

**Option B: UI Automation “SetValue/ValuePattern/TextPattern”**

- funktioniert nur, wenn das Ziel-Control UIA Patterns anbietet
- oft gut in modernen Apps, aber nicht universal

**Option C (Langfrist): TSF TIP / IME**

- “native” Integration
- hoher Aufwand (COM/Win32), tests, signing, deployment

### 4.3 STT Engines – Local

**Whisper-Familie (lokal)**

1. **faster-whisper (CTranslate2)**  
   - sehr performant (CPU/GPU), quantization möglich, gute Genauigkeit  
   - Python-first, ideal für Prototyping
2. **whisper.cpp**  
   - C/C++ binary, sehr portable, hat ein “stream”-Beispiel für naive Realtime  
   - gut zum Bundlen als Tool oder als library binding
3. **Vosk**  
   - echtes Streaming, offline, kleine Modelle  
   - meist schwächer als Whisper bei freier Sprache (aber schnell & leicht)

### 4.4 STT Engines – Remote Provider

(Alle unterstützen grundsätzlich “streaming” oder “batch”, aber mit sehr verschiedenen APIs/Audioformaten/Preismodellen)

- **OpenAI**  
  - Realtime transcription sessions (WebSocket/WebRTC), u.a. 24 kHz PCM und server-side VAD; liefert deltas (inkrementelle Transkripte) je nach Modell.  
- **Azure Speech**  
  - Continuous recognition mit intermediate results, language identification möglich, sehr gut integrierbar unter Windows; ebenfalls Basis von Windows Voice Typing.  
- **Deepgram**  
  - WebSocket STT, optional multilingual code-switching (“language=multi”) – explizit für Sprachwechsel.  
- **AssemblyAI** ★ Standard Remote Provider (Phase 2)  
  - Batch: Audio-Upload via Python SDK (`assemblyai`), automatisches Polling  
  - Modelle: Universal-3-Pro (Primär) + Universal-2 (Fallback)  
  - 6 Sprachen: EN, ES, DE, FR, PT, IT; Language Detection automatisch  
  - Streaming STT via WebSocket mit partial/final transcripts (Phase 2b geplant)  
  - Preis: $0.21/Stunde Audio  
- **NVIDIA Parakeet** (Phase 3 geplant)  
  - `parakeet-tdt-0.6b-v3`: FastConformer-TDT Architektur (NeMo Framework)  
  - NICHT Whisper/CTranslate2-kompatibel — benötigt eigenen Provider  
  - 25 EU-Sprachen, 600M Parameter, WER: DE 5.04%, EN 4.85%
- **Google Cloud Speech-to-Text**  
  - Streaming recognition (gRPC).

---

## 5) Empfehlung: “Best Practical Plan” (MVP → Full)

### 5.1 MVP-Stack-Empfehlung

Für den schnellsten funktionierenden Prototypen unter Windows 11:

**Sprache/Framework:** Python 3.12+  
**UI:** PySide6 (Qt) – overlay window, tray icon, settings dialog  
**Hotkey:** Win32 RegisterHotKey via pywin32 (oder ctypes)  
**Audio capture:** sounddevice (PortAudio)  
**VAD:** Energiebasierte VAD (`vad.py`)  
**Local STT:** faster-whisper (CTranslate2)  
**Insertion:** Clipboard-safe paste + SendInput

Warum das gut ist:

- Python + PySide6 liefert dich schnell zu “funktioniert wirklich”
- faster-whisper ist performant und in Python angenehm zu integrieren
- RegisterHotKey vermeidet keylogger-ähnliche Hooks
- Clipboard-Paste ist der universellste Insert-Mechanismus für MVP

### 5.2 Remote Provider + Streaming

**Implementierungsstatus:**

| Feature | Status |
|---------|--------|
| AssemblyAI Batch (SDK) | **Implementiert** — Upload → Transkription → Einfügen |
| Lokales Streaming (faster-whisper) | **Implementiert** (experimentell) — sliding-window + overlap |
| AssemblyAI Streaming (WebSocket) | **Implementiert** |
| OpenAI Batch + Streaming (chunked) | **Implementiert** |
| Deepgram Batch + Streaming (WebSocket) | **Implementiert** |
| Azure | Geplant |

**AssemblyAI Remote Provider (implementiert)**

- **SDK:** `assemblyai` Python-Paket
- **Modell:** Universal-3-Pro + Universal-2 Fallback
- **Spracherkennung:** Automatisch (`language_detection=True`), 6 Sprachen (EN, ES, DE, FR, PT, IT)
- **Preis:** $0.21/Stunde Audio
- **API-Key:** Gespeichert über Windows Credential Manager (`keyring`), Eingabe im Settings-Dialog

**Lokales Streaming (experimentell, implementiert)**

- Lokales Streaming über faster-whisper mit sliding-window + overlap
- Inkrementelle Live-Einfügung am Cursor während der Aufnahme
- Auto-Abort bei Fokuswechsel mit Beep-Benachrichtigung
- Dokumentiert in `docs/streaming-mode.md`

**OpenAI + Deepgram Streaming (implementiert)**

- OpenAI: Streaming über chunked partial re-transcription via `/v1/audio/transcriptions`
- Deepgram: Provider-native WebSocket-Streaming (`/v1/listen`) mit partial/final merge

### 5.3 Geplante Erweiterungen

**AssemblyAI Streaming**

- WebSocket-Streaming für Echtzeit-Teilergebnisse

**Weitere Remote-Provider**

- OpenAI (Realtime transcription sessions, WebSocket/WebRTC)
- Azure Speech (Continuous recognition, intermediate results)
- Deepgram (WebSocket STT, multilingual code-switching)
- Google Cloud Speech-to-Text (gRPC Streaming)

**NVIDIA Parakeet / NeMo (evaluiert — nicht empfohlen)**

- **Modell:** `nvidia/parakeet-tdt-0.6b-v3` (FastConformer-TDT Architektur)
- **Nicht CTranslate2-kompatibel** — bräuchte komplett neuen Provider + NeMo/PyTorch Dependencies
- **Vorteile:** 25 EU-Sprachen, 600M Parameter, exzellente WER (DE 5.04%, EN 1.93%)
- **Nachteile:** NVIDIA GPU Pflicht, massive Dependencies (~2-4 GB), Linux bevorzugt
- **Entscheidung:** Nicht implementieren — Kosten/Nutzen-Verhältnis ungünstig für Desktop-App auf Firmen-Laptops
- **Evaluation:** Siehe `docs/parakeet-evaluation.md`

**Native Integration (Langfrist)**
Wenn du wirklich “wie Windows Input” willst:

- TSF TIP/IME in C++/Rust (via windows-rs) oder C# COM interop
- optional mit eigenem candidate/composition window
- dann wird deine App nicht nur “pasten”, sondern “input method”

---

## 6) Architektur (HLD)

### 6.1 Komponenten

- **HotkeyManager**
  - Register/Unregister global hotkey
  - dispatch “toggle recording” events
- **UI Overlay**
  - state machine: Idle → Listening → Processing → Inserted/Error
  - small window + tray menu
- **AudioCapture**
  - mic stream to ring buffer
  - optional gain control
- **VAD**
  - detects speech start/end
  - outputs segments / commits turns
- **Transcriber (interface)**
  - `transcribe_file(audio) -> text`
  - optional `start_stream() / push_audio_chunk() / on_partial_result()`
- **Providers**
  - `LocalFasterWhisperTranscriber` — implementiert (Batch + Streaming)
  - `AssemblyAITranscriber` — implementiert (Batch)
  - `OpenAITranscriber` — implementiert (Batch + Streaming)
  - `DeepgramTranscriber` — implementiert (Batch + Streaming)
  - Azure — geplant
- **TextInserter**
  - Strategy: clipboard-paste (default)
  - Alternative: UIA setvalue (best effort)
- **SettingsStore**
  - JSON in %APPDATA% + migration
- **Logger**
  - structured log + diag export

### 6.2 Datenfluss (MVP Batch)

Hotkey → UI “Listening” → AudioCapture buffer → VAD decides end → Transcriber transcribes → TextInserter pastes → UI “Done”

### 6.3 Datenfluss (Streaming)

Hotkey → UI “Listening” → AudioCapture chunked → Transcriber streaming provider → UI shows partial → VAD commit end → final insert

---

## 7) Risiken / Stolpersteine (wichtig!)

1. **Win+H nicht ersetzbar**
   - OS reserviert Win-key shortcuts; Win+H ist Voice Typing. Plan daher mit custom hotkey.
2. **UIPI / Elevated windows**
   - Wenn Ziel-App elevated ist (Admin), kann ein normaler Prozess ggf. nicht zuverlässig Input senden. UIAccess=true ist möglich, aber Deployment/Signing/secure location erforderlich.
3. **Keyboard Hook = “Keylogger smell”**
   - Hooking kann EDR/AV triggern, besonders in Corporate Umgebungen.
4. **Clipboard Side Effects**
   - Muss sorgfältig restore, Race conditions vermeiden, optional “no clipboard modify” mode.
5. **Audio Device quirks**
   - Sample rate mismatches, exclusive mode, device switching.
6. **Streaming Quality**
   - Partial transcripts können “flattern” (revision), braucht Stabilization.
7. **Model/Performance**
   - Whisper large ist teuer; quantization/GPU optional, fallback to small/base.
8. **Privacy/Compliance**
   - Remote provider bedeutet Audio-Upload; braucht klare toggles/logs und evtl. opt-in.

---

## 8) Bill of Materials (BOM) – aktuell

### Runtime

- Python 3.12+
- PySide6
- ctypes + user32/kernel32 (Win32 API direkt, kein pywin32 nötig)
- sounddevice (+ PortAudio)
- numpy
- faster-whisper (inkl. ctranslate2)
- assemblyai SDK (für AssemblyAI Remote Provider)
- keyring (für API-Key-Speicherung via Windows Credential Manager)
- requests (für HuggingFace Hub Downloads)

### Assets

- Whisper model weights (z.B. `small`, `medium`, `large-v3-turbo`, `distil-large-v3.5`)
- App icon/tray icon

### Nicht verwendet (ursprünglich geplant)

- ~~onnxruntime / Silero VAD~~: App nutzt energiebasierte VAD (`vad.py`), nicht Silero
- ~~pyperclip~~: App nutzt native Win32 clipboard via ctypes

### Build/Packaging

- PyInstaller oder Nuitka
- optional: MSI (WiX) oder Squirrel/winget packaging

---

## 9) Abnahmetests (konkret, prompt-tauglich)

### Testfälle MVP

1. Notepad: Hotkey → “Hallo Welt” → Text erscheint.
2. Chrome Textarea: Hotkey → Diktat → Text erscheint.
3. Teams/Slack: Hotkey → Diktat → Text erscheint.
4. Stop während Aufnahme: Kein Crash, Audio released.
5. Kein Mikrofon: Klarer Error, keine UI-Hänger.
6. Modell fehlt: Auto-download prompt oder sauberer Error.

### Testfälle Enhanced

- Streaming partials erscheinen binnen 300–800ms (remote) bzw. 0.5–1.5s (local) je nach chunking
- Language auto detection: Deutsch → Englisch Wechsel in einer Session (Provider dependent)

---

## 10) Agent-Prompt-Seed (für Codex)

Du kannst dem Agenten ungefähr so starten (hier bewusst als Textbaustein, nicht als Code):

- “Implement an MVP Windows dictation app on Windows 11 with: global hotkey (RegisterHotKey), small always-on-top overlay, mic capture, VAD-based segmentation, transcription using faster-whisper, and text insertion by clipboard-safe paste at caret. Provide a settings dialog to choose model size and hotkey. Ensure no low-level keyboard hooks in MVP. Write clean modular code with classes: HotkeyManager, AudioCapture, Vad, Transcriber, TextInserter, OverlayUI, SettingsStore. Package via PyInstaller.”

---

## 11) Quellen (nur die wichtigsten, für Design-Entscheidungen)

(Die konkreten Links sind in deinen Research-Notizen/Chat-Citations enthalten; hier nur als “was ist belegt”)

- Windows Voice Typing nutzt Online Speech Recognition (Azure Speech) und wird mit Win+H gestartet. (Microsoft Support)
- Win-Key-Hotkeys sind systemreserviert; neue kommen mit Windows-Versionen dazu. (Raymond Chen / Microsoft)
- TSF ist der native Framework-Weg für “Text Services / Input Methods”. (Microsoft Learn)
- UIAccess/UAC Policies existieren und sind für Interaktion mit höher privilegierten UIs relevant. (Microsoft Learn)
- OpenAI Realtime transcription: Audio/PCM 24kHz, deltas/events, server_vad. (OpenAI Docs)
- faster-whisper (CTranslate2) und whisper.cpp liefern praktikable lokale Whisper-Varianten. (GitHub repos)
- Deepgram bietet explizites multilingual code-switching (language=multi). (Deepgram docs)
- Google STT Streaming via gRPC; AssemblyAI Streaming via WebSocket; Azure intermediate results. (jeweilige Doku)

---

## 12) Getroffene Entscheidungen (ehemals offen)

1. **Default Hotkey**: `Ctrl+Alt+Space` — Fallback `Ctrl+Win+LShift`. Key-Capture UI statt manuelle Texteingabe.
2. **Local STT Default Modell**: `small` — bester Tradeoff aus Geschwindigkeit, Qualität und Downloadgröße (~484 MB).
3. **Streaming zuerst lokal oder remote?** Lokal zuerst; anschließend Remote-Streaming für AssemblyAI, OpenAI und Deepgram implementiert.
4. **Insert-Strategie**: Clipboard-safe Paste mit 3 Modi (Auto, SendInput, WM_PASTE). UIA/TSF nicht implementiert (Aufwand zu hoch für Nutzen).
5. **Deployment**: PyInstaller `.spec` vorhanden; Wheelhouse-Offline-Install dokumentiert.
6. **Deployment**: einfache exe vs installer + auto-update
