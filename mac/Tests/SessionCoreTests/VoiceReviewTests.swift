import XCTest
@testable import SessionCore

/// Fixes for the voice review findings, one class per finding.
final class SilenceWatchTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_000)

    /// A zero buffer that arrives before this microphone has started doesn't count against it.
    func testBuffersBeforeTheMicrophoneStartsAreIgnored() {
        var watch = SilenceWatch(allowance: 3)
        XCTAssertEqual(watch.took(silent: true, at: t0.addingTimeInterval(10)), .ignored)
        watch.started(at: t0.addingTimeInterval(10))
        XCTAssertEqual(watch.took(silent: true, at: t0.addingTimeInterval(11)), .waiting)
    }

    func testSilenceLongerThanTheAllowanceSkipsAndSoundNeverDoes() {
        var watch = SilenceWatch(allowance: 3)
        watch.started(at: t0)
        XCTAssertEqual(watch.took(silent: true, at: t0.addingTimeInterval(2.9)), .waiting)
        XCTAssertEqual(watch.took(silent: true, at: t0.addingTimeInterval(3)), .skip)
        var heard = SilenceWatch(allowance: 0.7)
        heard.started(at: t0)
        XCTAssertEqual(heard.took(silent: false, at: t0.addingTimeInterval(0.1)), .firstSound)
        XCTAssertEqual(heard.took(silent: false, at: t0.addingTimeInterval(0.2)), .sound)
        XCTAssertEqual(heard.took(silent: true, at: t0.addingTimeInterval(5)), .sound, "silence after sound is a pause")
    }
}

final class RecognitionWaitTests: XCTestCase {
    func testTheWaitGrowsWithTheBacklogBetweenTwoAndThirtySeconds() {
        XCTAssertEqual(RecognitionWait.live(backlog: 0), 2)
        XCTAssertEqual(RecognitionWait.live(backlog: 1), 2)
        XCTAssertEqual(RecognitionWait.live(backlog: 4), 6)
        XCTAssertEqual(RecognitionWait.live(backlog: 600), 30)
        XCTAssertEqual(RecognitionWait.live(backlog: .nan), 2)
        XCTAssertEqual(RecognitionWait.live(backlog: -3), 2)
    }
}

final class VoiceTurnsTests: XCTestCase {
    func testAQuestionHeardWhileAnsweringWaitsItsTurn() {
        var turns = VoiceTurns()
        XCTAssertEqual(turns.heard("first?", asking: false), .ask("first?"))
        XCTAssertEqual(turns.heard("second?", asking: true), .queued(note: VoiceTurns.queuedNote))
        XCTAssertEqual(turns.heard("third?", asking: true), .queued(note: VoiceTurns.queuedNote))
        XCTAssertEqual(turns.answerFinished(), "second?")
        XCTAssertEqual(turns.answerFinished(), "third?")
        XCTAssertNil(turns.answerFinished())
    }

    func testNothingIsSpokenIntoAnOpenMicrophone() {
        var turns = VoiceTurns()
        XCTAssertEqual(turns.say("Answer.", listening: false, isReply: true), .speak("Answer."))
        XCTAssertEqual(turns.say("Answer.", listening: true, isReply: true), .deferred)
        XCTAssertEqual(turns.say("This is how fast I'll talk.", listening: true, isReply: false), .skipped)
        XCTAssertEqual(turns.listeningEnded(), "Answer.")
        XCTAssertNil(turns.listeningEnded(), "a deferred reply plays once")
    }
}

final class MeterChannelTests: XCTestCase {
    /// A microphone that records only on its second channel still reaches the recognizer and the meter.
    func testSpeechOnTheSecondChannelIsKept() {
        let silent = [Float](repeating: 0, count: 4)
        let speech: [Float] = [0.5, -0.5, 0.5, -0.5]
        silent.withUnsafeBufferPointer { a in
            speech.withUnsafeBufferPointer { b in
                XCTAssertEqual(Meter.loudest([a, b]), Meter.level(b))
                XCTAssertGreaterThan(Meter.loudest([a, b]), 0.5)
                var out = [Float](repeating: 9, count: 4)
                out.withUnsafeMutableBufferPointer { Meter.mono([a, b], into: $0) }
                XCTAssertEqual(out, [0.25, -0.25, 0.25, -0.25])
            }
        }
    }
}

final class ReportedWindowChoiceTests: XCTestCase {
    func testTheAnsweringModelsWindowIsUsedNotTheLargest() {
        let usage: [String: Any] = ["claude-haiku-5-5": ["contextWindow": 200_000], "claude-opus-5-5": ["contextWindow": 1_000_000]]
        XCTAssertEqual(StreamTurn.reportedWindow(usage, model: "haiku"), 200_000)
        XCTAssertEqual(StreamTurn.reportedWindow(usage, model: "claude-opus-5-5"), 1_000_000)
        XCTAssertNil(StreamTurn.reportedWindow(usage, model: "sonnet"))
        XCTAssertNil(StreamTurn.reportedWindow(usage, model: nil), "two models and no name: don't guess")
        XCTAssertEqual(StreamTurn.reportedWindow(["claude-haiku-5-5": ["contextWindow": 1_000_000]], model: nil), 1_000_000)
    }

    func testImplausibleWindowsAreRejected() {
        XCTAssertNil(StreamTurn.reportedWindow(["claude-haiku-5-5": ["contextWindow": 1]], model: "haiku"))
        XCTAssertNil(StreamTurn.reportedWindow(["claude-haiku-5-5": ["contextWindow": Int.max]], model: "haiku"))
        let d = UserDefaults(suiteName: "voice-review-\(UUID().uuidString)")!
        XCTAssertFalse(ContextWindow.remember(7_999, for: "haiku", settings: d))
        XCTAssertFalse(ContextWindow.remember(10_000_001, for: "haiku", settings: d))
        XCTAssertTrue(ContextWindow.remember(8_000, for: "haiku", settings: d))
        d.set(1, forKey: ContextWindow.reportedKey("sonnet"))
        XCTAssertEqual(ContextWindow.tokens(for: "sonnet", environment: [:], settings: d), 1_000_000,
                       "a bad value stored before this fix is ignored")
    }
}

final class VoiceLowFindingTests: XCTestCase {
    func testANonFiniteDownloadFractionShowsZeroAndOverReportsShowAHundred() {
        let note = { (f: Double) in ListeningText.modelNote(.downloading(f)) }
        XCTAssertEqual(note(.nan), "Downloading Apple's speech model (0%). Until it's ready, I use the older recognizer.")
        XCTAssertEqual(note(.infinity), note(.nan))
        XCTAssertEqual(note(-1), note(0))
        XCTAssertEqual(note(3), note(1))
    }

    func testEachVocabularyPhraseIsShort() {
        let long = "pasted build log line that goes on and on about compiler warnings in the trim module"
        let strings = Vocabulary.contextualStrings(titles: [long, "Trim fix"])
        XCTAssertFalse(strings.contains(long))
        XCTAssertTrue(strings.contains("Trim fix"))
        XCTAssertTrue(strings.contains("compiler"), "the long title's words still count")
        XCTAssertLessThanOrEqual(strings.map(\.count).max() ?? 0, Vocabulary.longestPhrase)
    }

    func testAFailedTurnsNoReplyNoteIsNeverReplayed() {
        let chat = [ChatTurn(role: .assistant, text: "Real answer."),
                    ChatTurn(role: .assistant, text: TurnRecord.noReplyPrefix + "Claude Code exited with status 1]")]
        XCTAssertEqual(Replay.target(chat), "Real answer.")
    }
}

final class ReadyPoolTests: XCTestCase {
    final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0
        func next() -> Int { lock.lock(); defer { lock.unlock() }; value += 1; return value }
        var count: Int { lock.lock(); defer { lock.unlock() }; return value }
    }

    @MainActor
    func testAfterATakeTheNextValueIsAlreadyBeingPrepared() async throws {
        let made = Counter()
        let pool = ReadyPool<Int>(check: { _ in true }, make: { made.next() })
        pool.warm()
        let first = try await pool.take(within: 5)
        XCTAssertEqual(first, 1)
        try await Task.sleep(nanoseconds: 200_000_000)
        XCTAssertEqual(made.count, 2, "the next question's value is prepared before it's asked for")
        let second = try await pool.take(within: 0.5)
        XCTAssertEqual(second, 2)
    }

    /// A press while the model is still downloading falls back quickly instead of waiting for the download.
    @MainActor
    func testATakeGivesUpAfterItsBoundWhileTheCheckRuns() async {
        let pool = ReadyPool<Int>(check: { _ in
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            return true
        }, make: { 1 })
        pool.warm()
        let started = Date()
        do {
            _ = try await pool.take(within: 0.3)
            XCTFail("the model wasn't ready")
        } catch {
            XCTAssertTrue(error is ReadyPool<Int>.NotReady)
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.5)
    }

    @MainActor
    func testAnUnavailableModelIsCheckedAgainAfterTheRetryTime() async throws {
        var clock = Date(timeIntervalSince1970: 0)
        let checks = Counter()
        let pool = ReadyPool<Int>(retryAfter: 60, now: { clock }, check: { _ in checks.next() >= 2 }, make: { 7 })
        pool.warm()
        await assertEventually { pool.state == .unavailable }
        pool.warm()
        XCTAssertEqual(checks.count, 1, "no retry before the retry time")
        do { _ = try await pool.take(within: 0.5); XCTFail("still unavailable") } catch {}
        clock = clock.addingTimeInterval(61)
        pool.warm()
        await assertEventually { pool.state == .ready }
        let value = try await pool.take(within: 1)
        XCTAssertEqual(value, 7)
        XCTAssertEqual(checks.count, 2)
    }

    struct Broken: Error {}

    @MainActor
    func testRepeatedPrepareFailuresMarkTheModelUnavailable() async {
        let attempts = Counter()
        let pool = ReadyPool<Int>(maximumFailures: 3, check: { _ in true }, make: {
            _ = attempts.next()
            throw Broken()
        })
        pool.warm()
        await assertEventually { pool.state == .unavailable }
        XCTAssertEqual(attempts.count, 3)
        do { _ = try await pool.take(within: 0.5); XCTFail("unavailable") } catch {
            XCTAssertTrue(error is ReadyPool<Int>.NotReady)
        }
    }

    @MainActor
    func testDownloadProgressIsReported() async {
        var states: [SpeechModelState] = []
        let pool = ReadyPool<Int>(check: { progress in
            progress(0.25)
            try? await Task.sleep(nanoseconds: 100_000_000)
            return true
        }, make: { 1 })
        pool.onState = { states.append($0) }
        pool.warm()
        await assertEventually { pool.state == .ready }
        XCTAssertEqual(states, [.checking, .downloading(0.25), .ready])
    }

    @MainActor
    func assertEventually(_ condition: @escaping () -> Bool, file: StaticString = #filePath, line: UInt = #line) async {
        for _ in 0..<100 where !condition() { try? await Task.sleep(nanoseconds: 20_000_000) }
        XCTAssertTrue(condition(), file: file, line: line)
    }
}

final class TranscriptionResultTests: XCTestCase {
    func testOnlyWordsCountAsASuccessfulTranscription() {
        XCTAssertTrue(Transcription.succeeded("Tell workspace 25 to stop."))
        XCTAssertFalse(Transcription.succeeded(""))
        XCTAssertFalse(Transcription.succeeded(" \n"))
        XCTAssertFalse(Transcription.succeeded(nil))
        XCTAssertFalse(Transcription.succeeded("error: kAFAssistantErrorDomain 1110 No speech detected"))
    }
}
