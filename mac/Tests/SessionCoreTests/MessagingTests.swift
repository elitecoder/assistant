import Foundation
import XCTest
@testable import SessionCore

final class MessagingTests: XCTestCase {
    func testTerminalsComeWithTheirWorkspaceAndTTY() {
        let tree = Data("""
        {"windows":[{"ref":"window:1","workspaces":[{"ref":"workspace:7","title":"Fix CI [7]","panes":[
          {"ref":"pane:1","surfaces":[{"ref":"surface:11","type":"terminal","tty":"ttys015"},
                                      {"ref":"surface:12","type":"browser"}]}]}]}]}
        """.utf8)
        XCTAssertEqual(Messaging.terminals(tree),
                       [Terminal(workspace: "workspace:7", title: "Fix CI [7]", surface: "surface:11", tty: "ttys015")])
    }

    func testClaudeSessionsAreReadFromTheirCommandLines() {
        let ps = """
        65603 ttys037  /Users/m/.local/bin/claude --session-id ef29cafb-042f-46ab-8a6d-a78e42d82264 --settings x
          812 ttys002  /Users/m/.local/bin/claude --resume 91c7c0d4-fb81-4634-91de-8b8dcfe652a9 --model opus
        19895 ??       /Users/m/.local/bin/claude daemon run --session-id 11111111-1111-1111-1111-111111111111
          900 ttys009  -zsh
        """
        XCTAssertEqual(Messaging.claudeSessions(ps), [
            "ttys037": .init(id: "ef29cafb-042f-46ab-8a6d-a78e42d82264", pid: 65603),
            "ttys002": .init(id: "91c7c0d4-fb81-4634-91de-8b8dcfe652a9", pid: 812),
        ])
    }

    func testProjectFolderMatchesClaudesNaming() {
        XCTAssertEqual(Messaging.projectFolder("/Users/mukuls/dev/firefly-platform"), "-Users-mukuls-dev-firefly-platform")
        XCTAssertEqual(Messaging.projectFolder("/private/tmp/claude-501/-Users-x"), "-private-tmp-claude-501--Users-x")
        XCTAssertEqual(Messaging.projectFolder("/Users/m/dev/a_b.c"), "-Users-m-dev-a-b-c")
    }

    func testTurnIsDeliveredThenFinishedOnlyAfterTheTurnEnds() {
        let since = Timestamps.parse("2026-11-11T10:00:00.000Z")!
        let before = transcript([user("go", at: "2026-11-11T09:00:00.000Z"), assistant("old", at: "2026-11-11T09:00:05.000Z")])
        XCTAssertFalse(Messaging.turn(before, sent: "go", since: since).delivered)
        let working = transcript([user("go", at: "2026-11-11T10:00:01.000Z"),
                                  assistant("Shipping now.", at: "2026-11-11T10:00:03.000Z")])
        XCTAssertEqual(Messaging.turn(working, sent: "go", since: since),
                       TurnState(delivered: true, finished: false, replies: ["Shipping now."]))
        let done = transcript([user("go", at: "2026-11-11T10:00:01.000Z"),
                               assistant("Shipping now.", at: "2026-11-11T10:00:03.000Z"),
                               assistant("PR #56 is up. Merge it?", at: "2026-11-11T10:04:00.000Z"),
                               line(["type": "system", "subtype": "turn_duration", "timestamp": "2026-11-11T10:04:01.000Z"])])
        XCTAssertEqual(Messaging.turn(done, sent: "go", since: since).finished, true)
        XCTAssertEqual(Messaging.turn(done, sent: "go", since: since).replies.last, "PR #56 is up. Merge it?")
    }

    func testStepsShowWhatWasSentAndWhatCameBack() {
        let tool = "/Apps/AssistantBar"
        let sent = line(["type": "assistant", "message": ["content": [["type": "tool_use", "name": "Bash",
                    "input": ["command": "\(tool) --send workspace:7 \"Use my token, then push.\""]]]]])
        XCTAssertEqual(Messaging.step(sent, sendCommand: tool), .sent(workspace: "workspace:7", message: "Use my token, then push."))
        let reply = line(["type": "user", "message": ["content": [["type": "tool_result", "content": "REPLY\nPushed."]]]])
        XCTAssertEqual(Messaging.step(reply, sendCommand: tool), .replied("Pushed."))
        let other = line(["type": "assistant", "message": ["content": [["type": "tool_use", "name": "Bash",
                     "input": ["command": "ls"]]]]])
        XCTAssertNil(Messaging.step(other, sendCommand: tool))
    }

    func testShellWordsHonorQuotes() {
        XCTAssertEqual(Messaging.shellWords(#"a --send ws:1 "it's \"done\"" 'x y'"#), ["a", "--send", "ws:1", #"it's "done""#, "x y"])
    }
}

final class ShellWordsTabTests: XCTestCase {
    func testTabsSeparateWordsLikeTheShell() {
        XCTAssertEqual(Messaging.shellWords("tool\t--wait\tworkspace:1"), ["tool", "--wait", "workspace:1"])
        XCTAssertEqual(Messaging.shellWords("tool --send ws:1 'a\tb'"), ["tool", "--send", "ws:1", "a\tb"])
    }
}
