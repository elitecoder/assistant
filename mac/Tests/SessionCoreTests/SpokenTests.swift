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
