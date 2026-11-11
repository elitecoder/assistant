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
