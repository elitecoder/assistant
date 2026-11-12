import AVFoundation
import Speech
import XCTest
@testable import AssistantBar
@testable import SessionCore

// Stand-ins for the microphone and the recognizers, so the Listener's states can be driven without audio.

final class FakeSession: SpeechSession {
    let onText: (String) -> Void
    let onError: (NSError) -> Void
    private let lock = NSLock()
    private var _appended: [AVAudioPCMBuffer] = []
    var finishing: ((String) -> Void)?
    var cancelled = false
    var finished = false
    let pending = 0

    init(onText: @escaping (String) -> Void, onError: @escaping (NSError) -> Void) {
        self.onText = onText
        self.onError = onError
    }

    var appended: [AVAudioPCMBuffer] { lock.withLock { _appended } }
    func append(_ buffer: AVAudioPCMBuffer) { lock.withLock { _appended.append(buffer) } }

    func finish(_ limit: FinishLimit, _ done: @escaping (String) -> Void) {
        finished = true
        finishing = done
    }

    func cancel() { cancelled = true }
}

final class FakeRecognizers: Recognizers {
    var state = SpeechModelState.ready
    var onState: ((SpeechModelState) -> Void)?
    var vocabulary: [String] = []
    let needsSpeechAccess = false
    var sessions: [FakeSession] = []

    func warm() {}

    func session(onText: @escaping (String) -> Void, onError: @escaping (NSError) -> Void) -> SpeechSession? {
        let session = FakeSession(onText: onText, onError: onError)
        sessions.append(session)
        return session
    }
}

final class FakeMic: MicrophoneSource {
    let mic: Microphone
    let stale = false
    var sink: ((AVAudioPCMBuffer) -> Void)?
    var lost: (() -> Void)?
    /// Runs inside `start`, before it returns, the way a real tap can fire while the engine starts.
    var duringStart: ((FakeMic) -> Void)?

    init(_ mic: Microphone) { self.mic = mic }

    func start(_ sink: @escaping (AVAudioPCMBuffer) -> Void, lost: @escaping () -> Void) -> String? {
        self.sink = sink
        self.lost = lost
        duringStart?(self)
        return nil
    }

    func stop() { sink = nil }
    func discard() { sink = nil }
}

func buffer(rate: Double = 16_000, channels: [[Float]]) -> AVAudioPCMBuffer {
    let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: AVAudioChannelCount(channels.count))!
    let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(channels[0].count))!
    buffer.frameLength = buffer.frameCapacity
    for (i, samples) in channels.enumerated() {
        samples.withUnsafeBufferPointer { buffer.floatChannelData![i].update(from: $0.baseAddress!, count: samples.count) }
    }
    return buffer
}

func silence(_ frames: Int = 1600) -> AVAudioPCMBuffer { buffer(channels: [[Float](repeating: 0, count: frames)]) }
func tone(_ frames: Int = 1600) -> [Float] { (0..<frames).map { sin(Float($0) / 5) * 0.4 } }

@MainActor
final class ListenerFlowTests: XCTestCase {
    let brio = Microphone(id: 1, name: "Logitech BRIO", builtIn: false, virtual: false)
    let airPods = Microphone(id: 2, name: "AirPods Pro", builtIn: false, virtual: false, bluetooth: true)
    var recognizers = FakeRecognizers()
    var mics: [UInt32: FakeMic] = [:]
    var opened: [UInt32] = []
    var excluded: [Set<UInt32>] = []
    var openResults: [Result<Void, MicrophoneFailure>] = []
    var heard: [String] = []
    var problems: [String] = []
    var interrupted: [(String, String)] = []

    func listener(_ choices: [Microphone]) -> Listener {
        for mic in choices { mics[mic.id] = FakeMic(mic) }
        let deps = Listener.Dependencies(access: { (.granted, .granted) }, requestAccess: { _, done in done(true, true) },
                                         choose: { [unowned self] excluding in
                                             self.excluded.append(excluding)
                                             return choices.first { !excluding.contains($0.id) }
                                         }, open: { [unowned self] mic in
                                             DispatchQueue.main.sync { self.opened.append(mic.id) }
                                             let next = DispatchQueue.main.sync { self.openResults.isEmpty ? nil : self.openResults.removeFirst() }
                                             if case .failure(let failure)? = next { return .failure(failure) }
                                             return .success(self.mics[mic.id]!)
                                         }, recognizers: recognizers)
        let listener = Listener(deps)
        listener.pauseBeforeWords = 30
        listener.pauseAfterWords = 30
        listener.onHeard = { [unowned self] in self.heard.append($0) }
        listener.onProblem = { [unowned self] in self.problems.append($0) }
        listener.onInterrupted = { [unowned self] in self.interrupted.append(($0, $1)) }
        return listener
    }

    func eventually(_ timeout: TimeInterval = 3, _ condition: () -> Bool, file: StaticString = #filePath, line: UInt = #line) {
        let end = Date().addingTimeInterval(timeout)
        while !condition() && Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        XCTAssertTrue(condition(), file: file, line: line)
    }

    func spin(_ seconds: TimeInterval) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }

    func testTheListeningRowShowsStartingTheMomentYouPress() {
        let l = listener([brio])
        l.start()
        XCTAssertTrue(l.listening, "the row appears before the microphone starts")
        XCTAssertFalse(l.soundArrived)
        XCTAssertEqual(ListeningText.label(heard: l.heard, soundArrived: l.soundArrived), "Starting…")
    }

    /// A late final result from the previous question goes to onHeard, but doesn't overwrite the new question's text.
    func testALateResultFromThePreviousQuestionDoesNotOverwriteTheNextOne() {
        let l = listener([brio])
        l.start()
        eventually { mics[1]?.sink != nil }
        recognizers.sessions[0].onText("tell the trim session")
        l.stop()
        XCTAssertTrue(l.finishing)
        l.start()
        eventually { recognizers.sessions.count == 2 && mics[1]?.sink != nil }
        recognizers.sessions[1].onText("what about the deploy")
        recognizers.sessions[0].finishing?("Tell the trim session to merge main.")
        XCTAssertEqual(l.heard, "what about the deploy")
        XCTAssertTrue(l.listening)
        XCTAssertEqual(heard, ["Tell the trim session to merge main."])
    }

    /// A press that ended while a format retry was pending: the retry must not start a second recognizer.
    func testAStaleFormatRetryDoesNotStartAnotherRecognizer() {
        let l = listener([brio])
        l.formatRetryDelay = 0.2
        openResults = [.failure(.notReady)]
        l.start()
        eventually { opened.count == 1 }
        spin(0.05)
        l.stop()
        l.start()
        eventually { recognizers.sessions.count == 2 && mics[1]?.sink != nil }
        spin(0.5)
        XCTAssertEqual(recognizers.sessions.count, 2, "the stale retry started a recognizer")
        XCTAssertTrue(recognizers.sessions[0].cancelled || recognizers.sessions[0].finished)
        XCTAssertTrue(l.listening)
    }

    /// The tap can fire before `start` returns. That first zero buffer must not be judged against an old clock, so
    /// AirPods aren't skipped while they switch into call mode.
    func testAZeroBufferBeforeTheMicrophoneStartsDoesNotSkipIt() {
        let l = listener([airPods, brio])
        l.start()
        eventually { mics[2]?.sink != nil }
        mics[2]?.sink?(silence())
        spin(0.1)
        l.stop()
        spin(3.2)
        mics[2]?.duringStart = { $0.sink?(silence()) }
        excluded = []
        l.start()
        eventually { mics[2]?.sink != nil }
        spin(0.3)
        mics[2]?.sink?(silence())
        spin(0.2)
        XCTAssertEqual(l.microphone, "AirPods Pro")
        XCTAssertFalse(excluded.contains { $0.contains(2) }, "AirPods were skipped")
    }

    func testAMicrophoneThatSendsOnlySilenceIsSkipped() {
        let l = listener([brio, airPods])
        l.start()
        eventually { mics[1]?.sink != nil }
        for _ in 0..<10 {
            mics[1]?.sink?(silence())
            spin(0.1)
        }
        eventually { l.microphone == "AirPods Pro" }
        XCTAssertTrue(recognizers.sessions[0].cancelled)
    }

    func testSpeechOnlyOnTheSecondChannelReachesTheRecognizerAndTheMeter() {
        let l = listener([brio])
        l.start()
        eventually { mics[1]?.sink != nil }
        mics[1]?.sink?(buffer(channels: [[Float](repeating: 0, count: 1600), tone()]))
        eventually { recognizers.sessions[0].appended.count == 1 }
        let sent = recognizers.sessions[0].appended[0]
        XCTAssertEqual(sent.format.channelCount, 1)
        let samples = UnsafeBufferPointer(start: sent.floatChannelData![0], count: Int(sent.frameLength))
        XCTAssertGreaterThan(Meter.level(samples), 0.3)
        eventually { (l.levels.last ?? 0) > 0.5 }
        XCTAssertTrue(l.soundArrived)
    }

    /// Unplugging mid-question stops listening with a note and keeps what was heard.
    func testADisconnectedMicrophoneEndsTheQuestionAndKeepsItsText() {
        let l = listener([brio])
        l.start()
        eventually { mics[1]?.sink != nil }
        recognizers.sessions[0].onText("tell the trim")
        mics[1]?.lost?()
        eventually { recognizers.sessions[0].finishing != nil }
        XCTAssertFalse(l.listening)
        recognizers.sessions[0].finishing?("tell the trim session")
        XCTAssertEqual(interrupted.map(\.0), ["tell the trim session"])
        XCTAssertEqual(interrupted.map(\.1), [Listener.disconnectedNote])
        XCTAssertEqual(heard, [], "a cut-off question isn't sent on its own")
    }
}

@available(macOS 26, *)
final class FakeBackend: AnalyzerBackend {
    let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
    let finalDelay: TimeInterval
    private let lock = NSLock()
    private var _frames: AVAudioFrameCount = 0
    private var _cancelled = false
    private var consumer: Task<Void, Never>?
    private var result: ((String, Bool, TimeInterval) -> Void)?

    init(finalDelay: TimeInterval) { self.finalDelay = finalDelay }

    var frames: AVAudioFrameCount { lock.withLock { _frames } }
    var cancelled: Bool { lock.withLock { _cancelled } }

    func start(_ input: AsyncStream<AnalyzerInput>, result: @escaping (String, Bool, TimeInterval) -> Void) async throws {
        self.result = result
        consumer = Task {
            for await item in input {
                let frames = item.buffer.frameLength
                lock.withLock { _frames += frames }
                result("partial", false, 0)
            }
        }
    }

    func finish() async throws {
        await consumer?.value
        try await Task.sleep(nanoseconds: UInt64(finalDelay * 1_000_000_000))
        result?("every word", true, Double(frames) / 16_000)
    }

    func cancel() async { lock.withLock { _cancelled = true } }
}

@MainActor
final class AnalyzerSessionTests: XCTestCase {
    func wait(_ timeout: TimeInterval, until condition: () -> Bool) {
        let end = Date().addingTimeInterval(timeout)
        while !condition() && Date() < end { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
    }

    /// The final result can come well after the question ends; finish waits for it instead of cutting it off.
    func testFinishWaitsForTheFinalResult() throws {
        guard #available(macOS 26, *) else { throw XCTSkip("needs macOS 26") }
        let backend = FakeBackend(finalDelay: 0.6)
        var texts: [String] = []
        let session = AnalyzerSession(take: { backend }, fallback: { nil }, onText: { texts.append($0) }, onError: { _ in })
        for _ in 0..<5 { session.append(silence()) }
        wait(2) { !texts.isEmpty }
        var final: String?
        session.finish(.live) { final = $0 }
        wait(3) { final != nil }
        XCTAssertEqual(final, "every word")
        XCTAssertEqual(backend.frames, 8_000)
    }

    /// A file waits for the analyzer however long it takes, past the live ceiling's 2-second floor.
    func testAFileWaitsPastTheLiveWait() throws {
        guard #available(macOS 26, *) else { throw XCTSkip("needs macOS 26") }
        let backend = FakeBackend(finalDelay: 2.5)
        let session = AnalyzerSession(take: { backend }, fallback: { nil }, onText: { _ in }, onError: { _ in })
        session.append(silence())
        var final: String?
        session.finish(.none) { final = $0 }
        wait(5) { final != nil }
        XCTAssertEqual(final, "every word")
    }

    /// The analyzer isn't ready in time: the audio heard so far, then the rest, go to the older recognizer.
    func testAnUnreadyAnalyzerHandsAllTheAudioToTheFallback() throws {
        guard #available(macOS 26, *) else { throw XCTSkip("needs macOS 26") }
        let fallback = FakeSession(onText: { _ in }, onError: { _ in })
        let session = AnalyzerSession(take: {
            try await Task.sleep(nanoseconds: 200_000_000)
            throw ReadyPool<SpeechBackend>.NotReady()
        }, fallback: { fallback }, onText: { _ in }, onError: { _ in })
        session.append(silence())
        session.append(silence())
        wait(2) { fallback.appended.count == 2 }
        session.append(silence())
        XCTAssertEqual(fallback.appended.count, 3)
        var final: String?
        session.finish(.live) { final = $0 }
        XCTAssertTrue(fallback.finished)
        fallback.finishing?("from the older recognizer")
        XCTAssertEqual(final, "from the older recognizer")
    }

    func testCancellingBeforeTheAnalyzerIsReadyCancelsItWhenItArrives() throws {
        guard #available(macOS 26, *) else { throw XCTSkip("needs macOS 26") }
        let backend = FakeBackend(finalDelay: 0)
        var texts: [String] = []
        let session = AnalyzerSession(take: {
            try await Task.sleep(nanoseconds: 200_000_000)
            return backend
        }, fallback: { nil }, onText: { texts.append($0) }, onError: { _ in })
        session.append(silence())
        session.cancel()
        wait(2) { backend.cancelled }
        XCTAssertTrue(backend.cancelled)
        XCTAssertEqual(texts, [])
    }

    func testAMicrophoneFormatItCantConvertFailsThePress() throws {
        guard #available(macOS 26, *) else { throw XCTSkip("needs macOS 26") }
        let backend = FakeBackend(finalDelay: 0)
        var errors: [NSError] = []
        let session = AnalyzerSession(take: { backend }, fallback: { nil }, onText: { _ in }, onError: { errors.append($0) })
        let odd = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 10_000_000, channels: 1, interleaved: false)!
        let buffer = AVAudioPCMBuffer(pcmFormat: odd, frameCapacity: 160)!
        buffer.frameLength = 160
        session.append(buffer)
        wait(2) { !errors.isEmpty || backend.frames > 0 }
        XCTAssertEqual(backend.frames, 0, "unconverted audio never reaches the analyzer")
        XCTAssertEqual(errors.count, 1)
    }
}

@MainActor
final class ModelVoiceTests: XCTestCase {
    var said: [String] = []

    func model() -> Model {
        setenv("ASSISTANT_DB", NSTemporaryDirectory() + "voice-\(UUID().uuidString)/conversations.db", 1)
        defer { unsetenv("ASSISTANT_DB") }
        let model = Model()
        model.talk = { [unowned self] in self.said.append($0) }
        return model
    }

    func testReplayIsIgnoredWhileListening() {
        let m = model()
        m.chat = [ChatTurn(role: .assistant, text: "The PR merged.")]
        m.listener.listening = true
        m.replay()
        XCTAssertEqual(said, [])
        m.listener.listening = false
        m.replay()
        XCTAssertEqual(said, ["The PR merged."])
    }

    func testASpokenAnswerWaitsForListeningToEnd() {
        let m = model()
        m.listener.listening = true
        m.answered("The PR merged.")
        XCTAssertEqual(said, [])
        XCTAssertEqual(m.chat.last?.text, VoiceTurns.deferredNote)
        m.listeningEnded()
        XCTAssertEqual(said, [], "still listening")
        m.listener.listening = false
        m.listeningEnded()
        XCTAssertEqual(said, ["The PR merged."])
    }

    /// A streamed answer starts on its first sentence and queues the rest; nothing plays while listening.
    func testAStreamedAnswerStartsOnItsFirstSentenceAndQueuesTheRest() {
        let m = model()
        var more: [String] = []
        m.talkMore = { more.append($0) }
        m.spokenPart("The version bump needs your token.")
        m.spokenPart("The trim fix merged.")
        XCTAssertEqual(said, ["The version bump needs your token."])
        XCTAssertEqual(more, ["The trim fix merged."])
        m.listener.listening = true
        m.spokenPart("One more thing.")
        XCTAssertEqual(more, ["The trim fix merged."], "nothing plays into the open microphone")
        m.listener.listening = false
        m.listeningEnded()
        XCTAssertEqual(said.last, "One more thing.")
    }

    func testAQuestionHeardWhileAnsweringIsQueuedWithANote() {
        let m = model()
        m.asking = true
        m.heard("And the trim fix?")
        XCTAssertEqual(m.chat.map(\.text), ["And the trim fix?", VoiceTurns.queuedNote])
        XCTAssertTrue(m.asking)
    }
}

final class SpeakerTests: XCTestCase {
    func testAppleUtterancesAndKokoroUseTheChosenSpeed() {
        let speaker = Speaker()
        let saved = UserDefaults.standard.object(forKey: Speaker.speedKey)
        defer { UserDefaults.standard.set(saved, forKey: Speaker.speedKey) }
        speaker.speed = 1.5
        XCTAssertEqual(speaker.utterance("hi").rate,
                       SpeechSpeed.appleRate(1.5, defaultRate: AVSpeechUtteranceDefaultSpeechRate,
                                             maximumRate: AVSpeechUtteranceMaximumSpeechRate))
        XCTAssertEqual(speaker.kokoroSpeed, 1.5)
        speaker.speed = 2
        XCTAssertEqual(speaker.kokoroSpeed, 2)
        XCTAssertGreaterThan(speaker.utterance("hi").rate, AVSpeechUtteranceDefaultSpeechRate)
    }

    /// Cutting an utterance off for a replay reports the old one cancelled; that mustn't end the new one.
    func testACallbackForAnEarlierUtteranceDoesNotEndTheCurrentOne() {
        let speaker = Speaker()
        let old = AVSpeechUtterance(string: "old")
        let current = AVSpeechUtterance(string: "current")
        speaker.appleUtterance = current
        speaker.speaking = true
        speaker.speechSynthesizer(AVSpeechSynthesizer(), didCancel: old)
        speaker.speechSynthesizer(AVSpeechSynthesizer(), didFinish: old)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertTrue(speaker.speaking)
        speaker.speechSynthesizer(AVSpeechSynthesizer(), didFinish: current)
        RunLoop.main.run(until: Date().addingTimeInterval(0.1))
        XCTAssertFalse(speaker.speaking)
    }
}

/// Renders speech with AVSpeechSynthesizer.write (nothing plays) and checks each speed's length.
final class AppleSpeechSpeedTests: XCTestCase {
    func seconds(rate: Float) throws -> Double {
        let synth = AVSpeechSynthesizer()
        let utterance = AVSpeechUtterance(string: "Tell the version bump session to use my token, then report back when it has pushed the branch.")
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = rate
        var frames = 0.0
        var sampleRate = 0.0
        let done = expectation(description: "rendered")
        synth.write(utterance) { buffer in
            guard let pcm = buffer as? AVAudioPCMBuffer else { return }
            if pcm.frameLength == 0 { return done.fulfill() }
            frames += Double(pcm.frameLength)
            sampleRate = pcm.format.sampleRate
        }
        wait(for: [done], timeout: 30)
        return try XCTUnwrap(sampleRate > 0 ? frames / sampleRate : nil)
    }

    func testEachSpeedPlaysWithinFifteenPercentOfItsMultiplier() throws {
        try XCTSkipIf(AVSpeechSynthesisVoice(language: "en-US") == nil, "no English voice")
        let rate = { (m: Double) in SpeechSpeed.appleRate(m, defaultRate: AVSpeechUtteranceDefaultSpeechRate,
                                                           maximumRate: AVSpeechUtteranceMaximumSpeechRate) }
        let normal = try seconds(rate: rate(1))
        for multiplier in [1.5, 2.0] {
            let speedup = normal / (try seconds(rate: rate(multiplier)))
            XCTAssertEqual(speedup, multiplier, accuracy: multiplier * 0.15, "\(multiplier)× played at \(speedup)×")
        }
    }
}

final class ChatScrollTests: XCTestCase {
    func key(heard: String = "", modelNote: String? = nil) -> [String] {
        ChatView.scrollKey(chat: [], asking: false, listening: true, heard: heard, note: nil, soundArrived: true,
                           modelNote: modelNote)
    }

    func testGrowingLiveTextAndTheModelNoteScrollTheChat() {
        XCTAssertNotEqual(key(heard: "tell"), key(heard: "tell me"))
        XCTAssertNotEqual(key(heard: "tell them"), key(heard: "tell then"), "a revision the same length still scrolls")
        XCTAssertNotEqual(key(), key(modelNote: ListeningText.modelNote(.downloading(0.5))))
        XCTAssertEqual(key(heard: "same"), key(heard: "same"))
    }
}
