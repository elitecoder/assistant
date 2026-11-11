import XCTest
@testable import SessionCore

final class SpokenTests: XCTestCase {
    func testPlainDropsMarkdownAndReadsWorkspaceNumbers() {
        let md = """
        - **Version Bump Conflicts [25]** waits on you.
        See [the PR](https://github.com/x/pull/1) and `gh pr view`.
        ```
        git status
        ```
        """
        XCTAssertEqual(Spoken.plain(md),
                       "Version Bump Conflicts, workspace 25 waits on you. See the PR and gh pr view.")
    }

    func testSpokenQuestionsAskForASpeakableReply() {
        XCTAssertTrue(Asker.prompt(question: "q", fleet: nil, focus: nil, spoken: true).contains("read aloud"))
        XCTAssertFalse(Asker.prompt(question: "q", fleet: nil, focus: nil).contains("read aloud"))
    }
}

final class VoiceProblemTests: XCTestCase {
    func testDictationOffPointsToTheSetting() {
        XCTAssertEqual(Spoken.problem(domain: "kLSRErrorDomain", code: 201, description: "Siri and Dictation are disabled"),
                       "Voice needs Dictation. Turn it on in System Settings, Keyboard, Dictation, then try again.")
    }

    func testOtherFailuresSayWhatHappened() {
        XCTAssertEqual(Spoken.problem(domain: "x", code: 1, description: "No speech detected"),
                       "Voice didn't work: No speech detected")
    }
}

final class MeterTests: XCTestCase {
    func level(_ samples: [Float]) -> Float { samples.withUnsafeBufferPointer { Meter.level($0) } }

    func testSilenceIsZeroAndFullScaleIsOne() {
        XCTAssertEqual(level([0, 0, 0]), 0)
        XCTAssertEqual(level([1, -1, 1, -1]), 1, accuracy: 0.001)
    }

    func testSpeechLevelLandsInTheMiddle() {
        XCTAssertEqual(level([0.03, -0.03, 0.03, -0.03]), 0.45, accuracy: 0.02)
    }

    func testWindowKeepsTheNewestLevels() {
        XCTAssertEqual(Meter.window([0.1, 0.2, 0.3], adding: 0.4, count: 3), [0.2, 0.3, 0.4])
        XCTAssertEqual(Meter.window([], adding: 0.5, count: 3), [0, 0, 0.5])
    }
}

final class MicrophoneChoiceTests: XCTestCase {
    let builtIn = Microphone(id: 1, name: "MacBook Pro Microphone", builtIn: true, virtual: false)
    let brio = Microphone(id: 2, name: "Logitech BRIO", builtIn: false, virtual: false)
    let teams = Microphone(id: 3, name: "Microsoft Teams Audio", builtIn: false, virtual: true)
    let phone = Microphone(id: 4, name: "iPhone Microphone", builtIn: false, virtual: false)

    func testOperatorPickWins() {
        XCTAssertEqual(Microphones.choose([builtIn, brio, phone], preferred: "iPhone Microphone", defaultID: 1,
                                          lidClosed: false), phone)
    }

    func testClosedLidSkipsTheBuiltInDefaultAndVirtualDevices() {
        XCTAssertEqual(Microphones.choose([builtIn, teams, brio], preferred: nil, defaultID: 1, lidClosed: true), brio)
    }

    func testOpenLidKeepsTheSystemDefault() {
        XCTAssertEqual(Microphones.choose([builtIn, brio], preferred: nil, defaultID: 1, lidClosed: false), builtIn)
    }

    func testMissingPickFallsBackToTheDefault() {
        XCTAssertEqual(Microphones.choose([builtIn, brio], preferred: "Gone", defaultID: 2, lidClosed: false), brio)
    }
}
