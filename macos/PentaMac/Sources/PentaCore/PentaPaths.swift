import CryptoKit
import Foundation

public enum PentaPaths {
    public static func defaultStorageRoot(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> URL {
        if let override = environment["PENTA_DATA_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent(".local", isDirectory: true)
            .appendingPathComponent("share", isDirectory: true)
    }

    public static func databasePath(
        for directory: URL,
        storageRoot: URL? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> URL {
        let resolved = directory.resolvingSymlinksInPath().standardizedFileURL
        let digest = SHA256.hash(data: Data(resolved.path.utf8))
        let hash = digest.map { String(format: "%02x", $0) }.joined()
        let root = storageRoot ?? defaultStorageRoot(environment: environment)
        return root
            .appendingPathComponent("penta", isDirectory: true)
            .appendingPathComponent("chats", isDirectory: true)
            .appendingPathComponent(hash, isDirectory: true)
            .appendingPathComponent("penta.db")
    }
}
