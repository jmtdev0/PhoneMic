package app.phonemic

/** State shared between the capture service and the screen; the screen only reads it. */
object MicState {
    @Volatile var running = false
    @Volatile var status = "Stopped"
    @Volatile var stats = ""
    @Volatile var level = 0f
    @Volatile var fingerprint: String? = null
}
