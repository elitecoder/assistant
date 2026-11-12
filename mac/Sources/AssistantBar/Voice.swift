import AudioToolbox
import AVFoundation
import ObjCExceptions
import KokoroSwift
import SessionCore
import os
import Speech

let voiceLog = Logger(subsystem: "com.assistant.sessions", category: "voice")

enum MicrophoneFailure: Error {
    /// No usable format yet, common right after access is granted.
    case notReady
    case failed(String)
}

/// A microphone ready to start, so tests can stand in for the real one.
protocol MicrophoneSource: AnyObject {
    var mic: Microphone { get }
    /// The device changed in a way the tap no longer fits, so the next press builds a new one.
    var stale: Bool { get }
    /// Starts the microphone; returns why it failed, if it did. `lost` runs, on any thread, if the microphone goes
    /// away or stops while running.
    func start(_ sink: @escaping (AVAudioPCMBuffer) -> Void, lost: @escaping () -> Void) -> String?
    /// Stops the microphone and prepares it again for the next press.
    func stop()
    func discard()
}

/// One AVAudioEngine bound to one microphone, with its tap installed and the engine prepared but not started, so a
/// press only has to start it. Building one takes 0.5 to 1.5 seconds, most of it waiting for the device switch.
final class MicrophoneInput: MicrophoneSource {
    let mic: Microphone
    let engine = AVAudioEngine()
    private let lock = NSLock()
    private var sink: ((AVAudioPCMBuffer) -> Void)?
    private var lost: (() -> Void)?
    private var _stale = false
    private var observer: NSObjectProtocol?

    private init(mic: Microphone) { self.mic = mic }

    deinit { observer.map(NotificationCenter.default.removeObserver) }

    var stale: Bool {
        lock.lock()
        defer { lock.unlock() }
        return _stale
    }

    static func make(_ mic: Microphone) -> Result<MicrophoneSource, MicrophoneFailure> {
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
            built?.changed(fits: Microphones.tapStillFits(tapped, device: InputDevice.inputShape(mic.id)))
        }
        return .success(built)
    }

    /// The device changed. A running engine stops itself on a change, so it restarts when the tap still fits and
    /// reports the microphone lost when it doesn't.
    private func changed(fits: Bool) {
        voiceLog.notice("\(self.mic.name, privacy: .public) changed configuration, \(fits ? "tap still fits" : "tap no longer fits", privacy: .public)")
        lock.lock()
        if !fits { _stale = true }
        let running = sink != nil
        let lost = lost
        lock.unlock()
        guard running else { return }
        var restarted = false
        if fits, !engine.isRunning {
            _ = AssistantCatchException { restarted = (try? self.engine.start()) != nil }
        }
        if !fits || (!engine.isRunning && !restarted) { lost?() }
    }

    private func deliver(_ buffer: AVAudioPCMBuffer) {
        lock.lock()
        let sink = sink
        lock.unlock()
        sink?(buffer)
    }

    func start(_ sink: @escaping (AVAudioPCMBuffer) -> Void, lost: @escaping () -> Void) -> String? {
        lock.lock()
        self.sink = sink
        self.lost = lost
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

    func stop() {
        lock.lock()
        sink = nil
        lost = nil
        lock.unlock()
        engine.stop()
        _ = AssistantCatchException { self.engine.prepare() }
    }

    func discard() {
        stop()
        _ = AssistantCatchException { self.engine.inputNode.removeTap(onBus: 0) }
    }
}

/// The speech recognizers a question can use, so tests can stand in for them.
protocol Recognizers: AnyObject {
    var state: SpeechModelState { get }
    var onState: ((SpeechModelState) -> Void)? { get set }
    var vocabulary: [String] { get set }
    /// Whether the next question needs speech recognition access (the older recognizer does).
    var needsSpeechAccess: Bool { get }
    func warm()
    func session(onText: @escaping (String) -> Void, onError: @escaping (NSError) -> Void) -> SpeechSession?
}

/// SpeechAnalyzer when its model is ready, SFSpeechRecognizer otherwise. A question that starts before the analyzer
/// is ready waits at most `analyzerWait` for it, then goes to the older recognizer with the audio heard so far.
final class LiveRecognizers: Recognizers {
    static let analyzerWait: TimeInterval = 1.5
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var model: AnyObject?
    var onState: ((SpeechModelState) -> Void)?
    var vocabulary = Vocabulary.terms {
        didSet { if #available(macOS 26, *) { (model as? AnalyzerModel)?.vocabulary = vocabulary } }
    }

    init() {
        if #available(macOS 26, *) {
            let model = AnalyzerModel()
            model.pool.onState = { [weak self] in self?.onState?($0) }
            self.model = model
        }
    }

    var state: SpeechModelState {
        if #available(macOS 26, *), let model = model as? AnalyzerModel { return model.pool.state }
        return .unavailable
    }

    var choice: SpeechRecognizerChoice { SpeechRecognizerChoice.choose(analyzerOS: model != nil, model: state) }

    var needsSpeechAccess: Bool { choice == .legacy }

    func warm() {
        if #available(macOS 26, *), let model = model as? AnalyzerModel { model.pool.warm() }
    }

    func session(onText: @escaping (String) -> Void, onError: @escaping (NSError) -> Void) -> SpeechSession? {
        session(limit: Self.analyzerWait, onText: onText, onError: onError)
    }

    /// `limit` nil waits as long as the analyzer takes to get ready, for a file.
    func session(limit: TimeInterval?, onText: @escaping (String) -> Void,
                 onError: @escaping (NSError) -> Void) -> SpeechSession? {
        let vocabulary = vocabulary
        let legacy = { [recognizer] () -> SpeechSession? in
            guard Listener.speechAccess == .granted else { return nil }
            return LegacySession(recognizer: recognizer, vocabulary: vocabulary, onText: onText, onError: onError)
        }
        if choice == .analyzer, #available(macOS 26, *), let model = model as? AnalyzerModel {
            voiceLog.notice("speech: using SpeechAnalyzer")
            let pool = model.pool
            return AnalyzerSession(take: { try await pool.take(within: limit) }, fallback: legacy, onText: onText,
                                   onError: onError)
        }
        voiceLog.notice("speech: using SFSpeechRecognizer")
        return LegacySession(recognizer: recognizer, vocabulary: vocabulary, onText: onText, onError: onError)
    }
}

/// Hears one spoken question, then hands its text to `onHeard` after a pause in speech.
/// A microphone that sends pure silence is skipped for the next one, the way call apps do.
final class Listener: ObservableObject {
    struct Dependencies {
        var access: () -> (mic: Access, speech: Access)
        var requestAccess: (_ needsSpeech: Bool, _ done: @escaping (_ mic: Bool, _ speech: Bool) -> Void) -> Void
        var choose: (_ excluding: Set<UInt32>) -> Microphone?
        var open: (Microphone) -> Result<MicrophoneSource, MicrophoneFailure>
        var recognizers: Recognizers
        /// Smart Turn and its voice detector, once downloaded; nil keeps the fixed pause timer.
        var turnModels: () -> OnnxTurn? = { nil }

        static func live() -> Dependencies {
            Dependencies(access: { (Listener.micAccess, Listener.speechAccess) }, requestAccess: { needsSpeech, done in
                AVCaptureDevice.requestAccess(for: .audio) { mic in
                    guard needsSpeech else { return done(mic, true) }
                    SFSpeechRecognizer.requestAuthorization { done(mic, $0 == .authorized) }
                }
            }, choose: { InputDevice.chosen(excluding: $0) }, open: MicrophoneInput.make, recognizers: LiveRecognizers(),
               turnModels: {
                TurnModels.shared.download()
                return TurnModels.shared.ready()
            })
        }
    }

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
    /// The microphone went away mid-question: what was heard so far, and a note saying so.
    var onInterrupted: ((_ heard: String, _ note: String) -> Void)?
    /// Listening has ended, whatever the outcome.
    var onEnded: (() -> Void)?

    var pauseAfterWords: TimeInterval = 2.5
    var pauseBeforeWords: TimeInterval = 6
    var formatRetryDelay: TimeInterval = 0.5

    static let disconnectedNote = "The microphone disconnected. What I heard is in the box: send it or clear it."

    /// Workspace titles and other words to bias recognition toward.
    var vocabulary: [String] {
        get { deps.recognizers.vocabulary }
        set { deps.recognizers.vocabulary = newValue }
    }

    private let deps: Dependencies
    private let audio = DispatchQueue(label: "assistant.microphone", qos: .userInteractive)
    /// Touched only on `audio`.
    private var prepared: MicrophoneSource?
    private var running: MicrophoneSource?
    private var speech: SpeechSession?
    /// Recognizers finishing earlier questions, kept alive until their final text arrives.
    private var closing: [UUID: SpeechSession] = [:]
    private var pause: Timer?
    /// Decides when the question is over from the sound of it; nil without Smart Turn.
    private var turn: TurnDetector?
    /// Smart Turn said the question is over before any words were recognized: stop as soon as they are.
    private var turnOver = false
    private var loudest: Float = 0
    private var micID: UInt32?
    private var tried = Set<UInt32>()
    private var formatRetried = Set<UInt32>()
    private var buffers = 0
    private var droppedBuffers = 0
    private var watch = SilenceWatch(allowance: 0.7)
    /// One microphone attempt; a skip or a retry starts another within the same question.
    private var session = UUID()
    /// One press.
    private var question = UUID()
    private var pressedAt = Date()

    init(_ deps: Dependencies = .live()) {
        self.deps = deps
        model = deps.recognizers.state
        deps.recognizers.onState = { [weak self] in self?.model = $0 }
    }

    func toggle() { listening ? stop() : start() }

    var choices: [String] { InputDevice.all().filter { !$0.virtual }.map(\.name) }

    var current: String? { listening ? microphone : deps.choose([])?.name }

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
        deps.recognizers.warm()
        _ = deps.turnModels()
        prewarm()
    }

    private func prewarm() {
        guard !listening, deps.access().mic == .granted, let mic = deps.choose([]) else { return }
        audio.async { _ = self.input(for: mic) }
    }

    /// The prepared input for `mic`, building one if needed. Runs on `audio`.
    private func input(for mic: Microphone) -> Result<MicrophoneSource, MicrophoneFailure> {
        if let prepared, prepared.mic.id == mic.id, !prepared.stale { return .success(prepared) }
        prepared?.discard()
        prepared = nil
        let started = Date()
        let made = deps.open(mic)
        if case .success(let input) = made {
            prepared = input
            voiceLog.notice("prepared \(mic.name, privacy: .public) in \(milliseconds(since: started), privacy: .public)")
        }
        return made
    }

    func start() {
        pressedAt = Date()
        voiceLog.notice("timing: pressed")
        question = UUID()
        tried = []
        formatRetried = []
        note = nil
        heard = ""
        soundArrived = false
        levels = [Float](repeating: 0, count: Self.bars)
        deps.recognizers.warm()
        let needsSpeech = deps.recognizers.needsSpeechAccess
        let access = deps.access()
        switch VoicePermissions.next(mic: access.mic, speech: access.speech, needsSpeech: needsSpeech) {
        case .problem(let text):
            end(problem: text)
        case .listen:
            listening = true
            begin()
        case .ask:
            listening = true
            let asked = question
            deps.requestAccess(needsSpeech) { mic, speech in
                DispatchQueue.main.async {
                    guard self.listening, self.question == asked else { return }
                    if !mic {
                        self.listening = false
                        self.end(problem: VoicePermissions.micProblem)
                    } else if !speech {
                        self.listening = false
                        self.end(problem: VoicePermissions.speechProblem)
                    } else {
                        self.begin()
                    }
                }
            }
        }
    }

    private func end(problem: String) {
        onProblem?(problem)
        onEnded?()
    }

    /// Starts the chosen microphone and a recognizer. `listening` is already true, so the panel shows the
    /// listening row from the press on.
    private func begin() {
        voiceLog.notice("timing: permissions done after \(self.elapsed(), privacy: .public)")
        speech?.cancel()
        speech = nil
        guard let mic = deps.choose(tried) else {
            listening = false
            end(problem: "None of your microphones sent any sound. Check them in System Settings, Sound.")
            return
        }
        let token = UUID()
        session = token
        micID = mic.id
        microphone = mic.name
        loudest = 0
        buffers = 0
        droppedBuffers = 0
        watch = SilenceWatch(allowance: Microphones.silenceAllowance(mic))
        guard let speech = makeSession(token) else {
            listening = false
            end(problem: "Speech recognition isn't available right now.")
            return
        }
        self.speech = speech
        let turn = deps.turnModels().map { models in
            TurnDetector(models: models) { [weak self] byModel in
                guard let self, self.session == token, self.listening else { return }
                guard !self.heard.isEmpty else {
                    voiceLog.notice("turn: over, but no words yet; stopping when they arrive")
                    self.turnOver = true
                    return
                }
                voiceLog.notice("turn: \(byModel ? "Smart Turn" : "the silence limit", privacy: .public) ended the question after \(self.elapsed(), privacy: .public)")
                self.stop()
            }
        }
        self.turn = turn
        turnOver = false
        audio.async {
            let result = self.input(for: mic)
            var failure: MicrophoneFailure?
            switch result {
            case .failure(let f):
                failure = f
            case .success(let input):
                let reason = input.start({ [weak self, weak speech] buffer in
                    guard let self else { return }
                    let (mono, level, silent) = Self.measure(buffer)
                    if let mono {
                        speech?.append(mono)
                        turn?.append(mono)
                    }
                    DispatchQueue.main.async { self.took(level: level, silent: silent, token) }
                }, lost: { [weak self] in
                    DispatchQueue.main.async { self?.lost(token) }
                })
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
            if self.turnOver && !text.isEmpty { return self.stop() }
            self.waitForPause()
        }
        let onError = { [weak self] (error: NSError) in
            guard let self, self.session == token, self.listening else { return }
            voiceLog.error("recognition error: \(error.domain, privacy: .public) \(error.code)")
            self.teardown()?.cancel()
            self.end(problem: Spoken.problem(domain: error.domain, code: error.code, description: error.localizedDescription))
        }
        return deps.recognizers.session(onText: onText, onError: onError)
    }

    private func started(_ mic: Microphone, _ token: UUID, _ failure: MicrophoneFailure?) {
        guard session == token, listening else { return }
        switch failure {
        case .notReady?:
            speech?.cancel()
            speech = nil
            retryOrSkip(mic, token)
        case .failed(let reason)?:
            voiceLog.error("microphone \(mic.name, privacy: .public) failed: \(reason, privacy: .public)")
            tried.insert(mic.id)
            begin()
        case nil:
            voiceLog.notice("listening on \(mic.name, privacy: .public) after \(self.elapsed(), privacy: .public)")
            watch.started(at: Date())
            DispatchQueue.main.asyncAfter(deadline: .now() + watch.allowance + 0.3) { [weak self] in
                guard let self, self.session == token, self.listening, self.buffers == 0 else { return }
                voiceLog.notice("\(mic.name, privacy: .public) delivered no audio, trying the next one")
                self.skipSilentMicrophone()
            }
        }
    }

    /// The buffer for the recognizer (every channel mixed into one), and the meter level of its loudest channel.
    static func measure(_ buffer: AVAudioPCMBuffer) -> (mono: AVAudioPCMBuffer?, level: Float, silent: Bool) {
        guard let data = buffer.floatChannelData else { return (buffer, 0, false) }
        let frames = Int(buffer.frameLength)
        let channels = (0..<Int(buffer.format.channelCount)).map { UnsafeBufferPointer(start: data[$0], count: frames) }
        let level = Meter.loudest(channels)
        let silent = channels.allSatisfy { $0.allSatisfy { $0 == 0 } }
        guard channels.count > 1 else { return (buffer, level, silent) }
        guard let format = AVAudioFormat(standardFormatWithSampleRate: buffer.format.sampleRate, channels: 1),
              let mono = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: buffer.frameLength) else { return (nil, level, silent) }
        mono.frameLength = buffer.frameLength
        Meter.mono(channels, into: UnsafeMutableBufferPointer(start: mono.floatChannelData![0], count: frames))
        return (mono, level, silent)
    }

    private func took(level: Float, silent: Bool, _ token: UUID) {
        guard session == token else { return }
        guard listening else {
            droppedBuffers += 1
            return
        }
        let verdict = watch.took(silent: silent, at: Date())
        guard verdict != .ignored else { return }
        buffers += 1
        if buffers == 1 { voiceLog.notice("timing: first audio after \(self.elapsed(), privacy: .public)") }
        loudest = max(loudest, level)
        levels = Meter.window(levels, adding: level, count: Self.bars)
        switch verdict {
        case .firstSound:
            soundArrived = true
            voiceLog.notice("timing: first sound after \(self.elapsed(), privacy: .public)")
        case .skip:
            skipSilentMicrophone()
        case .ignored, .waiting, .sound:
            break
        }
    }

    /// Waits once for a microphone whose format isn't ready (common right after access is granted), then moves on.
    private func retryOrSkip(_ mic: Microphone, _ token: UUID) {
        if formatRetried.insert(mic.id).inserted {
            DispatchQueue.main.asyncAfter(deadline: .now() + formatRetryDelay) { [weak self] in
                guard let self, self.listening, self.session == token else { return }
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

    /// The running microphone went away or stopped: end the question and keep what was heard for the operator.
    private func lost(_ token: UUID) {
        guard session == token, listening else { return }
        voiceLog.notice("\(self.microphone ?? "the microphone", privacy: .public) disconnected mid-question")
        audio.async {
            self.prepared?.discard()
            self.prepared = nil
        }
        close { [weak self] text in
            guard let self else { return }
            self.onInterrupted?(text, Self.disconnectedNote)
            self.onEnded?()
        }
    }

    private func waitForPause() {
        pause?.invalidate()
        // With Smart Turn deciding, the timer after words is only a backstop beyond its own silence limit.
        let afterWords = TurnEnd.backstop(timer: pauseAfterWords, smartTurn: turn != nil)
        pause = Timer.scheduledTimer(withTimeInterval: heard.isEmpty ? pauseBeforeWords : afterWords,
                                     repeats: false) { [weak self] _ in
            self?.stop()
        }
    }

    /// Stops the microphone and returns the recognizer, for the caller to finish or cancel.
    private func teardown() -> SpeechSession? {
        listening = false
        session = UUID()
        pause?.invalidate()
        turn = nil
        audio.async {
            self.running?.stop()
            self.running = nil
        }
        let speech = speech
        self.speech = nil
        return speech
    }

    /// Ends the question and gets its final text. A later press doesn't get this question's text or state.
    private func close(_ deliver: @escaping (String) -> Void) {
        let speech = teardown()
        let mine = question
        let heardSoFar = heard
        let stoppedAt = Date()
        finishing = true
        let done = { [weak self] (final: String) in
            guard let self else { return }
            self.closing[mine] = nil
            let text = (final.isEmpty ? heardSoFar : final).trimmingCharacters(in: .whitespacesAndNewlines)
            voiceLog.notice("final text (\(text.count) characters) after \(milliseconds(since: stoppedAt), privacy: .public)")
            self.finishing = self.closing[self.question] != nil
            if self.question == mine {
                self.heard = text
                self.prewarm()
            } else {
                voiceLog.notice("the final text arrived after the next question began")
            }
            deliver(text)
        }
        guard let speech else { return done("") }
        closing[mine] = speech
        speech.finish(.live, done)
    }

    /// Ends listening without asking anything: what was heard so far is dropped.
    func cancel() {
        guard listening else { return }
        voiceLog.notice("cancelled, dropping \(self.heard.count) characters")
        teardown()?.cancel()
        heard = ""
        onEnded?()
    }

    func stop() {
        guard listening else { return }
        voiceLog.notice("stopped, heard \(self.heard.count) characters so far, loudest level \(self.loudest), \(self.buffers) buffers, \(self.droppedBuffers) before listening")
        let loudest = loudest
        let microphone = microphone
        close { [weak self] text in
            guard let self else { return }
            if !text.isEmpty {
                self.onHeard?(text)
            } else {
                self.onProblem?(loudest < 0.15
                    ? "I didn't hear anything from \(microphone ?? "the microphone"). Pick another one from the microphone menu."
                    : "I heard sound but no words. Try again a little closer to the microphone.")
            }
            self.onEnded?()
        }
    }

    /// Transcribes a sound file through the same recognizer path a spoken question takes, for checking accuracy.
    /// Reads the file a little at a time, and waits for the recognizer to finish all of it.
    static func transcribe(_ url: URL, legacy: Bool, vocabulary: [String], done: @escaping (String) -> Void) -> String? {
        guard let file = try? AVAudioFile(forReading: url) else { return "Couldn't read \(url.path)." }
        let failed = { (error: NSError) in done("error: \(error.domain) \(error.code) \(error.localizedDescription)") }
        let session: SpeechSession?
        if legacy {
            session = LegacySession(recognizer: SFSpeechRecognizer(locale: Locale(identifier: "en-US")), vocabulary: vocabulary,
                                    onText: { _ in }, onError: failed)
        } else {
            let recognizers = LiveRecognizers()
            recognizers.vocabulary = vocabulary
            recognizers.warm()
            session = recognizers.session(limit: nil, onText: { _ in }, onError: failed)
        }
        guard let session else { return "Speech recognition isn't available." }
        let chunk = AVAudioFrameCount(file.processingFormat.sampleRate / 10)
        DispatchQueue.global().async {
            while let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: chunk),
                  (try? file.read(into: buffer)) != nil, buffer.frameLength > 0 {
                session.append(buffer)
                while session.pending > 50 { Thread.sleep(forTimeInterval: 0.01) }
            }
            DispatchQueue.main.async {
                session.finish(.none) { text in
                    withExtendedLifetime(session) { done(text) }
                }
            }
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
    /// Buffers scheduled on the player and not finished: Assistant's audio is playing while this is above zero.
    private var queued = 0 {
        didSet { playingNow.set(queued > 0) }
    }
    /// The same, for the microphone tap's thread.
    private let playingNow = Flag()
    /// Kokoro syntheses still running for the current reply.
    private var pending = 0
    /// The Apple utterance playing now; callbacks for an earlier one, cut off by a replay or a sample, are ignored.
    var appleUtterance: AVSpeechUtterance?
    /// False while the microphone is open: a voice or speed sample would play into it.
    var mayTalk: () -> Bool = { true }
    /// True while this engine's echo-cancelled microphone watches for the operator talking over the answer.
    private(set) var watching = false
    private var appleConverter: AVAudioConverter?

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

    var kokoroSpeed: Float { Float(speed) }

    func setSpeed(_ value: Double) {
        speed = value
        sample("This is how fast I'll talk.")
    }

    private func sample(_ text: String) {
        if mayTalk() { say(text) }
    }

    func use(_ voice: VoiceInfo) {
        UserDefaults.standard.set(voice.id, forKey: Self.preferenceKey)
        objectWillChange.send()
        prepare()
        sample(Kokoro.npzKey(voice.id) != nil && !KokoroEngine.installed
            ? "I'm downloading that voice. Until it's ready, I'll sound like this."
            : "Hi, this is how I'll sound.")
    }

    /// Speaks `markdown` from the start, cutting off anything still playing.
    func say(_ markdown: String) {
        stop()
        queued = 0
        pending = 0
        speak(Spoken.plain(markdown))
    }

    /// Speaks `markdown` after what's already playing, for a reply that arrives a sentence at a time.
    func append(_ markdown: String) {
        speaking ? speak(Spoken.plain(markdown)) : say(markdown)
    }

    private func speak(_ text: String) {
        guard let voice = current, Kokoro.npzKey(voice.id) != nil, KokoroEngine.installed else {
            prepare()
            sayWithApple(text)
            return
        }
        let mine = generation
        pending += 1
        speaking = true
        kokoro.speak(text, voiceID: voice.id, speed: kokoroSpeed, isCurrent: { [weak self] in self?.generation == mine },
                     chunk: { [weak self] samples in DispatchQueue.main.async { self?.play(samples, mine) } },
                     done: { [weak self] ok in
                         DispatchQueue.main.async {
                             guard let self, self.generation == mine else { return }
                             self.pending -= 1
                             if !ok && self.queued == 0 && self.pending == 0 { self.sayWithApple(text) } else { self.finishIfDrained() }
                         }
                     })
    }

    private func play(_ samples: [Float], _ mine: Int) {
        guard generation == mine, !samples.isEmpty,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count))
        else { return }
        buffer.frameLength = buffer.frameCapacity
        samples.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        play(buffer, mine)
    }

    private func play(_ buffer: AVAudioPCMBuffer, _ mine: Int) {
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
        if pending == 0 && queued == 0 && appleUtterance == nil { speaking = false }
    }

    /// `text` as an Apple utterance at the chosen voice and speed.
    func utterance(_ text: String) -> AVSpeechUtterance {
        let utterance = AVSpeechUtterance(string: text)
        utterance.rate = SpeechSpeed.appleRate(speed, defaultRate: AVSpeechUtteranceDefaultSpeechRate,
                                               maximumRate: AVSpeechUtteranceMaximumSpeechRate)
        utterance.voice = current.flatMap { AVSpeechSynthesisVoice(identifier: $0.id) }
            ?? AVSpeechSynthesisVoice(language: "en-US")
        return utterance
    }

    private func sayWithApple(_ text: String) {
        let utterance = utterance(text)
        speaking = true
        guard watching else {
            appleUtterance = utterance
            synth.speak(utterance)
            return
        }
        // Played through this engine, so echo cancellation knows to remove it from the microphone.
        let mine = generation
        pending += 1
        synth.write(utterance) { [weak self] buffer in
            guard let pcm = buffer as? AVAudioPCMBuffer else { return }
            let last = pcm.frameLength == 0
            let converted = last ? nil : self?.convertApple(pcm)
            DispatchQueue.main.async {
                guard let self, self.generation == mine else { return }
                if let converted { self.play(converted, mine) }
                if last {
                    self.pending -= 1
                    self.finishIfDrained()
                }
            }
        }
    }

    private func convertApple(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        if appleConverter?.inputFormat != buffer.format { appleConverter = AVAudioConverter(from: buffer.format, to: format) }
        guard let converter = appleConverter else { return nil }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * format.sampleRate / buffer.format.sampleRate) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }
        var consumed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return buffer
        }
        return out.frameLength > 0 ? out : nil
    }

    /// Opens this engine's microphone with Apple's voice processing, which cancels the answer this engine plays,
    /// and calls `onBargeIn` when the operator talks over it. Nothing it hears is transcribed. Voice processing uses
    /// the Mac's default input and output devices. False when it can't start.
    func watchForBargeIn(_ onBargeIn: @escaping () -> Void) -> Bool {
        guard !watching else { return true }
        audio.stop()
        let input = audio.inputNode
        var started = false
        let raised = AssistantCatchException {
            do {
                try input.setVoiceProcessingEnabled(true)
                guard let mono = AVAudioFormat(standardFormatWithSampleRate: input.outputFormat(forBus: 0).sampleRate,
                                               channels: 1) else { return }
                var watch = BargeInWatch()
                let playing = self.playingNow
                var buffers = 0
                input.installTap(onBus: 0, bufferSize: 4800, format: mono) { buffer, _ in
                    guard let data = buffer.floatChannelData?[0] else { return }
                    let level = Meter.level(UnsafeBufferPointer(start: data, count: Int(buffer.frameLength)))
                    buffers += 1
                    if buffers % 50 == 1 { voiceLog.notice("barge-in: echo-cancelled level \(level)") }
                    if watch.take(level: level, duration: Double(buffer.frameLength) / buffer.format.sampleRate,
                                  playing: playing.value) {
                        voiceLog.notice("barge-in: the operator is talking over the answer")
                        DispatchQueue.main.async(execute: onBargeIn)
                    }
                }
                self.audio.prepare()
                try self.audio.start()
                started = true
            } catch {
                voiceLog.error("barge-in: voice processing failed: \(error.localizedDescription, privacy: .public)")
            }
        }
        if let raised { voiceLog.error("barge-in: \(raised.name.rawValue, privacy: .public): \(raised.reason ?? "", privacy: .public)") }
        watching = started && raised == nil
        if !watching { stopWatching(force: true) }
        voiceLog.notice("barge-in: watching \(self.watching ? "on" : "off", privacy: .public)")
        return watching
    }

    func stopWatching(force: Bool = false) {
        guard watching || force else { return }
        watching = false
        _ = AssistantCatchException {
            self.audio.inputNode.removeTap(onBus: 0)
            self.audio.stop()
            try? self.audio.inputNode.setVoiceProcessingEnabled(false)
        }
    }

    func stop() {
        generation += 1
        queued = 0
        pending = 0
        appleUtterance = nil
        player.stop()
        synth.stopSpeaking(at: .immediate)
        speaking = false
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        DispatchQueue.main.async {
            guard u === self.appleUtterance else { return }
            self.appleUtterance = nil
            self.finishIfDrained()
        }
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
        DispatchQueue.main.async { if u === self.appleUtterance && self.queued == 0 { self.speaking = false } }
    }
}

/// A Bool shared across threads.
final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var _value = false
    var value: Bool { lock.withLock { _value } }
    func set(_ new: Bool) { lock.withLock { _value = new } }
}
