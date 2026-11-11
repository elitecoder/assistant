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
