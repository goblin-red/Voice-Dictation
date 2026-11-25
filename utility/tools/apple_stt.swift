// Распознаватель речи Apple (локально, на этом Маке) для диктовки «Apple» в GOBL(in) Voice.
//
// Запуск: apple-stt ru-RU [nopunct]   (nopunct — без расстановки знаков препинания)
// Вход (stdin) — кадры: 1 байт вида + 4 байта длины (big-endian) + данные:
//   S <id>    — новая фраза
//   A <pcm>   — звук фразы: int16, 16 кГц, моно
//   E <id>    — фраза кончилась, нужен окончательный текст
//   C <слова> — подсказка: слова команд через перевод строки (распознаватель их «знает»)
// Выход (stdout) — по JSON на строку:
//   {"ready": true}                                — готов
//   {"id": "...", "final": false, "text": "..."}   — текст по ходу речи
//   {"id": "...", "final": true,  "text": "..."}   — окончательный текст фразы
//   {"error": "..."}                               — не работает (нет разрешения / языка)
//
// Сборка: swiftc -O tools/apple_stt.swift -o lib/bin/apple-stt

import AVFoundation
import Foundation
import Speech

setvbuf(stdout, nil, _IOLBF, 0)

let output = NSLock()

func emit(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj),
          let line = String(data: data, encoding: .utf8) else { return }
    output.lock()
    print(line)
    output.unlock()
}

// ---------- разрешение и язык

let localeId = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "en-US"
let punctuation = !CommandLine.arguments.contains("nopunct")

let authWait = DispatchSemaphore(value: 0)
var auth = SFSpeechRecognizerAuthorizationStatus.notDetermined
SFSpeechRecognizer.requestAuthorization { status in
    auth = status
    authWait.signal()
}
authWait.wait()
guard auth == .authorized else {
    emit(["error": "no_permission", "status": auth.rawValue])
    exit(2)
}

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeId)),
      recognizer.supportsOnDeviceRecognition else {
    emit(["error": "no_language", "locale": localeId])
    exit(3)
}
recognizer.queue = OperationQueue()      // ответы — не в главном потоке (он читает stdin)

let format = AVAudioFormat(standardFormatWithSampleRate: 16000, channels: 1)!

// ---------- фразы

final class Phrase {
    let request = SFSpeechAudioBufferRecognitionRequest()
    var task: SFSpeechRecognitionTask?
}

var phrases: [String: Phrase] = [:]
var hints: [String] = []                 // слова команд: contextualStrings каждой фразы
var current: Phrase?
let state = NSLock()

func start(_ id: String) {
    let phrase = Phrase()
    phrase.request.shouldReportPartialResults = true
    phrase.request.requiresOnDeviceRecognition = true
    phrase.request.taskHint = .dictation
    phrase.request.contextualStrings = hints
    if #available(macOS 13, *) {
        phrase.request.addsPunctuation = punctuation
    }
    phrase.task = recognizer.recognitionTask(with: phrase.request) { result, error in
        if let result = result {
            emit(["id": id, "final": result.isFinal, "text": result.bestTranscription.formattedString])
            if result.isFinal {
                state.lock(); phrases[id] = nil; state.unlock()
            }
        } else if error != nil {
            // «речи не было» и прочее — пустой окончательный текст
            emit(["id": id, "final": true, "text": "", "note": error!.localizedDescription])
            state.lock(); phrases[id] = nil; state.unlock()
        }
    }
    state.lock()
    phrases[id] = phrase
    current = phrase
    state.unlock()
}

func append(_ data: Data) {
    state.lock(); let phrase = current; state.unlock()
    guard let phrase = phrase, data.count >= 2 else { return }
    let frames = data.count / 2
    guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else { return }
    buffer.frameLength = AVAudioFrameCount(frames)
    let out = buffer.floatChannelData![0]
    data.withUnsafeBytes { raw in
        let samples = raw.bindMemory(to: Int16.self)
        for i in 0..<frames {
            out[i] = Float(Int16(littleEndian: samples[i])) / 32768
        }
    }
    phrase.request.append(buffer)
}

func end(_ id: String) {
    state.lock()
    let phrase = phrases[id]
    if current === phrase { current = nil }
    state.unlock()
    phrase?.request.endAudio()
}

// ---------- чтение команд

let input = FileHandle.standardInput

func readExact(_ count: Int) -> Data? {
    var data = Data()
    while data.count < count {
        let chunk = input.readData(ofLength: count - data.count)
        if chunk.isEmpty { return nil }     // Voice закрылся
        data.append(chunk)
    }
    return data
}

emit(["ready": true, "locale": localeId])

while let head = readExact(5) {
    let length = head[1...4].reduce(0) { ($0 << 8) | Int($1) }
    guard let payload = readExact(length) else { break }
    switch head[0] {
    case UInt8(ascii: "S"):
        start(String(decoding: payload, as: UTF8.self))
    case UInt8(ascii: "A"):
        append(payload)
    case UInt8(ascii: "E"):
        end(String(decoding: payload, as: UTF8.self))
    case UInt8(ascii: "C"):
        hints = String(decoding: payload, as: UTF8.self).split(separator: "\n").map(String.init)
    default:
        break
    }
}
exit(0)
