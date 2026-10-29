package app.phonemic

/** State shared between the capture service and the screen; the screen only reads it. */
object MicState {
    @Volatile var running = false
    /** True once the PC accepted this phone and the audio is flowing. */
    @Volatile var ready = false
    @Volatile var status = "Stopped"
    @Volatile var stats = ""
    @Volatile var level = 0f
    @Volatile var fingerprint: String? = null

    // What the PC lets this phone do besides being a microphone (set on the PC's pairing page).
    @Volatile var canDictate = false
    @Volatile var canSend = false
    /** Claude Desktop's dictation is on, as last reported by the PC. */
    @Volatile var dictating = false
    /** The PC's answer to the last Dictate or Send press. */
    @Volatile var controlStatus = ""
}
