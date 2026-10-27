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
