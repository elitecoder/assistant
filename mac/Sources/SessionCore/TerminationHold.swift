import Darwin
import Foundation

/// Set by the signal handler while termination is held.
private var terminationAsked: sig_atomic_t = 0

/// Holds off Stop, Quit, and talking over the answer while `--send` pastes a message and presses Enter, so the two
/// happen together: a message is never left pasted as a draft without its Enter. SIGTERM, SIGINT, and SIGHUP are
/// caught in this process and blocked in the `cmux` calls it makes meanwhile; once both steps are done, the caller
/// exits if one arrived.
public enum TerminationHold {
    static let signals = [SIGTERM, SIGINT, SIGHUP]

    /// Runs `body` with termination held; returns its result and whether termination was asked for meanwhile.
    public static func run<T>(_ body: () -> T) -> (value: T, asked: Bool) {
        terminationAsked = 0
        var previous: [sigaction] = []
        for signal in signals {
            var action = sigaction()
            action.__sigaction_u.__sa_handler = { _ in terminationAsked = 1 }
            sigemptyset(&action.sa_mask)
            var old = sigaction()
            sigaction(signal, &action, &old)
            previous.append(old)
        }
        let value = body()
        for (signal, old) in zip(signals, previous) {
            var old = old
            sigaction(signal, &old, nil)
        }
        return (value, terminationAsked != 0)
    }

    /// `--send`'s delivery: pastes with `cmux`, then presses Enter if the paste went in, all with termination held.
    public static func pasteAndEnter(_ cmux: String, paste: [String], enter: [String]) -> (paste: (Int32, Data), interrupted: Bool) {
        let (pasted, asked) = run { () -> (Int32, Data) in
            let pasted = command(cmux, paste)
            if pasted.0 == 0 { _ = command(cmux, enter) }
            return pasted
        }
        return (pasted, asked)
    }

    /// Runs `tool` with termination signals blocked, so ending the run can't stop it halfway; it still exits when its
    /// work is done. A run that hangs past `timeout` is killed and reports status -1.
    public static func command(_ tool: String, _ args: [String], timeout: TimeInterval = 30) -> (Int32, Data) {
        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else { return (-1, Data()) }
        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_adddup2(&actions, fds[1], STDOUT_FILENO)
        posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0)
        posix_spawn_file_actions_addclose(&actions, fds[0])
        posix_spawn_file_actions_addclose(&actions, fds[1])
        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        var blocked = sigset_t()
        sigemptyset(&blocked)
        signals.forEach { sigaddset(&blocked, $0) }
        var defaults = sigset_t()
        sigfillset(&defaults)
        signals.forEach { sigdelset(&defaults, $0) }
        posix_spawnattr_setsigmask(&attributes, &blocked)
        posix_spawnattr_setsigdefault(&attributes, &defaults)
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF))
        var environment = ProcessInfo.processInfo.environment
        environment["CMUX_QUIET"] = "1"
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        let argv = ([tool] + args).map { strdup($0) } + [nil]
        let envp = environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { (argv + envp).forEach { free($0) } }
        var pid: pid_t = 0
        let spawned = posix_spawn(&pid, tool, &actions, &attributes, argv, envp)
        close(fds[1])
        guard spawned == 0 else {
            close(fds[0])
            return (-1, Data())
        }
        let killer = DispatchWorkItem { kill(pid, SIGKILL) }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
        let data = FileHandle(fileDescriptor: fds[0], closeOnDealloc: true).readDataToEndOfFile()
        var status: Int32 = 0
        while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
        killer.cancel()
        let signaled = status & 0x7f != 0
        return (signaled ? -1 : (status >> 8) & 0xff, data)
    }
}
