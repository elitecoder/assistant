import AudioToolbox
import AVFoundation
import ObjCExceptions
import KokoroSwift
import SessionCore
import os
import Speech

let voiceLog = Logger(subsystem: "com.assistant.sessions", category: "voice")

/// Hears one spoken question, then hands its text to `onHeard` after a pause in speech.
/// A microphone that sends pure silence is skipped for the next one, the way call apps do.
final class Listener: ObservableObject {
    @Published var listening = false
    @Published var heard = ""
    @Published var levels = [Float](repeating: 0, count: Listener.bars)
    @Published var microphone: String?
    @Published var note: String?
    static let bars = 36
    static let silenceBeforeSwitch: TimeInterval = 0.7
    var onHeard: ((String) -> Void)?
    var onProblem: ((String) -> Void)?

    private var engine = AVAudioEngine()
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var pause: Timer?
    private var loudest: Float = 0
    private var micID: UInt32?
    private var tried = Set<UInt32>()
    private var formatRetried = Set<UInt32>()
    private var transcript = TranscriptAccumulator()
    private var buffers = 0
    private var droppedBuffers = 0
    private var startedAt = Date()
    private var gotSignal = false
    private var session = UUID()

    func toggle() { listening ? stop() : start() }

    var choices: [String] { InputDevice.all().filter { !$0.virtual }.map(\.name) }

    var current: String? { listening ? microphone : InputDevice.chosen()?.name }

    func use(_ name: String) {
        InputDevice.preferred = name
        note = nil
        objectWillChange.send()
        guard listening else { return }
        teardown()
        tried = []
        begin()
    }

    private var pressedAt = Date()
    private var firstBufferLogged = false

    private func elapsed() -> String { String(format: "%.0f ms", Date().timeIntervalSince(pressedAt) * 1000) }

    func start() {
        pressedAt = Date()
        voiceLog.notice("timing: pressed")
        AVCaptureDevice.requestAccess(for: .audio) { mic in
            SFSpeechRecognizer.requestAuthorization { speech in
                DispatchQueue.main.async {
                    if !mic {
                        self.onProblem?("Assistant can't use the microphone. Allow it in System Settings, Privacy & Security, Microphone.")
                    } else if speech != .authorized {
                        self.onProblem?("Assistant can't use speech recognition. Allow it in System Settings, Privacy & Security, Speech Recognition.")
                    } else {
                        self.tried = []
                        self.formatRetried = []
                        self.note = nil
                        self.begin()
                    }
                }
            }
        }
    }

    private func begin() {
        voiceLog.notice("timing: permissions done after \(self.elapsed(), privacy: .public)")
        guard !listening, let recognizer, recognizer.isAvailable else {
            onProblem?("Speech recognition isn't available right now.")
            return
        }
        guard let mic = InputDevice.chosen(excluding: tried) else {
            onProblem?("None of your microphones sent any sound. Check them in System Settings, Sound.")
            return
        }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        engine = AVAudioEngine()
        let input = engine.inputNode
        if let unit = input.audioUnit {
            var device = AudioDeviceID(mic.id)
            let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                              &device, UInt32(MemoryLayout<AudioDeviceID>.size))
            if status != noErr { voiceLog.error("couldn't select \(mic.name, privacy: .public): \(status)") }
        }
        micID = mic.id
        microphone = mic.name
        levels = [Float](repeating: 0, count: Self.bars)
        loudest = 0
        buffers = 0
        droppedBuffers = 0
        startedAt = Date()
        gotSignal = false
        let token = UUID()
        session = token
        let hardware = input.inputFormat(forBus: 0)
        let format = input.outputFormat(forBus: 0)
        guard Microphones.usable(sampleRate: hardware.sampleRate, channels: hardware.channelCount),
              Microphones.usable(sampleRate: format.sampleRate, channels: format.channelCount) else {
            voiceLog.error("\(mic.name, privacy: .public) has no usable format yet (\(format.sampleRate) Hz, \(format.channelCount) ch)")
            retryOrSkip(mic)
            return
        }
        var startError: Error?
        let raised = AssistantCatchException {
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
                request.append(buffer)
                guard let channel = buffer.floatChannelData?[0] else { return }
                let samples = UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength))
                let level = Meter.level(samples)
                let silent = samples.allSatisfy { $0 == 0 }
                DispatchQueue.main.async {
                    guard let self, self.session == token else { return }
                    self.took(level: level, silent: silent)
                }
            }
            self.engine.prepare()
            do { try self.engine.start() } catch { startError = error }
        }
        if raised != nil || startError != nil {
            let reason = raised.map { "\($0.name.rawValue): \($0.reason ?? "")" } ?? startError!.localizedDescription
            voiceLog.error("microphone \(mic.name, privacy: .public) failed: \(reason, privacy: .public)")
            _ = AssistantCatchException { input.removeTap(onBus: 0) }
            tried.insert(mic.id)
            begin()
            return
        }
        voiceLog.notice("listening on \(mic.name, privacy: .public) after \(self.elapsed(), privacy: .public)")
        firstBufferLogged = false
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.silenceBeforeSwitch + 0.3) { [weak self] in
            guard let self, self.session == token, self.listening, self.buffers == 0 else { return }
            voiceLog.notice("\(mic.name, privacy: .public) delivered no audio, trying the next one")
            self.skipSilentMicrophone()
        }
        self.request = request
        transcript = TranscriptAccumulator()
        heard = ""
        listening = true
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self, self.session == token, self.listening else { return }
                if let result {
                    self.heard = self.transcript.update(result.bestTranscription.formattedString)
                    self.waitForPause()
                    if result.isFinal { self.stop() }
                } else if let error = error as NSError? {
                    voiceLog.error("recognition error: \(error.domain, privacy: .public) \(error.code)")
                    self.stop()
                    self.onProblem?(Spoken.problem(domain: error.domain, code: error.code,
                                                   description: error.localizedDescription))
                }
            }
        }
        waitForPause()
    }

    private func took(level: Float, silent: Bool) {
        guard listening else {
            droppedBuffers += 1
            return
        }
        buffers += 1
        if !firstBufferLogged {
            firstBufferLogged = true
            voiceLog.notice("timing: first audio after \(self.elapsed(), privacy: .public)")
        }
        loudest = max(loudest, level)
        levels = Meter.window(levels, adding: level, count: Self.bars)
        if !silent { gotSignal = true }
        if !gotSignal, Date().timeIntervalSince(startedAt) >= Self.silenceBeforeSwitch { skipSilentMicrophone() }
    }

    /// Waits once for a microphone whose format isn't ready (common right after access is granted), then moves on.
    private func retryOrSkip(_ mic: Microphone) {
        if formatRetried.insert(mic.id).inserted {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.begin() }
            return
        }
        tried.insert(mic.id)
        begin()
    }

    private func skipSilentMicrophone() {
        let silentName = microphone ?? "That microphone"
        voiceLog.notice("\(silentName, privacy: .public) sent pure silence, trying the next one")
        if let micID { tried.insert(micID) }
        teardown()
        begin()
        if listening, let microphone { note = "\(silentName) sent no sound, so I switched to \(microphone)." }
    }

    private func waitForPause() {
        pause?.invalidate()
        pause = Timer.scheduledTimer(withTimeInterval: heard.isEmpty ? 6 : 2.5, repeats: false) { [weak self] _ in
            self?.stop()
        }
    }

    private func teardown() {
        listening = false
        session = UUID()
        pause?.invalidate()
        engine.stop()
        engine.inputNode.removeTap(onBus: 0)
        request?.endAudio()
        task?.cancel()
        request = nil
        task = nil
    }

    func stop() {
        guard listening else { return }
        teardown()
        let text = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        voiceLog.notice("stopped, heard \(text.count) characters, loudest level \(self.loudest), \(self.buffers) buffers, \(self.droppedBuffers) before listening")
        if !text.isEmpty {
            onHeard?(text)
        } else {
            onProblem?(loudest < 0.15
                ? "I didn't hear anything from \(microphone ?? "the microphone"). Pick another one from the microphone menu."
                : "I heard sound but no words. Try again a little closer to the microphone.")
        }
    }
}

final class Speaker: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published var speaking = false
    @Published var kokoroStatus: KokoroEngine.Status
    static let preferenceKey = "voice"

    private let synth = AVSpeechSynthesizer()
    private let kokoro = KokoroEngine()
    private let audio = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let format = AVAudioFormat(standardFormatWithSampleRate: Double(KokoroTTS.Constants.samplingRate),
                                       channels: 1)!
    private var generation = 0
    private var queued = 0
    private var synthesized = false

    override init() {
        kokoroStatus = kokoro.status
        super.init()
        synth.delegate = self
        audio.attach(player)
        audio.connect(player, to: audio.mainMixerNode, format: format)
        kokoro.onStatus = { [weak self] in self?.kokoroStatus = $0 }
    }

    static var systemDefault: String? { AVSpeechSynthesisVoice(language: "en-US")?.identifier }

    static func installed() -> [VoiceInfo] {
        AVSpeechSynthesisVoice.speechVoices().map {
            VoiceInfo(id: $0.identifier, name: $0.name, language: $0.language, quality: $0.quality.rawValue,
                      novelty: $0.voiceTraits.contains(.isNoveltyVoice))
        }
    }

    private var all: [VoiceInfo] { (KokoroEngine.supported ? Kokoro.voices : []) + Self.installed() }

    var voices: [VoiceInfo] { Voices.usable(all, systemDefault: Self.systemDefault) }

    var current: VoiceInfo? {
        Voices.choose(all, preferred: UserDefaults.standard.string(forKey: Self.preferenceKey),
                      systemDefault: Self.systemDefault)
    }

    /// Starts the one-time Kokoro download when a Kokoro voice is in use.
    func prepare() {
        if current.map({ Kokoro.npzKey($0.id) != nil }) == true { kokoro.download() }
    }

    static let speedKey = "speechSpeed"

    var speed: Double {
        get { SpeechSpeed.clamped(UserDefaults.standard.object(forKey: Self.speedKey) as? Double ?? SpeechSpeed.defaultValue) }
        set {
            UserDefaults.standard.set(SpeechSpeed.clamped(newValue), forKey: Self.speedKey)
            objectWillChange.send()
        }
    }

    func setSpeed(_ value: Double) {
        speed = value
        say("This is how fast I'll talk.")
    }

    func use(_ voice: VoiceInfo) {
        UserDefaults.standard.set(voice.id, forKey: Self.preferenceKey)
        objectWillChange.send()
        prepare()
        say(Kokoro.npzKey(voice.id) != nil && !KokoroEngine.installed
            ? "I'm downloading that voice. Until it's ready, I'll sound like this."
            : "Hi, this is how I'll sound.")
    }

    func say(_ markdown: String) {
        stop()
        let text = Spoken.plain(markdown)
        guard let voice = current, Kokoro.npzKey(voice.id) != nil, KokoroEngine.installed else {
            prepare()
            sayWithApple(text)
            return
        }
        generation += 1
        let mine = generation
        queued = 0
        synthesized = false
        speaking = true
        kokoro.speak(text, voiceID: voice.id, speed: Float(speed), isCurrent: { [weak self] in self?.generation == mine },
                     chunk: { [weak self] samples in DispatchQueue.main.async { self?.play(samples, mine) } },
                     done: { [weak self] ok in
                         DispatchQueue.main.async {
                             guard let self, self.generation == mine else { return }
                             self.synthesized = true
                             if !ok && self.queued == 0 { self.sayWithApple(text) } else { self.finishIfDrained() }
                         }
                     })
    }

    private func play(_ samples: [Float], _ mine: Int) {
        guard generation == mine, !samples.isEmpty,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count))
        else { return }
        buffer.frameLength = buffer.frameCapacity
        samples.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        if !audio.isRunning { try? audio.start() }
        queued += 1
        player.scheduleBuffer(buffer) { [weak self] in
            DispatchQueue.main.async {
                guard let self, self.generation == mine else { return }
                self.queued -= 1
                self.finishIfDrained()
            }
        }
        if !player.isPlaying { player.play() }
    }

    private func finishIfDrained() {
        if synthesized && queued == 0 { speaking = false }
    }

    private func sayWithApple(_ text: String) {
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = SpeechSpeed.appleRate(speed, defaultRate: AVSpeechUtteranceDefaultSpeechRate,
                                               maximumRate: AVSpeechUtteranceMaximumSpeechRate)
        utterance.voice = current.flatMap { AVSpeechSynthesisVoice(identifier: $0.id) }
            ?? AVSpeechSynthesisVoice(language: "en-US")
        speaking = true
        synth.speak(utterance)
    }

    func stop() {
        generation += 1
        player.stop()
        synth.stopSpeaking(at: .immediate)
        speaking = false
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.speaking = false }
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        DispatchQueue.main.async { if self.queued == 0 { self.speaking = false } }
    }
}
