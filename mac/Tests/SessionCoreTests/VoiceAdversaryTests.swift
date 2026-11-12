import XCTest
@testable import SessionCore

/// Adversarial checks on the pure voice pieces. A failing test here is a finding, not a flaky test.
final class VoiceAdversaryLiveTranscriptTests: XCTestCase {
    func testEmptyAndWhitespaceResultsNeverAddSpaces() {
        var t = LiveTranscript()
        XCTAssertEqual(t.take("", isFinal: false), "")
        XCTAssertEqual(t.take("   \n", isFinal: true), "")
        XCTAssertEqual(t.take(" hi ", isFinal: true), "hi")
        XCTAssertEqual(t.take("\t", isFinal: false), "hi")
        XCTAssertEqual(t.take("", isFinal: true), "hi")
        XCTAssertEqual(t.text, "hi")
    }

    func testAlternatingVolatileAndFinalKeepsEveryFinalOnceInOrder() {
        var t = LiveTranscript()
        for i in 0..<500 {
            t.take("guess \(i)", isFinal: false)
            t.take("word\(i)", isFinal: true)
        }
        XCTAssertEqual(t.text, (0..<500).map { "word\($0)" }.joined(separator: " "))
        XCTAssertEqual(t.volatile, "")
    }

    func testAVolatileGuessNeverSurvivesTheFinalThatReplacesIt() {
        var t = LiveTranscript()
        t.take("tell work space twenty", isFinal: false)
        t.take("Tell workspace 20", isFinal: true)
        XCTAssertFalse(t.text.contains("twenty"), t.text)
    }

    func testAVeryLongDictationStaysFast() {
        var t = LiveTranscript()
        let started = Date()
        for i in 0..<20_000 {
            t.take(String(repeating: "x", count: 40) + "\(i)", isFinal: i % 2 == 0)
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 5, "quadratic joins would stall the main thread")
    }

    func testEmojiAndCombiningCharactersSurviveJoining() {
        var t = LiveTranscript()
        t.take("ship it 🚀", isFinal: true)
        t.take("café e\u{301}", isFinal: false)
        XCTAssertEqual(t.text, "ship it 🚀 café e\u{301}")
    }
}

final class VoiceAdversaryTranscriptAccumulatorTests: XCTestCase {
    /// Reproduced end to end: `--transcribe pause_same_first.wav --legacy` prints only the second sentence.
    /// The recognizer times an utterance when it ends, before it restarts (see TranscriptAccumulatorTests).
    func testARestartThatBeginsWithTheSameWordKeepsTheFirstSentence() {
        let untimed = { (n: Int) in [TranscriptAccumulator.Segment](repeating: .init(start: 0, duration: 0), count: n) }
        var t = TranscriptAccumulator()
        _ = t.update("Tell workspace 25 to stop the build", segments: untimed(7))
        _ = t.update("Tell workspace 25 to stop the build",
                     segments: (0..<7).map { .init(start: Double($0) * 0.3, duration: 0.3) })
        _ = t.update("Tell", segments: untimed(1))
        _ = t.update("Tell workspace", segments: untimed(2))
        let text = t.update("Tell workspace 30 to start the deploy", segments: untimed(7))
        XCTAssertTrue(text.contains("stop the build"), "the first question was dropped: \(text)")
    }
}

final class VoiceAdversaryVocabularyTests: XCTestCase {
    func testThousandsOfTitlesStayWithinTheLimitAndAreQuick() {
        let titles = (0..<5_000).map { "FFP-\($0) fix the trim handle for clip\($0) in workspace\($0)" }
        let started = Date()
        let strings = Vocabulary.contextualStrings(titles: titles)
        XCTAssertLessThan(Date().timeIntervalSince(started), 2)
        XCTAssertEqual(strings.count, Vocabulary.limit)
        XCTAssertEqual(Set(strings.map { $0.lowercased() }).count, strings.count)
    }

    func testDuplicateTitlesInAnyCaseAppearOnce() {
        let strings = Vocabulary.contextualStrings(titles: Array(repeating: "Trim Fix", count: 300) + ["TRIM FIX", "trim fix"])
        XCTAssertEqual(strings.filter { $0.lowercased() == "trim fix" }.count, 1)
    }

    func testEmojiTitlesDoNotCrashOrLeakEmptyStrings() {
        let strings = Vocabulary.contextualStrings(titles: ["🚀🚀🚀", "🔥 hotfix 🔥", "--'--", "'''", "\u{200B}"])
        XCTAssertFalse(strings.contains(""))
        XCTAssertTrue(strings.contains("hotfix"))
    }

    func testZeroAndNegativeLimitsReturnNothing() {
        XCTAssertEqual(Vocabulary.contextualStrings(titles: ["a b c"], limit: 0), [])
        XCTAssertEqual(Vocabulary.contextualStrings(titles: ["a b c"], limit: -5), [])
    }

    /// A pasted log or a long branch name as a workspace title goes to the recognizer as one phrase.
    func testAHugeTitleIsNotSentAsOneContextualPhrase() {
        let huge = String(repeating: "assembler ", count: 100_000)
        let strings = Vocabulary.contextualStrings(titles: [huge])
        XCTAssertLessThanOrEqual(strings.map(\.count).max() ?? 0, 200,
                                 "a \(strings.map(\.count).max() ?? 0)-character contextual string reaches the recognizer")
    }
}

final class VoiceAdversaryReplayTests: XCTestCase {
    func testOnlyStepsErrorsAndQuestionsMeansNothingToReplay() {
        let chat = [ChatTurn(role: .you, text: "hi"), ChatTurn(role: .step, text: "Sending…"),
                    ChatTurn(role: .step, text: ChatHistory.compactedNote), ChatTurn(role: .error, text: "Claude failed.")]
        XCTAssertNil(Replay.target(chat))
    }

    func testAQuestionStillRunningReplaysTheAnswerBeforeIt() {
        let chat = [ChatTurn(role: .you, text: "first?"), ChatTurn(role: .assistant, text: "First answer."),
                    ChatTurn(role: .you, text: "second?"), ChatTurn(role: .step, text: "Sending…")]
        XCTAssertEqual(Replay.target(chat), "First answer.")
    }

    func testAnAnswerThatIsOnlyInvisibleCharactersIsSkipped() {
        let chat = [ChatTurn(role: .assistant, text: "Real."), ChatTurn(role: .assistant, text: "\u{00A0}\u{2028}\n\t")]
        XCTAssertEqual(Replay.target(chat), "Real.")
    }

    func testAHugeChatFindsTheNewestAnswerQuickly() {
        let chat = (0..<50_000).map { ChatTurn(role: $0 % 2 == 0 ? .you : .assistant, text: "turn \($0)") }
        XCTAssertEqual(Replay.target(chat), "turn 49999")
    }
}

final class VoiceAdversaryTapShapeTests: XCTestCase {
    func testOddDeviceFormats() {
        let engine = AudioShape(sampleRate: 48_000, channels: 1)
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 8_000, channels: 1), engine: engine),
                       AudioShape(sampleRate: 8_000, channels: 1), "an HFP headset at 8 kHz taps at 8 kHz")
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 192_000, channels: 2), engine: engine),
                       AudioShape(sampleRate: 192_000, channels: 1))
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 48_000, channels: 8),
                                            engine: AudioShape(sampleRate: 48_000, channels: 8)),
                       AudioShape(sampleRate: 48_000, channels: 8))
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 0, channels: 8), engine: engine), engine)
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: .nan, channels: 1), engine: engine), engine)
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: .infinity, channels: 1), engine: engine), engine)
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: -44_100, channels: 1), engine: engine), engine)
        XCTAssertNil(Microphones.tapShape(device: nil, engine: AudioShape(sampleRate: .nan, channels: 1)))
        XCTAssertNil(Microphones.tapShape(device: AudioShape(sampleRate: 48_000, channels: 0),
                                          engine: AudioShape(sampleRate: 48_000, channels: 0)))
    }

    func testTapStillFitsOddChanges() {
        let tap = AudioShape(sampleRate: 48_000, channels: 1)
        XCTAssertFalse(Microphones.tapStillFits(tap, device: AudioShape(sampleRate: .nan, channels: 1)))
        XCTAssertFalse(Microphones.tapStillFits(tap, device: AudioShape(sampleRate: 48_000, channels: 0)))
        XCTAssertFalse(Microphones.tapStillFits(tap, device: AudioShape(sampleRate: 8_000, channels: 1)),
                       "AirPods dropping into HFP must rebuild")
        XCTAssertTrue(Microphones.tapStillFits(tap, device: AudioShape(sampleRate: 48_000, channels: 8)))
    }

    func testSilenceAllowanceEdges() {
        let both = Microphone(id: 9, name: "", builtIn: true, virtual: true, bluetooth: true)
        XCTAssertEqual(Microphones.silenceAllowance(both), 3)
        XCTAssertEqual(Microphones.silenceAllowance(Microphone(id: 0, name: "", builtIn: true, virtual: false)), 0.7)
    }
}

final class VoiceAdversarySpeedTests: XCTestCase {
    func testNonFiniteAndExtremeSpeeds() {
        for bad in [Double.nan, .infinity, -.infinity, -.nan, .signalingNaN] {
            XCTAssertEqual(SpeechSpeed.clamped(bad), SpeechSpeed.defaultValue)
            XCTAssertEqual(SpeechSpeed.label(bad), "1×")
            XCTAssertEqual(SpeechSpeed.appleRate(bad, defaultRate: 0.5, maximumRate: 1), 0.5)
        }
        XCTAssertEqual(SpeechSpeed.clamped(.greatestFiniteMagnitude), 2)
        XCTAssertEqual(SpeechSpeed.clamped(-.greatestFiniteMagnitude), 1)
        XCTAssertEqual(SpeechSpeed.clamped(.leastNonzeroMagnitude), 1)
        XCTAssertEqual(SpeechSpeed.clamped(-0.0), 1)
        XCTAssertEqual(SpeechSpeed.label(1.999), "2×")
        XCTAssertEqual(SpeechSpeed.label(1.004), "1×")
        XCTAssertEqual(SpeechSpeed.appleRate(2, defaultRate: 0.5, maximumRate: 0.4), 0.4)
    }
}

final class VoiceAdversaryContextWindowTests: XCTestCase {
    private func defaults() -> UserDefaults { UserDefaults(suiteName: "voice-adv-\(UUID().uuidString)")! }

    func testZeroAndNegativeReportsAreIgnored() {
        let d = defaults()
        XCTAssertFalse(ContextWindow.remember(0, for: "haiku", settings: d))
        XCTAssertFalse(ContextWindow.remember(-5, for: "haiku", settings: d))
        XCTAssertEqual(ContextWindow.tokens(for: "haiku", environment: [:], settings: d), 1_000_000)
    }

    /// One malformed result event (contextWindow: 1) would make every later turn compress.
    func testAnAbsurdlySmallReportDoesNotStick() {
        let d = defaults()
        ContextWindow.remember(1, for: "haiku", settings: d)
        XCTAssertGreaterThanOrEqual(ContextWindow.tokens(for: "haiku", environment: [:], settings: d), 10_000)
    }

    func testAnAbsurdlyLargeReportDoesNotStick() {
        let d = defaults()
        ContextWindow.remember(Int.max, for: "haiku", settings: d)
        XCTAssertLessThanOrEqual(ContextWindow.tokens(for: "haiku", environment: [:], settings: d), 100_000_000)
    }

    func testANegativeOrGarbageOverrideFallsThrough() {
        let d = defaults()
        XCTAssertEqual(ContextWindow.tokens(for: "opus", environment: ["ASSISTANT_CONTEXT_WINDOW": "-1"], settings: d), 1_000_000)
        XCTAssertEqual(ContextWindow.tokens(for: "opus", environment: ["ASSISTANT_CONTEXT_WINDOW": "lots"], settings: d), 1_000_000)
    }
}

final class VoiceAdversaryChoiceTests: XCTestCase {
    func testOddDownloadFractionsStillProduceANote() {
        XCTAssertEqual(ListeningText.modelNote(.downloading(0)),
                       "Downloading Apple's speech model (0%). Until it's ready, I use the older recognizer.")
        XCTAssertEqual(ListeningText.modelNote(.downloading(1.7)),
                       "Downloading Apple's speech model (100%). Until it's ready, I use the older recognizer.",
                       "an over-reported fraction is clamped")
    }

    func testPermissionsNeverListenWithAnyDenial() {
        let all: [Access] = [.granted, .denied, .undetermined]
        for mic in all { for speech in all { for needs in [true, false] {
            let next = VoicePermissions.next(mic: mic, speech: speech, needsSpeech: needs)
            if mic == .denied || (needs && speech == .denied) { XCTAssertNotEqual(next, .listen) }
            if next == .listen { XCTAssertEqual(mic, .granted) }
        } } }
    }
}
