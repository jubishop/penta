import Foundation

public final class ClaudeService: CLIAgentService, @unchecked Sendable {
    private let hookSettings: String?
    private var seenSessionID = false
    private var hasEmittedText = false

    public init(executable: String?, model: String? = nil, hookSettings: String? = nil) {
        self.hookSettings = hookSettings
        super.init(agentName: "Claude", executable: executable, model: model)
    }

    public override func resetParseState() {
        seenSessionID = false
        hasEmittedText = false
    }

    public override func buildArguments(
        prompt: String,
        sessionID: String?,
        systemPrompt: String?
    ) -> [String] {
        var args = [
            "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--include-partial-messages"
        ]

        if let hookSettings {
            args += ["--settings", hookSettings]
        } else {
            args.append("--dangerously-skip-permissions")
        }

        if let model {
            args += ["--model", model]
        }
        if let systemPrompt {
            args += ["--append-system-prompt", systemPrompt]
        }
        if let sessionID {
            args += ["--resume", sessionID]
        }
        args.append(prompt)
        return args
    }

    public override func parseLine(_ data: [String: Any]) -> [StreamEvent] {
        let messageType = data["type"] as? String
        var events: [StreamEvent] = []

        if messageType == "system" {
            let subtype = data["subtype"] as? String
            if subtype == "init", let sessionID = data["session_id"] as? String {
                seenSessionID = true
                events.append(StreamEvent(kind: .sessionStarted, sessionID: sessionID))
            } else if subtype == "api_retry" {
                let attempt = data["attempt"].map(String.init(describing:)) ?? "?"
                events.append(StreamEvent(kind: .warning, error: "Retrying (attempt \(attempt))..."))
            }
        } else if messageType == "stream_event",
                  let event = data["event"] as? [String: Any] {
            let eventType = event["type"] as? String
            if eventType == "content_block_start",
               let block = event["content_block"] as? [String: Any] {
                let blockType = block["type"] as? String
                if blockType == "tool_use" {
                    events.append(StreamEvent(
                        kind: .toolUseStarted,
                        toolID: block["id"] as? String,
                        toolName: block["name"] as? String
                    ))
                } else if hasEmittedText {
                    events.append(StreamEvent(kind: .textDelta, text: "\n\n"))
                }
            } else if eventType == "content_block_delta",
                      let delta = event["delta"] as? [String: Any] {
                let deltaType = delta["type"] as? String
                if deltaType == "text_delta", let text = delta["text"] as? String, !text.isEmpty {
                    hasEmittedText = true
                    events.append(StreamEvent(kind: .textDelta, text: text))
                } else if deltaType == "thinking_delta",
                          let thinking = delta["thinking"] as? String,
                          !thinking.isEmpty {
                    events.append(StreamEvent(kind: .thinking, text: thinking))
                }
            }
        } else if messageType == "rate_limit_event" {
            let status = data["status"] as? String ?? ""
            if status == "warning" || status == "rejected" {
                events.append(StreamEvent(kind: .warning, error: "Rate limited (\(status))"))
            }
        } else if messageType == "result" {
            let resultText = data["result"] as? String ?? ""
            if (data["is_error"] as? Bool) == true {
                events.append(StreamEvent(kind: .error, error: resultText))
            } else if !resultText.isEmpty {
                events.append(StreamEvent(kind: .textComplete, text: resultText))
            }

            if let sessionID = data["session_id"] as? String, !seenSessionID {
                seenSessionID = true
                events.append(StreamEvent(kind: .sessionStarted, sessionID: sessionID))
            }

            if let cost = data["cost_usd"] ?? data["total_cost_usd"] {
                events.append(StreamEvent(kind: .usage, usageDescription: "cost_usd=\(cost)"))
            }
        }

        return events
    }
}
