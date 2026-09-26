# Fusion 360 on Linux with Bottles (Flatpak) — Community Tutorial

> ⚠️ **Legacy (May 2026).** Kept for reference. On current Fusion builds this setup reaches the login screen but the
> viewport does not render. See the [main README](../README.md) for the Docker + patched Wine + OpenGL method.

A complete guide to install and run Autodesk Fusion 360 on Linux using Bottles (Flatpak),
including solving the OAuth2 login problem which is the main challenge.

**Tested on:** Pop!_OS 22.04 LTS · kernel 6.x · NVIDIA discrete GPU  
**Should work on:** Ubuntu 22.04+, Debian-based distros with Flatpak support  
**Fusion 360 version:** 2702.x (2025–2026 releases)

---

## What You Need

| Software | Version | Install |
|---|---|---|
| Flatpak | any | `sudo apt install flatpak` |
| Bottles | 63.2+ | `flatpak install flathub com.usebottles.bottles` |
| Firefox | any | default browser (for OAuth login) |
| `nsenter` | any | included in `util-linux` (pre-installed) |
| Wine | sys-wine-11.0 | provided by Bottles runner |

---

## Part 1 — Install Fusion 360 in Bottles

### 1.1 Create the Bottle

Open Bottles → New Bottle:
- **Name:** Fusion-360 (or anything you like)
- **Environment:** Gaming
- **Runner:** sys-wine-11.0 (or latest available)
- **Architecture:** win64

### 1.2 Bottle settings (before installing)

In the bottle's settings, configure:

```
Parameters:
  Windows version: Windows 10
  DXVK: enabled
  Renderer: vulkan
  Discrete GPU: true (if you have a dedicated GPU)
  VKD3D: enabled
```

Install these dependencies via Bottles → Installers or via `winetricks`:
```
d3dx9, d3dcompiler_43, d3dcompiler_47, mono, gecko, webview2
```

### 1.3 Download and install Fusion 360

Download the Fusion 360 installer from autodesk.com.  
Run it inside the bottle: Bottles → Run Executable → select the installer.

Follow the installer normally. Fusion will download and install itself.

After installation, Bottles will auto-detect `FusionLauncher.exe` as an executable.

---

## Part 2 — The Program Arguments (Critical Fix)

This is what makes Fusion actually run without crashing.

In Bottles, go to the detected **Autodesk Fusion** program → edit arguments.  
Add this **before** `%command%`:

```
WINEDEBUG=-all WINEDLLOVERRIDES="api-ms-win-crt-private-l1-1-0,api-ms-win-crt-conio-l1-1-0,api-ms-win-crt-convert-l1-1-0,api-ms-win-crt-environment-l1-1-0,api-ms-win-crt-filesystem-l1-1-0,api-ms-win-crt-heap-l1-1-0,api-ms-win-crt-locale-l1-1-0,api-ms-win-crt-math-l1-1-0,api-ms-win-crt-multibyte-l1-1-0,api-ms-win-crt-process-l1-1-0,api-ms-win-crt-runtime-l1-1-0,api-ms-win-crt-stdio-l1-1-0,api-ms-win-crt-string-l1-1-0,api-ms-win-crt-utility-l1-1-0,api-ms-win-crt-time-l1-1-0,atl140,concrt140,msvcp140,msvcp140_1,msvcp140_atomic_wait,ucrtbase,vcomp140,vccorlib140,vcruntime140,vcruntime140_1=n,b;adpclientservice.exe=" %command%
```

**Why this works:**  
Without it, Wine uses its own internal replacements for the Microsoft Visual C++ runtime
DLLs (`vcruntime140`, `msvcp140`, `ucrtbase`, etc.). Fusion 360 ships its own versions
of these DLLs. The `=n,b` flag (native first, then builtin) forces Wine to use Fusion's
own copies, which avoids a guaranteed page fault crash in `ntdll.dll` at startup.

The `adpclientservice.exe=` entry prevents the Autodesk Desktop Connector service from
loading, which is not needed for desktop use and can cause issues under Wine.

---

## Part 3 — The OAuth2 Login Problem

This is the hardest part. When you click **Sign In** in Fusion 360, it opens your browser.
After you log in on autodesk.com, the browser tries to redirect to a URL like:

```
adskidmgr:/login?code=XXXX&state=YYYY
```

This `adskidmgr://` is a custom URI scheme. On Linux, the browser needs to hand this
URL to a program that delivers the OAuth code into the Wine session running Fusion 360.

The challenge: Fusion runs inside a **bwrap sandbox** (Flatpak's isolation layer).
The `AdskIdentityManager.exe` process waiting for the code is inside that sandbox,
connected to a specific wineserver instance. If you naively launch a new `wine` process
to handle the callback, it connects to a different wineserver and never finds the SSO
server → login fails with "No SSO server is running".

**The solution:** use `nsenter` to enter the bwrap namespace of the running Fusion
process, then run `AdskIdentityManager.exe` from inside that namespace. This way it
finds the correct wineserver socket and delivers the OAuth code to the right process.

### 3.1 Create the protocol handler script

```bash
sudo nano /usr/local/bin/autodesk360-handler
```

Paste this content:

```bash
#!/bin/bash
URL="$1"
LOGFILE="/tmp/autodesk360-handler.log"

# Adjust this path to match your Bottles bottle location
WINEPREFIX="$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/Fusion-360"

# Find the AdskIdentityManager.exe path — it changes with updates
# Run: find "$WINEPREFIX/drive_c" -name "AdskIdentityManager.exe"
# and update the path below
ADSKIDMGR='C:\Program Files\Autodesk\webdeploy\production\<HASH>\Autodesk Identity Manager\AdskIdentityManager.exe'

exec >> "$LOGFILE" 2>&1

echo "$(date -Iseconds) === CALLBACK RECEIVED ==="
echo "$(date -Iseconds) URL: '$URL'"

# Find the Linux PID of the running Fusion process
FUSION_PID=$(pgrep -f "Fusion360.exe" | head -1)
if [ -z "$FUSION_PID" ]; then
    echo "$(date -Iseconds) ERROR: Fusion360 is not running"
    exit 1
fi
echo "$(date -Iseconds) Fusion360 Linux PID: $FUSION_PID"

# Read the actual UID/GID from the Fusion process (do not hardcode)
FUSION_UID=$(awk '/^Uid:/{print $2}' /proc/$FUSION_PID/status)
FUSION_GID=$(awk '/^Gid:/{print $2}' /proc/$FUSION_PID/status)

# Read the DISPLAY used by Fusion
FUSION_DISPLAY=$(tr '\0' '\n' < /proc/$FUSION_PID/environ 2>/dev/null \
    | grep '^DISPLAY=' | head -1 | cut -d= -f2-)
[ -z "$FUSION_DISPLAY" ] && FUSION_DISPLAY=":1"

echo "$(date -Iseconds) UID=$FUSION_UID GID=$FUSION_GID DISPLAY=$FUSION_DISPLAY"

# Enter the bwrap mount+ipc namespaces and run AdskIdentityManager
# --mount: sees the same /tmp (where the wineserver socket lives)
# --ipc:   sees the same IPC namespace (shared memory objects)
# --setuid/--setgid: run as the same user as Fusion (not root)
# NO --pid: causes issues with --setuid in some kernel configurations
sudo nsenter -t "$FUSION_PID" --mount --ipc \
    --setuid "$FUSION_UID" --setgid "$FUSION_GID" -- \
    env HOME="$HOME" \
        USER="$USER" \
        USERNAME="$USER" \
        LOGNAME="$USER" \
        WINEPREFIX="$WINEPREFIX" \
        WINEDEBUG=-all \
        DISPLAY="$FUSION_DISPLAY" \
    /app/bin/wine "$ADSKIDMGR" "$URL"

echo "$(date -Iseconds) Exit code: $?"
```

Make it executable:
```bash
sudo chmod +x /usr/local/bin/autodesk360-handler
```

### 3.2 Find the AdskIdentityManager path

The `<HASH>` in the path changes with every Fusion update. Find it with:

```bash
find ~/.var/app/com.usebottles.bottles/data/bottles/bottles/Fusion-360/drive_c \
  -name "AdskIdentityManager.exe" 2>/dev/null
```

Update the `ADSKIDMGR` variable in the handler with the full Windows path
(using backslashes), e.g.:
```
C:\Program Files\Autodesk\webdeploy\production\4bca736...\Autodesk Identity Manager\AdskIdentityManager.exe
```

### 3.3 Register the protocol handler

Create the desktop entry:

```bash
nano ~/.local/share/applications/adskidmgr.desktop
```

```ini
[Desktop Entry]
Name=Autodesk Identity Manager
Exec=/usr/local/bin/autodesk360-handler %u
Type=Application
NoDisplay=false
Terminal=false
MimeType=x-scheme-handler/adskidmgr;x-scheme-handler/autodesk360;
```

Register it:
```bash
xdg-mime default adskidmgr.desktop x-scheme-handler/adskidmgr
xdg-mime default adskidmgr.desktop x-scheme-handler/autodesk360
update-desktop-database ~/.local/share/applications/
```

### 3.4 Allow nsenter without password

The handler is called by the browser (Firefox), which cannot type a sudo password.
Create a sudoers rule:

```bash
sudo nano /etc/sudoers.d/autodesk360-nsenter
```

```
your-username ALL=(root) NOPASSWD: /usr/bin/nsenter
```

Replace `your-username` with your actual Linux username.  
Verify the path to nsenter with `which nsenter` (usually `/usr/bin/nsenter`).

### 3.5 Configure Firefox to use the handler

Open Firefox and go to `about:config`.  
Search for: `network.protocol-handler.expose.adskidmgr`  
Set it to `false` (creates it if it doesn't exist).

Then open your Firefox profile's `handlers.json` to confirm or manually add:

```bash
# Find your profile
ls ~/.mozilla/firefox/*.default-release/handlers.json
```

Edit the file and make sure the `schemes` section includes:
```json
"adskidmgr": { "action": 4 }
```

Action `4` = use external application (your handler script).

Alternatively, trigger it automatically: after visiting the OAuth page once and Firefox
asks what to do with `adskidmgr://`, choose "Open with" → "Other" → browse to
`/usr/local/bin/autodesk360-handler` and check "Remember for this site".

---

## Part 4 — Logging In

1. Make sure Fusion 360 is open and showing the login screen.
2. Click **Sign In** — your browser will open autodesk.com.
3. Log in normally with your Autodesk account.
4. After login, the browser receives the `adskidmgr://` callback and calls your handler.
5. The handler enters the Bottles bwrap namespace, runs `AdskIdentityManager.exe`,
   which finds the running SSO server and delivers the OAuth code.
6. Fusion 360 shows the main interface — you are logged in.

**The OAuth code expires in ~5 minutes.** Complete the browser login quickly.

To monitor the handler in real time:
```bash
tail -f /tmp/autodesk360-handler.log
```

A successful delivery looks like:
```
Sending oauth2 code signal AdOAuth2Code-XXXX to the SSO server process pid XXXX
Found valid http route: /login
Sending quit signal AdOAuth2Code-XXXX
Exit code: 0
```

---

## Part 5 — Performance Tips

Fusion 360 runs slower than on Windows — this is expected under Wine/translation layers.
These settings help:

**In the bottle (bottle.yml or Bottles UI):**
- `renderer: vulkan` + `dxvk: true` → DXVK translates Direct3D 11 to Vulkan.
  This is faster and more stable than the default OpenGL translation (WineD3D).
- `discrete_gpu: true` → uses your dedicated GPU.

**Inside Fusion 360 (Preferences → Graphics):**
- Lower visual effects quality
- Disable shadows and ambient occlusion
- Reduce or disable antialiasing
- Disable reflection and texture display if not needed

---

## Troubleshooting

### "No SSO server is running" in handler log
The wine process launched by the handler is not connecting to the correct wineserver.
Causes and fixes:
- Fusion 360 is not running → start Fusion first, then log in.
- `FUSION_PID` found the wrong process → check with `pgrep -af Fusion360`.
- Namespace entry failed → check sudo permissions for nsenter.
- Run the diagnostic commands in the handler (see `id` output in the log).

### Fusion crashes with page fault in ntdll.dll on startup
Missing or wrong `WINEDLLOVERRIDES`. Apply the full overrides string from Part 2.

### cer_dialog crash after Fusion crash
Normal behavior under Wine. The Autodesk crash reporter (`cer_dialog.exe`) itself
crashes because it uses Windows APIs not fully implemented in Wine. Ignore it.
Clear lock files and restart Fusion:
```bash
find ~/.var/app/com.usebottles.bottles/data/bottles/bottles/Fusion-360 \
  -name "*.lock" -o -name "*.lck" | xargs rm -f 2>/dev/null
```

### Fusion won't open after a crash (stuck on "Initializing")
Stale lock files from the previous crash are blocking startup. Run:
```bash
BOTTLE="$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/Fusion-360"
find "$BOTTLE" \( -name "*.lock" -o -name "*.lck" \) -delete
pkill -f wineserver
```
Then reopen from Bottles.

### AdskIdentityManager.exe path changes after update
Fusion updates itself to a new production hash. After an update, find the new path:
```bash
find ~/.var/app/com.usebottles.bottles/data/bottles/bottles/Fusion-360/drive_c \
  -name "AdskIdentityManager.exe"
```
Update the `ADSKIDMGR` variable in `/usr/local/bin/autodesk360-handler`.

---

## Why This Works — Technical Summary

Fusion 360's login uses **OAuth2 PKCE**. The flow is:

```
Fusion 360 (Wine) → opens browser → user logs in → autodesk.com redirects to
adskidmgr://login?code=XXX&state=YYY → browser calls handler script →
handler enters bwrap namespace via nsenter → runs AdskIdentityManager.exe
inside the correct wineserver context → finds IDSDKIpcServers-v2 shared memory
→ signals AdOAuth2Code-<PID> event → SSO server exchanges code+PKCE verifier
for access token → Fusion 360 is authenticated
```

The bwrap sandbox (Flatpak's isolation) creates private mount and IPC namespaces.
The wineserver socket lives at `/tmp/.wine-<uid>/server-<dev>-<inode>/socket` inside
the bwrap's private `/tmp`. Without `nsenter --mount`, any wine process you launch
on the host won't see this socket and will start its own isolated wineserver — which
has no knowledge of Fusion's SSO server.

`nsenter --mount --ipc` enters the existing namespaces, making the new wine process
join the correct wineserver session and find the waiting SSO server process.

The `wine start "adskidmgr://..."` approach (using ShellExecute) **does not work**
because Wine's ShellExecute truncates the URL at the first `&` character, dropping
the `&state=...` parameter which is required for PKCE verification.
You must call `AdskIdentityManager.exe` directly with the full URL as an argument.

---

## Files Summary

| File | Purpose |
|---|---|
| `/usr/local/bin/autodesk360-handler` | Protocol handler — delivers OAuth callback to Wine |
| `~/.local/share/applications/adskidmgr.desktop` | Registers `adskidmgr://` scheme with the desktop |
| `/etc/sudoers.d/autodesk360-nsenter` | Allows passwordless `sudo nsenter` for the handler |
| `~/.mozilla/firefox/*.default-release/handlers.json` | Firefox protocol → handler association |

---

*Tested May 2026. Contributions and corrections welcome.*
