import Foundation
import XCTest
@testable import SessionCore

/// The chat's PreToolUse guard and Assistant's command line, which together keep the chat's Claude to its own commands.
final class ToolGuardTests: XCTestCase {
    let tool = "/Apps/Assistant.app/Contents/MacOS/AssistantBar"
    var root: String!

    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("guard-\(UUID().uuidString)/projects").path
        try FileManager.default.createDirectory(atPath: root + "/-Users-x-dev", withIntermediateDirectories: true)
    }

    func bash(_ command: String) -> ToolGuard.Decision {
        ToolGuard.decide(toolName: "Bash", input: ["command": command], cwd: root, tool: tool, root: root)
    }

    func testAssistantsOwnCommandsPassInTheirExactShapes() {
        for command in ["\(tool) --sessions", "\(tool) --send workspace:25 \"use my token, then report\"",
                        "\(tool) --send workspace:25 'it\"s fine'", "\(tool) --send workspace:25 \"say \\\"hi\\\"\"",
                        "\(tool) --wait workspace:25", "\(tool) --search '{\"query\": \"deploy token\", \"sort\": \"newest\"}'",
                        "\(tool) --memory '{\"target\": \"user\", \"operations\": [{\"action\": \"add\", \"content\": \"x\"}]}'",
                        "  \(tool) --sessions  "] {
            XCTAssertEqual(bash(command), .allow, command)
        }
    }

    func testEverythingElseInBashIsDenied() {
        let attacks = [
            "tail -n 5 /etc/hosts", "cat ~/dev/generated-docs/notes.md", "ls /tmp", "head ~/.claude/settings.json",
            "launchctl list", "launchctl print gui/501", "cmux list-pane-surfaces", "gh pr merge 1", "git push origin main",
            "\(tool) --search '{}' --render hello /tmp/victim", "\(tool) --sessions; rm -rf /tmp/x", "\(tool) --sessions && ls",
            "\(tool) --sessions | tee /tmp/x", "\(tool) --sessions > /tmp/x", "\(tool) --wait $(whoami)", "\(tool) --wait `whoami`",
            "\(tool) --send workspace:1 \"$(cat ~/.ssh/id_rsa)\"", "\(tool) --send workspace:1 \"x\"\n\(tool) --sessions",
            "\(tool) --wait workspace:*", "\(tool) --send --render x", "\(tool) --wait workspace:1 --timeout 1",
            "\(tool) --search 'not json'", "\(tool) --search '[1]'", "\(tool) --render hi /tmp/v", "\(tool) --print",
            "\(tool) --pretooluse-guard", "/other/AssistantBar --sessions", "\(tool)", "\(tool) --sessions # comment",
            "\(tool) --sessions &", "\(tool) --send workspace:1 'unterminated", "\(tool) --sessions\\;", "~/x --sessions",
            "FOO=1 \(tool) --sessions", "\(tool) --memory '{}' extra", "\(tool) --send workspace:1 \"a\" \"b\"",
        ]
        for command in attacks {
            XCTAssertEqual(bash(command), .deny(ToolGuard.onlyOwnCommands), command)
        }
    }

    func testReadingIsLimitedToTheTranscriptsFolder() throws {
        let inside = root + "/-Users-x-dev/abc.jsonl"
        XCTAssertEqual(ToolGuard.decide(toolName: "Read", input: ["file_path": inside], cwd: root, tool: tool, root: root), .allow)
        XCTAssertEqual(ToolGuard.decide(toolName: "Grep", input: ["pattern": "x"], cwd: root, tool: tool, root: root), .allow)
        XCTAssertEqual(ToolGuard.decide(toolName: "Grep", input: ["pattern": "x", "path": "-Users-x-dev", "glob": "*.jsonl"],
                                        cwd: root, tool: tool, root: root), .allow)
        XCTAssertEqual(ToolGuard.decide(toolName: "Glob", input: ["pattern": "**/*.jsonl"], cwd: root, tool: tool, root: root), .allow)
        let link = root + "/-Users-x-dev/escape"
        try FileManager.default.createSymbolicLink(atPath: link, withDestinationPath: NSHomeDirectory() + "/dev")
        let outside: [(String, [String: Any])] = [
            ("Read", ["file_path": NSHomeDirectory() + "/dev/notes.md"]), ("Read", ["file_path": root + "/../settings.json"]),
            ("Read", ["file_path": "~/.claude/settings.json"]), ("Read", ["file_path": link + "/secret"]), ("Read", [:]),
            ("Grep", ["pattern": "x", "path": "/tmp"]), ("Grep", ["pattern": "x", "glob": "../../*"]),
            ("Glob", ["pattern": "/Users/**"]), ("Glob", ["pattern": "../**"]), ("Glob", ["pattern": "*", "path": NSHomeDirectory()]),
        ]
        for (name, input) in outside {
            XCTAssertNotEqual(ToolGuard.decide(toolName: name, input: input, cwd: root, tool: tool, root: root), .allow, "\(name) \(input)")
        }
        for name in ["Edit", "Write", "WebFetch", "Task", "NotebookEdit", "mcp__x__y"] {
            XCTAssertNotEqual(ToolGuard.decide(toolName: name, input: [:], cwd: root, tool: tool, root: root), .allow, name)
        }
    }

    func testTheHookSpeaksClaudeCodesFormat() throws {
        let input = Data(#"{"tool_name":"Bash","tool_input":{"command":"ls /tmp"},"cwd":"/x"}"#.utf8)
        let output = ToolGuard.hookOutput(ToolGuard.decide(hookInput: input, tool: tool, root: root))
        let decoded = try JSONSerialization.jsonObject(with: Data(output.utf8)) as? [String: Any]
        let specific = decoded?["hookSpecificOutput"] as? [String: Any]
        XCTAssertEqual(specific?["hookEventName"] as? String, "PreToolUse")
        XCTAssertEqual(specific?["permissionDecision"] as? String, "deny")
        XCTAssertEqual(ToolGuard.hookOutput(.allow), "", "an allowed call still goes through the normal permission check")
        XCTAssertEqual(ToolGuard.decide(hookInput: Data("junk".utf8), tool: tool), .deny("unreadable tool call"))
    }
}

final class AppCommandTests: XCTestCase {
    let a = "/A/AssistantBar"

    func testEachCommandIsRecognizedOnlyFirstWithItsExactArguments() {
        XCTAssertEqual(AppCommand.parse([a]), .app)
        XCTAssertEqual(AppCommand.parse([a, "--show", "--listen"]), .app)
        XCTAssertEqual(AppCommand.parse([a, "--show", "--talk"]), .app)
        XCTAssertEqual(AppCommand.parse([a, "--sessions"]), .sessions)
        XCTAssertEqual(AppCommand.parse([a, "--send", "workspace:1", "go"]), .send(ref: "workspace:1", message: "go", timeout: nil))
        XCTAssertEqual(AppCommand.parse([a, "--send", "workspace:1", "go", "--timeout", "60"]), .send(ref: "workspace:1", message: "go", timeout: 60))
        XCTAssertEqual(AppCommand.parse([a, "--wait", "workspace:1"]), .wait(ref: "workspace:1", timeout: nil))
        XCTAssertEqual(AppCommand.parse([a, "--search", "{}"]), .search("{}"))
        XCTAssertEqual(AppCommand.parse([a, "--memory", "{}"]), .memory("{}"))
        XCTAssertEqual(AppCommand.parse([a, "--render", "hi", "/tmp/a.wav"]), .render(text: "hi", file: "/tmp/a.wav"))
        XCTAssertEqual(AppCommand.parse([a, "--ask", "q", "--spoken", "--session", "work"]), .ask(question: "q", session: "work", spoken: true, sayTo: nil))
        XCTAssertEqual(AppCommand.parse([a, "--ask", "q", "--spoken", "--say-to", "/tmp/a.wav"]),
                       .ask(question: "q", session: nil, spoken: true, sayTo: "/tmp/a.wav"))
        XCTAssertEqual(AppCommand.parse([a, "--compress", "--focus", "token"]), .compress(session: nil, focus: "token"))
        XCTAssertEqual(AppCommand.parse([a, "--pretooluse-guard"]), .toolGuard)
        XCTAssertEqual(AppCommand.parse([a, "--transcribe", "/tmp/q.wav"]), .transcribe(file: "/tmp/q.wav", legacy: false))
        XCTAssertEqual(AppCommand.parse([a, "--transcribe", "/tmp/q.wav", "--legacy"]), .transcribe(file: "/tmp/q.wav", legacy: true))
    }

    func testASmuggledSecondCommandIsRejected() {
        for args in [[a, "--search", "{}", "--render", "hello", "/tmp/victim"], [a, "--memory", "{}", "--send", "w", "m"],
                     [a, "--sessions", "--render", "x", "/tmp/v"], [a, "--send", "w", "m", "--render", "x"],
                     [a, "--wait", "w", "--timeout"], [a, "--ask", "q", "--session"], [a, "--ask", "q", "--spoken", "--spoken"],
                     [a, "--render", "x"], [a, "--print", "--render", "x", "y"], [a, "--show", "--render", "x", "y"],
                     [a, "hello", "--sessions"], [a, "--search"], [a, "--compress", "--render", "x"],
                     [a, "--transcribe"], [a, "--transcribe", "q.wav", "--render", "x", "y"],
                     [a, "--transcribe", "q.wav", "--legacy", "--legacy"]] {
            XCTAssertNil(AppCommand.parse(args), "\(args)")
        }
    }
}
