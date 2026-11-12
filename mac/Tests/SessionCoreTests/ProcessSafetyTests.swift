import XCTest
@testable import SessionCore

final class ProcessSafetyTests: XCTestCase {
    var folder: URL!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appendingPathComponent("process-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: folder) }

    func script(_ name: String, _ body: String) throws -> String {
        let url = folder.appendingPathComponent(name)
        try ("#!/bin/sh\n" + body + "\n").write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        return url.path
    }

    func invocation(_ input: Data) -> ClaudeInvocation {
        ClaudeInvocation(systemPrompt: "", input: input, model: "haiku", effort: nil)
    }

    /// `claude` exiting before it reads a large transcript used to kill the app with SIGPIPE.
    func testAClaudeThatExitsEarlyWithALargeInputDoesNotKillTheApp() {
        let run = Asker.run(invocation(Data(repeating: 0x61, count: 1 << 20)), tool: "/A/AssistantBar",
                            binary: "/usr/bin/true", children: ChildProcesses(), onLine: { _ in })
        XCTAssertNil(run.launchError)
        XCTAssertFalse(run.stopped)
        XCTAssertNil(run.crashSignal)
    }

    func testWritingToAChildThatStoppedReadingFailsInsteadOfRaising() throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/true")
        let input = Pipe()
        p.standardInput = input
        try p.run()
        p.waitUntilExit()
        XCTAssertFalse(ChildInput.feed(input.fileHandleForWriting, Data(repeating: 0, count: 1 << 20)))
    }

    /// The turn's record survives the early exit, including a `--send` the run had already delivered.
    func testTheRecordOfASendIsKeptWhenClaudeQuitsWithoutReadingItsInput() throws {
        let call = #"{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"/A/AssistantBar --send workspace:3 \"go\""}}]}}"#
        let reply = #"{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"REPLY\npushed"}]}}"#
        let fake = try script("claude", "printf '%s\\n' '\(call)' '\(reply)'\nexit 0")
        let store = try ConversationStore(url: folder.appendingPathComponent("conversations.db"))
        let engine = ChatEngine(store: store, memoryFolder: folder.appendingPathComponent("memory"), tool: "/A/AssistantBar",
                                model: "haiku", effort: nil, windowFor: { _ in 1_000_000 },
                                runner: { invocation, onLine in
                                    Asker.run(invocation, tool: "/A/AssistantBar", binary: fake, children: ChildProcesses(), onLine: onLine)
                                }, summarizer: { _ in "## Goal\nx" })
        let outcome = engine.ask(String(repeating: "long question ", count: 80_000), fleet: ScanResult()) { _ in }
        XCTAssertTrue(outcome.failed)
        let rows = try store.messages(sessionID: try XCTUnwrap(store.sessions().first?.session.id))
        XCTAssertTrue(rows.contains { $0.content.contains("--send workspace:3") }, "the delivered send is in the record")
        XCTAssertTrue(rows.contains { $0.role == .tool && $0.content.hasPrefix("REPLY") })
    }

    /// A crash is told apart from an operator's Stop.
    func testACrashIsNotRecordedAsAStop() throws {
        let crasher = try script("crash", "kill -SEGV $$")
        let crash = Asker.run(invocation(Data()), tool: "/A/AssistantBar", binary: crasher, children: ChildProcesses(), onLine: { _ in })
        XCTAssertEqual(crash.crashSignal, SIGSEGV)
        XCTAssertFalse(crash.stopped)

        let children = ChildProcesses()
        let sleeper = try script("sleeper", "sleep 30")
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.5) { children.endAll() }
        let stopped = Asker.run(invocation(Data()), tool: "/A/AssistantBar", binary: sleeper, children: children, onLine: { _ in })
        XCTAssertTrue(stopped.stopped)
        XCTAssertNil(stopped.crashSignal)
    }

    /// Stop and Quit end the processes the app started and everything below them, like `--send` under `claude`.
    func testEndingAllAlsoEndsGrandchildren() throws {
        let children = ChildProcesses()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "sleep 30 & sleep 31 & wait"]
        try p.run()
        children.add(p)
        Thread.sleep(forTimeInterval: 0.3)
        let below = ProcessTree.descendants(of: p.processIdentifier, parents: ProcessTree.current())
        XCTAssertEqual(below.count, 2)
        children.endAll()
        p.waitUntilExit()
        Thread.sleep(forTimeInterval: 0.3)
        for pid in below { XCTAssertNotEqual(kill(pid, 0), 0, "pid \(pid) is still running") }
    }

    func testDescendantsWalkTheWholeTree() {
        let parents: [Int32: Int32] = [10: 1, 11: 10, 12: 11, 13: 10, 20: 1, 1: 1]
        XCTAssertEqual(Set(ProcessTree.descendants(of: 10, parents: parents)), [11, 12, 13])
        XCTAssertEqual(ProcessTree.parse("  10     1\n 11 10\nbad\n"), [10: 1, 11: 10])
    }

    func testAHungCommandTimesOut() {
        let started = Date()
        let (status, _) = Shell.run("/bin/sleep", ["5"], timeout: 0.3)
        XCTAssertEqual(status, -1)
        XCTAssertLessThan(Date().timeIntervalSince(started), 2)
    }
}

final class LateChildTests: XCTestCase {
    func sleeper() throws -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sleep")
        p.arguments = ["30"]
        try p.run()
        return p
    }

    /// A child that shows up while the tree is being frozen is frozen and ended too.
    func testAChildStartedDuringTheFreezeIsEnded() throws {
        let root = try sleeper(), late = try sleeper()
        defer { root.terminate(); late.terminate() }
        let children = ChildProcesses()
        children.add(root)
        var looks = 0
        children.endAll {
            looks += 1
            return looks == 1 ? [late.processIdentifier: root.processIdentifier] : [:]
        }
        late.waitUntilExit()
        XCTAssertEqual(late.terminationReason, .uncaughtSignal)
    }

    /// One started after the freeze, just as SIGTERM goes out, is caught by the last look.
    func testAChildStartedAfterTheFreezeIsEndedByTheLastLook() throws {
        let root = try sleeper(), late = try sleeper()
        defer { root.terminate(); late.terminate() }
        let children = ChildProcesses()
        children.add(root)
        var looks = 0
        children.endAll {
            looks += 1
            return looks >= 2 ? [late.processIdentifier: root.processIdentifier] : [:]
        }
        late.waitUntilExit()
        XCTAssertEqual(late.terminationReason, .uncaughtSignal)
        root.waitUntilExit()
    }

    /// A child that handles SIGTERM (to save its state) gets to run its handler: it's let go after the freeze.
    func testAFrozenProcessRunsItsTermHandler() throws {
        let marker = FileManager.default.temporaryDirectory.appendingPathComponent("term-\(UUID().uuidString)").path
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", "trap 'echo handled > \(marker); exit 0' TERM; while true; do sleep 0.1; done"]
        try p.run()
        Thread.sleep(forTimeInterval: 0.2)
        let children = ChildProcesses()
        children.add(p)
        children.endAll { [:] }
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        XCTAssertTrue(FileManager.default.fileExists(atPath: marker), "the TERM handler never ran")
    }
}
