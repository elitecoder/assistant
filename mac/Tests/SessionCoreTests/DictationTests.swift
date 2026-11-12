import XCTest
@testable import SessionCore

final class TapShapeTests: XCTestCase {
    func testAirPodsInCallModeTapAtTheirOwnRateNotTheStaleEngineRate() {
        let shape = Microphones.tapShape(device: AudioShape(sampleRate: 24_000, channels: 1),
                                         engine: AudioShape(sampleRate: 48_000, channels: 1))
        XCTAssertEqual(shape, AudioShape(sampleRate: 24_000, channels: 1))
    }

    func testChannelsNeverExceedTheDevice() {
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 16_000, channels: 1),
                                            engine: AudioShape(sampleRate: 48_000, channels: 2)),
                       AudioShape(sampleRate: 16_000, channels: 1))
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 48_000, channels: 2),
                                            engine: AudioShape(sampleRate: 48_000, channels: 1)),
                       AudioShape(sampleRate: 48_000, channels: 1), "a stereo webcam still taps in the engine's mono")
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 48_000, channels: 2),
                                            engine: AudioShape(sampleRate: 0, channels: 0)),
                       AudioShape(sampleRate: 48_000, channels: 2))
    }

    func testAnUnreadableDeviceFallsBackToTheEngineAndNothingUsableIsNil() {
        XCTAssertEqual(Microphones.tapShape(device: nil, engine: AudioShape(sampleRate: 44_100, channels: 1)),
                       AudioShape(sampleRate: 44_100, channels: 1))
        XCTAssertEqual(Microphones.tapShape(device: AudioShape(sampleRate: 0, channels: 1),
                                            engine: AudioShape(sampleRate: 44_100, channels: 1)),
                       AudioShape(sampleRate: 44_100, channels: 1))
        XCTAssertNil(Microphones.tapShape(device: AudioShape(sampleRate: 0, channels: 0),
                                          engine: AudioShape(sampleRate: 0, channels: 1)))
    }
}

final class MicrophoneTimingTests: XCTestCase {
    func testABluetoothHeadsetGetsLongerToSwitchIntoCallMode() {
        let airPods = Microphone(id: 1, name: "AirPods Pro", builtIn: false, virtual: false, bluetooth: true)
        let brio = Microphone(id: 2, name: "Logitech BRIO", builtIn: false, virtual: false)
        XCTAssertEqual(Microphones.silenceAllowance(airPods), 3)
        XCTAssertEqual(Microphones.silenceAllowance(brio), 0.7)
    }

    func testAPreparedTapSurvivesOnlyAChangeThatKeepsItsFormat() {
        let tap = AudioShape(sampleRate: 24_000, channels: 1)
        XCTAssertTrue(Microphones.tapStillFits(tap, device: AudioShape(sampleRate: 24_000, channels: 1)))
        XCTAssertTrue(Microphones.tapStillFits(tap, device: AudioShape(sampleRate: 24_000, channels: 2)))
        XCTAssertFalse(Microphones.tapStillFits(tap, device: AudioShape(sampleRate: 16_000, channels: 1)))
        XCTAssertFalse(Microphones.tapStillFits(AudioShape(sampleRate: 48_000, channels: 2),
                                                device: AudioShape(sampleRate: 48_000, channels: 1)))
        XCTAssertFalse(Microphones.tapStillFits(tap, device: nil), "a device that's gone needs a new engine")
    }
}

final class LiveTranscriptTests: XCTestCase {
    func testVolatileGuessesReplaceEachOtherAndFinalTextStays() {
        var t = LiveTranscript()
        XCTAssertEqual(t.take("tell", isFinal: false), "tell")
        XCTAssertEqual(t.take("tell work space", isFinal: false), "tell work space")
        XCTAssertEqual(t.take("Tell workspace 25", isFinal: true), "Tell workspace 25")
        XCTAssertEqual(t.take(" to use", isFinal: false), "Tell workspace 25 to use")
        XCTAssertEqual(t.take(" to use my token.", isFinal: true), "Tell workspace 25 to use my token.")
        XCTAssertEqual(t.volatile, "")
    }

    func testAPauseKeepsWhatWasFinalized() {
        var t = LiveTranscript()
        t.take("What does the version bump session need?", isFinal: true)
        XCTAssertEqual(t.take("And", isFinal: false), "What does the version bump session need? And")
        XCTAssertEqual(t.take("And the trim fix?", isFinal: true), "What does the version bump session need? And the trim fix?")
    }

    func testAnEmptyFinalResultClearsTheGuess() {
        var t = LiveTranscript()
        t.take("um", isFinal: false)
        XCTAssertEqual(t.take("", isFinal: true), "")
    }
}

final class VocabularyTests: XCTestCase {
    func testFixedTermsComeFirstThenTitlesThenTheirDistinctiveWords() {
        let strings = Vocabulary.contextualStrings(titles: ["FFP-118669  breadcrumbs fix", "Assembler E2E suite"])
        XCTAssertEqual(Array(strings.prefix(Vocabulary.terms.count)), Vocabulary.terms)
        let rest = Array(strings.dropFirst(Vocabulary.terms.count))
        XCTAssertEqual(rest, ["FFP-118669 breadcrumbs fix", "Assembler E2E suite", "FFP-118669", "breadcrumbs", "E2E",
                              "suite"])
    }

    func testRepeatsIgnoreCaseAndShortOrCommonWordsAreLeftOut() {
        let strings = Vocabulary.contextualStrings(titles: ["cmux pr", "Add the new kokoro voice", "  ", "42 99"])
        let extra = Array(strings.dropFirst(Vocabulary.terms.count))
        XCTAssertEqual(extra, ["cmux pr", "Add the new kokoro voice", "42 99", "voice"])
    }

    func testTheListStopsAtTheLimit() {
        let titles = (1...80).map { "Session alpha\($0) beta\($0)" }
        XCTAssertEqual(Vocabulary.contextualStrings(titles: titles).count, Vocabulary.limit)
        XCTAssertEqual(Vocabulary.contextualStrings(titles: titles, limit: 3), Array(Vocabulary.terms.prefix(3)))
    }
}

final class ReplayTests: XCTestCase {
    func testTheNewestAnswerWithTextIsReplayed() {
        let chat = [ChatTurn(role: .you, text: "first?"), ChatTurn(role: .assistant, text: "Old answer."),
                    ChatTurn(role: .you, text: "second?"), ChatTurn(role: .assistant, text: "New answer."),
                    ChatTurn(role: .step, text: "Sending…"), ChatTurn(role: .error, text: "Claude failed.")]
        XCTAssertEqual(Replay.target(chat), "New answer.")
    }

    func testBlankAnswersAndChatsWithoutAnswersHaveNothingToReplay() {
        XCTAssertNil(Replay.target([]))
        XCTAssertNil(Replay.target([ChatTurn(role: .you, text: "hi"), ChatTurn(role: .error, text: "no")]))
        XCTAssertEqual(Replay.target([ChatTurn(role: .assistant, text: "Kept."), ChatTurn(role: .assistant, text: " \n")]),
                       "Kept.")
    }
}

final class RecognizerChoiceTests: XCTestCase {
    func testTheNewerModelIsUsedWhenItIsReadyOrStillBeingChecked() {
        XCTAssertEqual(SpeechRecognizerChoice.choose(analyzerOS: true, model: .ready), .analyzer)
        XCTAssertEqual(SpeechRecognizerChoice.choose(analyzerOS: true, model: .checking), .analyzer)
    }

    func testTheOlderRecognizerCoversOldMacsAndMissingOrDownloadingModels() {
        XCTAssertEqual(SpeechRecognizerChoice.choose(analyzerOS: false, model: .ready), .legacy)
        XCTAssertEqual(SpeechRecognizerChoice.choose(analyzerOS: true, model: .downloading(0.4)), .legacy)
        XCTAssertEqual(SpeechRecognizerChoice.choose(analyzerOS: true, model: .unavailable), .legacy)
    }
}

final class VoicePermissionsTests: XCTestCase {
    func testGrantedAccessListensWithoutAsking() {
        XCTAssertEqual(VoicePermissions.next(mic: .granted, speech: .undetermined, needsSpeech: false), .listen)
        XCTAssertEqual(VoicePermissions.next(mic: .granted, speech: .granted, needsSpeech: true), .listen)
    }

    func testMissingAccessAsksAndDeniedAccessExplains() {
        XCTAssertEqual(VoicePermissions.next(mic: .undetermined, speech: .granted, needsSpeech: true), .ask)
        XCTAssertEqual(VoicePermissions.next(mic: .granted, speech: .undetermined, needsSpeech: true), .ask)
        XCTAssertEqual(VoicePermissions.next(mic: .denied, speech: .granted, needsSpeech: false),
                       .problem(VoicePermissions.micProblem))
        XCTAssertEqual(VoicePermissions.next(mic: .granted, speech: .denied, needsSpeech: true),
                       .problem(VoicePermissions.speechProblem))
        XCTAssertEqual(VoicePermissions.next(mic: .granted, speech: .denied, needsSpeech: false), .listen)
    }
}

final class ListeningTextTests: XCTestCase {
    func testStartingUntilSoundThenListeningUntilWords() {
        XCTAssertEqual(ListeningText.label(heard: "", soundArrived: false), "Starting…")
        XCTAssertEqual(ListeningText.label(heard: "", soundArrived: true), "Listening…")
        XCTAssertEqual(ListeningText.label(heard: "tell", soundArrived: true), "tell")
    }

    func testOnlyADownloadShowsAModelNote() {
        XCTAssertEqual(ListeningText.modelNote(.downloading(0.416)),
                       "Downloading Apple's speech model (42%). Until it's ready, I use the older recognizer.")
        XCTAssertNil(ListeningText.modelNote(.ready))
        XCTAssertNil(ListeningText.modelNote(.checking))
        XCTAssertNil(ListeningText.modelNote(.unavailable))
    }
}
