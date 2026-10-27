# Changelog

### 27/10/2026
* First version: HTTPS + WebSocket server with a pairing code, lockout after wrong codes and a local-network-only filter.
* Phone page that captures the microphone (AudioWorklet, 20 ms chunks), keeps the screen awake and reconnects on its own.
* Audio output into VB-CABLE's "CABLE Input" through WASAPI with a small jitter buffer.
* Self-signed certificate generated per set of local addresses.
* Unit tests for the buffer, pairing, certificates and the WebSocket protocol, plus a real-hardware loopback check
  (1 kHz tone through CABLE, about 150 ms end to end).
* Tested from the S20 FE through Chrome: the phone worked as the microphone button of Claude Desktop.
* Native Android app (Kotlin, no AndroidX): foreground microphone service, trust-on-first-use certificate pinning,
  reconnection with backoff, Wi-Fi and wake locks. Builds to a 1.3 MB debug APK.
* The server now prints the certificate SHA-256 and serves the APK at `/app.apk`, so the phone can install it over
  Wi-Fi without a USB cable.
* Published the project as a public GitHub repository (jmtdev0/PhoneMic) under the MIT license.
* Release 0.1.1: the Android app's example address is now generic (192.168.1.20:8443); 0.1.0 was withdrawn.
* Version 0.2.0, pairing by QR code that never expires:
  * Single-use pairing tokens (5 minutes) shown as a QR code on a loopback-only pairing page, with the link also as text.
  * Each phone gets a permanent identity: a P-256 key created in the Android Keystore, proven by signing a fresh
    challenge on every connection. The paired phones are listed on the page and can be revoked, which cuts them off at once.
  * Stable server key: the certificate is re-issued when the addresses change but the key and its pin stay, so the app
    pins the public key instead of trusting the first certificate it sees.
  * The app finds the PC again after an address change by sweeping the Wi-Fi network for the pinned key.
  * Tests: 48 for the PC side (pairing flow over real sockets included) and 10 for the app; checked end to end on an
    Android 16 emulator against the real server (pair, stream, rediscover after a wrong address, revoke).
