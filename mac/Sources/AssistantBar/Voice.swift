import AudioToolbox
import AVFoundation
import ObjCExceptions
import KokoroSwift
import SessionCore
import os
import Speech

let voiceLog = Logger(subsystem: "com.assistant.sessions", category: "voice")

/// One AVAudioEngine bound to one microphone, with its tap installed and the engine prepared but not started, so a
/// press only has to start it. Building one takes 0.5 to 1.5 seconds, most of it waiting for the device switch.
final class MicrophoneInput {
    enum Failure: Error {
        /// No usable format yet, common right after access is granted.
        case notReady
        case failed(String)
    }

    let mic: Microphone
    let engine = AVAudioEngine()
    private(set) var stale = false
    private let lock = NSLock()
    private var sink: ((AVAudioPCMBuffer) -> Void)?
    private var observer: NSObjectProtocol?

    private init(mic: Microphone) { self.mic = mic }

    deinit { observer.map(NotificationCenter.default.removeObserver) }

    static func make(_ mic: Microphone) -> Result<MicrophoneInput, Failure> {
        let built = MicrophoneInput(mic: mic)
        let input = built.engine.inputNode
        if let unit = input.audioUnit {
            var device = AudioDeviceID(mic.id)
            let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                              &device, UInt32(MemoryLayout<AudioDeviceID>.size))
            if status != noErr { voiceLog.error("couldn't select \(mic.name, privacy: .public): \(status)") }
        }
        let reported = input.outputFormat(forBus: 0)
        let device = InputDevice.inputShape(mic.id)
        guard let shape = Microphones.tapShape(device: device, engine: AudioShape(sampleRate: reported.sampleRate,
                                                                                  channels: reported.channelCount)),
              let format = AVAudioFormat(standardFormatWithSampleRate: shape.sampleRate, channels: shape.channels) else {
            voiceLog.error("\(mic.name, privacy: .public) has no usable format yet (\(reported.sampleRate) Hz, \(reported.channelCount) ch)")
            return .failure(.notReady)
        }
        if format.sampleRate != reported.sampleRate {
            voiceLog.notice("\(mic.name, privacy: .public) runs at \(format.sampleRate) Hz, not the \(reported.sampleRate) Hz the engine reports")
        }
        let raised = AssistantCatchException {
            input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak built] buffer, _ in built?.deliver(buffer) }
            built.engine.prepare()
        }
        if let raised {
            return .failure(.failed("\(raised.name.rawValue): \(raised.reason ?? "")"))
        }
        let tapped = AudioShape(sampleRate: format.sampleRate, channels: format.channelCount)
        built.observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                                                                object: built.engine, queue: nil) { [weak built] _ in
            let now = InputDevice.inputShape(mic.id)
            let fits = Microphones.tapStillFits(tapped, device: now)
            voiceLog.notice("\(mic.name, privacy: .public) changed configuration (\(now?.sampleRate ?? 0) Hz), \(fits ? "tap still fits" : "rebuilding", privacy: .public)")
            if !fits { built?.stale = true }
        }
        return .success(built)
    }

    private func deliver(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        let sink = sink
        lock.unlock()
        sink?(buffer)
    }

    /// Starts the microphone; returns why it failed, if it did.
    func start(_ sink: @escaping (AVAudioPCMBuffer) -> Void) -> String? {
        lock.lock()
        self.sink = sink
        lock.unlock()
        var startError: Error?
        let raised = AssistantCatchException {
            do { try self.engine.start() } catch { startError = error }
        }
        guard let reason = raised.map({ "\($0.name.rawValue): \($0.reason ?? "")" }) ?? startError?.localizedDescription
        else { return nil }
        stop()
        return reason
    }

    /// Stops the microphone and prepares it again for the next press.
    func stop() {
        lock.lock()
        sink = nil
        lock.unlock()
        engine.stop()
        _ = AssistantCatchException { self.engine.prepare() }
    }

    func discard() {
        stop()
        _ = AssistantCatchException { self.engine.inputNode.removeTap(onBus: 0) }
    }
}

/// Hears one spoken question, then hands its text to `onHeard` after a pause in speech.
/// A microphone that sends pure silence is skipped for the next one, the way call apps do.
final class Listener: ObservableObject {
    @Published var listening = false
    @Published var soundArrived = false
    @Published var finishing = false
    @Published var heard = ""
    @Published var levels = [Float](repeating: 0, count: Listener.bars)
    @Published var microphone: String?
    @Published var note: String?
    @Published var model = SpeechModelState.checking
    static let bars = 36
    var onHeard: ((String) -> Void)?
    var onProblem: ((String) -> Void)?

    /// Workspace titles and other words to bias recognition toward.
    var vocabulary = Vocabulary.terms {
        didSet { if #available(macOS 26, *) { (analyzerModel as? AnalyzerModel)?.vocabulary = vocabulary } }
    }

    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private let audio = DispatchQueue(label: "assistant.microphone", qos: .userInteractive)
    /// Touched only on `audio`.
    private var prepared: MicrophoneInput?
    private var running: MicrophoneInput?
    private var speech: SpeechSession?
    /// The recognizer finishing the last question, kept alive until its final text arrives.
    private var closing: SpeechSession?
    private var pause: Timer?
    private var loudest: Float = 0
    private var micID: UInt32?
    private var tried = Set<UInt32>()
    private var formatRetried = Set<UInt32>()
    private var buffers = 0
    private var droppedBuffers = 0
    private var startedAt = Date()
    private var gotSignal = false
    private var silenceAllowance: TimeInterval = 0.7
    private var session = UUID()
    private var pressedAt = Date()

    private lazy var analyzerModel: AnyObject? = {
        guard #available(macOS 26, *) else { return nil }
        let model = AnalyzerModel()
        model.vocabulary = vocabulary
        model.onState = { [weak self] in self?.model = $0 }
        return model
    }()

    private var analyzerOS: Bool {
        if #available(macOS 26, *) { return true }
        return false
    }

    var choice: SpeechRecognizerChoice { SpeechRecognizerChoice.choose(analyzerOS: analyzerOS, model: model) }

    func toggle() { listening ? stop() : start() }

    var choices: [String] { InputDevice.all().filter { !$0.virtual }.map(\.name) }

    var current: String? { listening ? microphone : InputDevice.chosen()?.name }

    func use(_ name: String) {
        InputDevice.preferred = name
        note = nil
        objectWillChange.send()
        guard listening else { return prewarm() }
        teardown()?.cancel()
        tried = []
        listening = true
        begin()
    }

    private func elapsed() -> String { String(format: "%.0f ms", Date().timeIntervalSince(pressedAt) * 1000) }

    static var micAccess: Access {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: .granted
        case .notDetermined: .undetermined
        default: .denied
        }
    }

    static var speechAccess: Access {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: .granted
        case .notDetermined: .undetermined
        default: .denied
        }
    }

    /// Gets the speech model and the chosen microphone ready without starting either, so the next press is quick.
    func prepare() {
        if #available(macOS 26, *) { (analyzerModel as? AnalyzerModel)?.warm() }
        prewarm()
    }

    private func prewarm() {
        guard !listening, Self.micAccess == .granted, let mic = InputDevice.chosen() else { return }
        audio.async { _ = self.input(for: mic) }
    }

    /// The prepared input for `mic`, building one if needed. Runs on `audio`.
    private func input(for mic: Microphone) -> Result<MicrophoneInput, MicrophoneInput.Failure> {
        if let prepared, prepared.mic.id == mic.id, !prepared.stale { return .success(prepared) }
        prepared?.discard()
        prepared = nil
        let started = Date()
        let made = MicrophoneInput.make(mic)
        if case .success(let input) = made {
            prepared = input
            voiceLog.notice("prepared \(mic.name, privacy: .public) in \(milliseconds(since: started), privacy: .public)")
        }
        return made
    }

    func start() {
        pressedAt = Date()
        voiceLog.notice("timing: pressed")
        tried = []
        formatRetried = []
        note = nil
        heard = ""
        soundArrived = false
        levels = [Float](repeating: 0, count: Self.bars)
        let needsSpeech = choice == .legacy
        switch VoicePermissions.next(mic: Self.micAccess, speech: Self.speechAccess, needsSpeech: needsSpeech) {
        case .problem(let text):
            onProblem?(text)
        case .listen:
            listening = true
            begin()
        case .ask:
            listening = true
            AVCaptureDevice.requestAccess(for: .audio) { mic in
                let next = { (speech: SFSpeechRecognizerAuthorizationStatus?) in
                    DispatchQueue.main.async {
                        guard self.listening else { return }
                        if !mic {
                            self.listening = false
                            self.onProblem?(VoicePermissions.micProblem)
                        } else if let speech, speech != .authorized {
                            self.listening = false
                            self.onProblem?(VoicePermissions.speechProblem)
                        } else {
                            self.begin()
                        }
                    }
                }
                if needsSpeech { SFSpeechRecognizer.requestAuthorization { next($0) } } else { next(nil) }
            }
        }
    }

    /// Starts the chosen microphone and a recognizer. `listening` is already true, so the panel shows the
    /// listening row from the press on.
    private func begin() {
        voiceLog.notice("timing: permissions done after \(self.elapsed(), privacy: .public)")
        guard let mic = InputDevice.chosen(excluding: tried) else {
            listening = false
            onProblem?("None of your microphones sent any sound. Check them in System Settings, Sound.")
            return
        }
        let token = UUID()
        session = token
        micID = mic.id
        microphone = mic.name
        loudest = 0
        buffers = 0
        droppedBuffers = 0
        gotSignal = false
        guard let speech = makeSession(token) else {
            listening = false
            onProblem?("Speech recognition isn't available right now.")
            return
        }
        self.speech = speech
        audio.async {
            let result = self.input(for: mic)
            var failure: MicrophoneInput.Failure?
            switch result {
            case .failure(let f):
                failure = f
            case .success(let input):
                let reason = input.start { [weak self, weak speech] buffer in
                    speech?.append(buffer)
                    self?.measure(buffer, token)
                }
                if let reason {
                    failure = .failed(reason)
                    input.discard()
                    self.prepared = nil
                } else {
                    self.running = input
                }
            }
            DispatchQueue.main.async { self.started(mic, token, failure) }
        }
        waitForPause()
    }

    private func makeSession(_ token: UUID) -> SpeechSession? {
        let onText = { [weak self] (text: String) in
            guard let self, self.session == token, self.listening else { return }
            self.heard = text
            self.waitForPause()
        }
        let onError = { [weak self] (error: NSError) in
            guard let self, self.session == token, self.listening else { return }
            voiceLog.error("recognition error: \(error.domain, privacy: .public) \(error.code)")
            self.teardown()?.cancel()
            self.onProblem?(Spoken.problem(domain: error.domain, code: error.code, description: error.localizedDescription))
        }
        if choice == .analyzer, #available(macOS 26, *), let model = analyzerModel as? AnalyzerModel {
            voiceLog.notice("speech: using SpeechAnalyzer")
            model.warm()
            return AnalyzerSession(model: model, onText: onText, onError: onError)
        }
        voiceLog.notice("speech: using SFSpeechRecognizer")
        return LegacySession(recognizer: recognizer, vocabulary: vocabulary, onText: onText, onError: onError)
    }

    private func started(_ mic: Microphone, _ token: UUID, _ failure: MicrophoneInput.Failure?) {
        guard session == token, listening else { return }
        switch failure {
        case .notReady?:
            speech?.cancel()
            speech = nil
            retryOrSkip(mic)
        case .failed(let reason)?:
            voiceLog.error("microphone \(mic.name, privacy: .public) failed: \(reason, privacy: .public)")
            speech?.cancel()
            speech = nil
            tried.insert(mic.id)
            begin()
        case nil:
            voiceLog.notice("listening on \(mic.name, privacy: .public) after \(self.elapsed(), privacy: .public)")
            startedAt = Date()
            silenceAllowance = Microphones.silenceAllowance(mic)
            DispatchQueue.main.asyncAfter(deadline: .now() + silenceAllowance + 0.3) { [weak self] in
                guard let self, self.session == token, self.listening, self.buffers == 0 else { return }
                voiceLog.notice("\(mic.name, privacy: .public) delivered no audio, trying the next one")
                self.skipSilentMicrophone()
            }
        }
    }

    private func measure(_ buffer: AVAudioPCMBuffer, _ token: UUID) {
        guard let channel = buffer.floatChannelData?[0] else { return }
        let samples = UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength))
        let level = Meter.level(samples)
        let silent = samples.allSatisfy { $0 == 0 }
        DispatchQueue.main.async {
            guard self.session == token else { return }
            self.took(level: level, silent: silent)
        }
    }

    private func took(level: Float, silent: Bool) {
        guard listening else {
            droppedBuffers += 1
            return
        }
        buffers += 1
        if buffers == 1 { voiceLog.notice("timing: first audio after \(self.elapsed(), privacy: .public)") }
        loudest = max(loudest, level)
        levels = Meter.window(levels, adding: level, count: Self.bars)
        if !silent, !gotSignal {
            gotSignal = true
            soundArrived = true
            voiceLog.notice("timing: first sound after \(self.elapsed(), privacy: .public)")
        }
        if !gotSignal, Date().timeIntervalSince(startedAt) >= silenceAllowance { skipSilentMicrophone() }
    }

    /// Waits once for a microphone whose format isn't ready (common right after access is granted), then moves on.
    private func retryOrSkip(_ mic: Microphone) {
        if formatRetried.insert(mic.id).inserted {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                guard let self, self.listening else { return }
                self.begin()
            }
            return
        }
        tried.insert(mic.id)
        begin()
    }

    private func skipSilentMicrophone() {
        let silentName = microphone ?? "That microphone"
        voiceLog.notice("\(silentName, privacy: .public) sent pure silence, trying the next one")
        if let micID { tried.insert(micID) }
        teardown()?.cancel()
        listening = true
        soundArrived = false
        begin()
        if listening, let microphone { note = "\(silentName) sent no sound, so I switched to \(microphone)." }
    }

    private func waitForPause() {
        pause?.invalidate()
        pause = Timer.scheduledTimer(withTimeInterval: heard.isEmpty ? 6 : 2.5, repeats: false) { [weak self] _ in
            self?.stop()
        }
    }

    /// Stops the microphone and returns the recognizer, for the caller to finish or cancel.
    private func teardown() -> SpeechSession? {
        listening = false
        session = UUID()
        pause?.invalidate()
        audio.async {
            self.running?.stop()
            self.running = nil
        }
        let speech = speech
        self.speech = nil
        return speech
    }

    func stop() {
        guard listening else { return }
        let speech = teardown()
        voiceLog.notice("stopped, heard \(self.heard.count) characters so far, loudest level \(self.loudest), \(self.buffers) buffers, \(self.droppedBuffers) before listening")
        let stoppedAt = Date()
        finishing = true
        let deliver = { [weak self] (final: String) in
            guard let self else { return }
            self.closing = nil
            self.finishing = false
            let text = (final.isEmpty ? self.heard : final).trimmingCharacters(in: .whitespacesAndNewlines)
            voiceLog.notice("final text (\(text.count) characters) after \(milliseconds(since: stoppedAt), privacy: .public)")
            self.heard = text
            if !text.isEmpty {
                self.onHeard?(text)
            } else {
                self.onProblem?(self.loudest < 0.15
                    ? "I didn't hear anything from \(self.microphone ?? "the microphone"). Pick another one from the microphone menu."
                    : "I heard sound but no words. Try again a little closer to the microphone.")
            }
            self.prewarm()
        }
        closing = speech
        if let speech { speech.finish(deliver) } else { deliver("") }
    }

    /// Transcribes a sound file through the same recognizer path a spoken question takes, for checking accuracy.
    static func transcribe(_ url: URL, legacy: Bool, vocabulary: [String], done: @escaping (String) -> Void) -> String? {
        guard let file = try? AVAudioFile(forReading: url) else { return "Couldn't read \(url.path)." }
        let failed = { (error: NSError) in done("error: \(error.domain) \(error.code) \(error.localizedDescription)") }
        let session: SpeechSession?
        if !legacy, #available(macOS 26, *) {
            let model = AnalyzerModel()
            model.vocabulary = vocabulary
            model.warm()
            session = AnalyzerSession(model: model, onText: { _ in }, onError: failed)
        } else {
            session = LegacySession(recognizer: SFSpeechRecognizer(locale: Locale(identifier: "en-US")), vocabulary: vocabulary,
                                    onText: { _ in }, onError: failed)
        }
        guard let session else { return "Speech recognition isn't available." }
        let chunk = AVAudioFrameCount(file.processingFormat.sampleRate / 10)
        while let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: chunk),
              (try? file.read(into: buffer)) != nil, buffer.frameLength > 0 {
            session.append(buffer)
        }
        session.finish { text in
            withExtendedLifetime(session) { done(text) }
        }
        return nil
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
