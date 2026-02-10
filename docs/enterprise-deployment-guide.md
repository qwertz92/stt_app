# Enterprise Deployment Guide (Windows / Corporate)

Dieser Guide beschreibt, wie `tts_app` in stark eingeschraenkten Unternehmensumgebungen lauffaehig gemacht wird (Zscaler, GPO/AppLocker, kein `uv.exe`, eingeschraenkter Internetzugriff).

## Kurzfazit

- Diese App muss auf **nativem Windows** laufen (nicht in Linux/WSL), weil sie Win32-APIs fuer Hotkey/Clipboard/Input nutzt.
- `uv` ist optional. Wenn `uv.exe` geblockt ist, verwende normalen Python + pip-Workflow.
- Fuer strikt abgeschottete Netze ist ein **Wheelhouse (Offline-Paketordner)** die robusteste Methode.

## Was ist ein Wheel?

Ein Wheel ist ein vorgebautes Python-Paket im Format `.whl`.

- Vergleichbar mit einem fertigen Binärpaket.
- Vorteil: keine lokale Kompilierung notwendig.
- Installation ist schneller und stabiler als Source-Builds.
- Besonders wichtig bei Paketen mit nativen Anteilen (z. B. `pywin32`, `numpy`, `ctranslate2`).

Beispiel:
- `pywin32-308-cp312-cp312-win_amd64.whl`
  - `cp312`: Python 3.12
  - `win_amd64`: Windows x64

## Option A: Standard Windows Setup ohne uv

Voraussetzungen:
- Python 3.12 installiert
- Zugriff auf internen PyPI-Proxy/Artifactory

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev-win.txt
python main.py
```

Tests:

```powershell
python -m pytest
python scripts/smoke_test.py
```

## Option B: Offline/Wheelhouse Setup (empfohlen fuer stark eingeschraenkte Netze)

### B1) Auf einem Build-Rechner mit Paketzugriff

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip download -r requirements-dev-win.txt -d wheelhouse
```

Ergebnis: Ordner `wheelhouse/` mit allen `.whl` (und ggf. sdists).

### B2) Wheelhouse intern verteilen

- Als ZIP in internes Artefakt-Repo ablegen
- Oder auf Fileshare bereitstellen

### B3) Auf Zielrechner installieren (ohne Internet)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --no-index --find-links .\wheelhouse -r requirements-dev-win.txt
python main.py
```

## Option C: PyInstaller EXE (Deployment-freundlich)

Im Projekt ist `tts_app.spec` vorhanden.

Build:

```powershell
python -m pip install pyinstaller
pyinstaller tts_app.spec
```

Hinweise:
- EXE sollte idealerweise signiert werden (Code Signing), sonst greifen manche Unternehmensrichtlinien/EDR.
- EDR kann Input-Injection dennoch blockieren; dann braucht es Policy-Freigabe fuer die signierte Anwendung.

## WSL: Hilft das?

Kurz: fuer Laufzeit der App **nein**.

- WSL/Linux kann fuer Git/Editor/Build-Skripte hilfreich sein.
- Die App selbst benoetigt Windows-spezifische APIs (`pywin32`, `RegisterHotKey`, `SendInput`, Foreground Window).
- Daher: Start der App auf nativer Windows-Python-Umgebung.

## Troubleshooting Checklist (Corporate)

1. `uv.exe` durch GPO geblockt:
- Ohne `uv` arbeiten (Option A/B).

2. `irm ... | iex` durch Zscaler blockiert:
- Keine Installer-Skripte aus dem Internet ausfuehren.
- Interne Artefaktquelle oder Wheelhouse verwenden.

3. `pywin32` auf WSL/Linux nicht installierbar:
- Erwartetes Verhalten (Windows-only Paket).
- App auf Windows laufen lassen.

4. App transkribiert, fuegt aber nicht ein:
- Paste-Mode in Settings wechseln (`auto`, `wm_paste`, `send_input`).
- Bei Problemen laesst `keep_transcript_in_clipboard` den Text fuer manuelles Einfuegen im Clipboard.

5. Zielanwendung ist elevated (Admin) oder geschuetzt:
- App mit passenden Rechten starten oder IT-Freigabe fuer UI-Interaktion klaeren.

## Empfohlener Rollout in Firmenumgebung

1. Internes Wheelhouse bauen und versionieren.
2. Python 3.12 + venv + Offline-Install standardisieren.
3. Smoke-Test als Pflichtschritt aufnehmen (`python scripts/smoke_test.py`).
4. Optional: signierte PyInstaller-EXE fuer Endnutzer verteilen.
5. Hotkey/Paste-Mode per Voreinstellung in `%APPDATA%\tts_app\settings.json` zentral vorgeben.
