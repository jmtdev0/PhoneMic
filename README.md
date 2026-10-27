# PhoneMic

Use a phone as the microphone of a Windows PC over the local network. The phone opens a web page, captures its
microphone and streams it to the PC, which plays it into a virtual audio cable. Any app that can record from that
cable (for example the microphone button of Claude Desktop) hears the phone.

```
phone (Chrome) --wss--> PhoneMic server --WASAPI--> "CABLE Input"  ==>  "CABLE Output" (the microphone apps use)
```

## Requirements

- Windows 10/11 and Python 3.13.
- [VB-CABLE](https://vb-audio.com/Cable/) installed (provides "CABLE Input" and "CABLE Output").
- PC and phone on the same Wi-Fi network.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Use

1. Run `run.cmd` (or `python -m phonemic`). It prints an `https://<pc-ip>:8443` address and a 6-digit pairing code.
   The first time, allow Python through Windows Firewall on **private networks**.
2. On the phone, open that address. Chrome warns about the certificate because it is self-signed: choose
   *Advanced* -> *Proceed*.
3. Enter the pairing code, press **Start**, and allow the microphone. Keep the page open; the screen stays on.
4. On the PC, make **CABLE Output** the default recording device (*Settings -> System -> Sound -> Input*).

Options: `--output` (playback device name fragment), `--port`, `--code`, `--prebuffer-ms`, `--list-devices`.

## Android app (keeps working with the screen off)

The web page needs the phone's screen on. The native app in `android/` runs as a foreground service of type
"microphone", so it keeps sending with the screen off.

- Build (JDK 21 from Android Studio, Android SDK 36): from `android/`,
  `.\gradlew.bat assembleDebug` with `JAVA_HOME` and `ANDROID_HOME` set. The APK lands in
  `android/app/build/outputs/apk/debug/app-debug.apk`.
- Install without a cable: with the server running, open `https://<pc-ip>:8443/app.apk` on the phone, download it and
  allow installing from that source. It is debug-signed, so Play Protect may ask for confirmation.
- Use: enter the PC address and pairing code once, press **Start**, allow the microphone and notifications.
- The app pins the PC's certificate the first time it connects (trust on first use) and shows its SHA-256, which the
  PC window prints too. If the PC's addresses change and the certificate is regenerated, tap **Forget certificate**.

## Security

- Only loopback and private-network (RFC 1918) addresses can connect.
- A pairing code is required; five wrong attempts lock that address out for a minute.
- The code changes on every start, and nothing leaves the local network.
- The certificate is generated locally in `certs/` (ignored by git) and renewed when the PC's addresses change.

## Checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe -m scripts.loopback_check   # real audio: tone -> CABLE Input -> CABLE Output
```
