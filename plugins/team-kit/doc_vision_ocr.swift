import Foundation
import Vision
import ImageIO

struct Line: Codable {
    let text: String
    let x: Double
    let y: Double
    let height: Double
}
@main struct OCR {
    static func main() {
        do {
            let data = FileHandle.standardInput.readDataToEndOfFile()
            let paths = try JSONDecoder().decode([String].self, from: data)
            let probe = VNRecognizeTextRequest()
            probe.recognitionLevel = .accurate
            guard try probe.supportedRecognitionLanguages().contains("ko-KR") else {
                throw NSError(domain: "Vision Korean unavailable", code: 1)
            }
            var pages: [[Line]] = []
            for path in paths {
                let lines: [Line] = try autoreleasepool {
                    let request = VNRecognizeTextRequest()
                    request.recognitionLevel = .accurate
                    request.recognitionLanguages = ["ko-KR", "en-US"]
                    request.usesLanguageCorrection = true
                    try VNImageRequestHandler(url: URL(fileURLWithPath: path), options: [:]).perform([request])
                    return (request.results ?? []).compactMap { item in
                        guard let text = item.topCandidates(1).first?.string, !text.isEmpty else { return nil }
                        return Line(text: text, x: item.boundingBox.minX,
                                    y: 1 - item.boundingBox.midY, height: item.boundingBox.height)
                    }
                }
                pages.append(lines)
            }
            FileHandle.standardOutput.write(try JSONEncoder().encode(pages))
        } catch {
            // Never print recognized document text or source paths in an error.
            FileHandle.standardError.write(Data("Vision OCR unavailable\n".utf8))
            exit(1)
        }
    }
}
