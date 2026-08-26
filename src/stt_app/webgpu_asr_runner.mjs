import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

let AutoProcessor;
let GraniteSpeechForConditionalGeneration;
let pipeline;

async function loadRuntimeDependencies() {
  const transformers = await import("@huggingface/transformers");
  AutoProcessor = transformers.AutoProcessor;
  GraniteSpeechForConditionalGeneration =
    transformers.GraniteSpeechForConditionalGeneration;
  pipeline = transformers.pipeline;
  transformers.env.allowLocalModels = true;
  transformers.env.allowRemoteModels = false;
  transformers.env.useBrowserCache = false;
  transformers.env.useFSCache = true;
}

const TARGET_SAMPLE_RATE = 16000;
const GRANITE_MAX_CHUNK_SECONDS = 30;
const GRANITE_BOUNDARY_CONTEXT_SECONDS = 5;
const GRANITE_MIN_ENERGY_WINDOW_SAMPLES = 1600;
const MAX_WAV_DATA_BYTES = 512 * 1024 * 1024;
const MAX_WAV_FRAMES = 16000 * 60 * 60 * 8;
const MAX_PROTOCOL_LINE_CHARS = 1024 * 1024;

// Models that load through the high-level Transformers.js
// GraniteSpeechForConditionalGeneration pipeline (q4 packages). Granite 4.1 2B
// shares Granite 4.0's component layout, so it uses the same path.
const GRANITE_PIPELINE_MODELS = new Set([
  "granite-4.0-1b-speech",
  "granite-speech-4.1-2b",
]);

function parseArgs(argv) {
  const args = {
    server: false,
    model: "",
    modelPath: "",
    device: "auto",
    dtype: "q4",
  };
  for (let index = 0; index < argv.length; index += 1) {
    const value = argv[index];
    if (value === "--server") {
      args.server = true;
      continue;
    }
    if (value === "--model") {
      args.model = argv[index + 1] || "";
      index += 1;
      continue;
    }
    if (value === "--model-path") {
      args.modelPath = argv[index + 1] || "";
      index += 1;
      continue;
    }
    if (value === "--device") {
      args.device = argv[index + 1] || "auto";
      index += 1;
      continue;
    }
    if (value === "--dtype") {
      args.dtype = argv[index + 1] || "q4";
      index += 1;
    }
  }
  return args;
}

function writeJson(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function formatError(error) {
  if (error && error.stack) {
    return String(error.stack);
  }
  if (error && error.message) {
    return String(error.message);
  }
  return String(error);
}

function conciseError(error) {
  const firstLine = formatError(error)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find(Boolean);
  return String(firstLine || error || "unknown error").slice(0, 600);
}

export function parseProtocolRequestLine(rawLine) {
  if (rawLine.length > MAX_PROTOCOL_LINE_CHARS) {
    throw new Error("Protocol request line is too large.");
  }
  const line = rawLine.trim();
  if (!line) {
    return null;
  }
  let request;
  try {
    request = JSON.parse(line);
  } catch (error) {
    throw new Error(`Invalid JSON request: ${formatError(error)}`);
  }
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new Error("Protocol request must be a JSON object.");
  }
  return request;
}

function modelPathForTransformers(modelPath) {
  return String(modelPath || "").replaceAll("\\", "/");
}

function readAscii(buffer, offset, length) {
  return buffer.toString("ascii", offset, offset + length);
}

export function findChunk(buffer, chunkId) {
  let offset = 12;
  while (offset + 8 <= buffer.length) {
    const id = readAscii(buffer, offset, 4);
    const size = buffer.readUInt32LE(offset + 4);
    const dataOffset = offset + 8;
    const dataEnd = dataOffset + size;
    if (dataEnd > buffer.length) {
      throw new Error(
        `Invalid WAV file: ${id || "unknown"} chunk exceeds the file bounds.`,
      );
    }
    if (id === chunkId) {
      return { offset: dataOffset, size };
    }
    offset = dataEnd + (size % 2);
    if (offset > buffer.length && dataEnd !== buffer.length) {
      throw new Error(`Invalid WAV file: ${id || "unknown"} chunk padding is truncated.`);
    }
  }
  return null;
}

function decodePcmSample(buffer, byteOffset, bitsPerSample) {
  if (bitsPerSample === 8) {
    return (buffer.readUInt8(byteOffset) - 128) / 128;
  }
  if (bitsPerSample === 16) {
    return Math.max(-1, buffer.readInt16LE(byteOffset) / 32768);
  }
  if (bitsPerSample === 24) {
    const raw = buffer.readUIntLE(byteOffset, 3);
    const signed = raw & 0x800000 ? raw | 0xff000000 : raw;
    return Math.max(-1, signed / 8388608);
  }
  if (bitsPerSample === 32) {
    return Math.max(-1, buffer.readInt32LE(byteOffset) / 2147483648);
  }
  throw new Error(`Unsupported PCM WAV bit depth: ${bitsPerSample}`);
}

function decodeFloatSample(buffer, byteOffset, bitsPerSample) {
  if (bitsPerSample === 32) {
    return buffer.readFloatLE(byteOffset);
  }
  if (bitsPerSample === 64) {
    return buffer.readDoubleLE(byteOffset);
  }
  throw new Error(`Unsupported float WAV bit depth: ${bitsPerSample}`);
}

function resampleLinear(audio, sourceRate, targetRate) {
  if (sourceRate === targetRate) {
    return audio;
  }
  if (sourceRate <= 0 || targetRate <= 0) {
    throw new Error(`Invalid WAV sample rate: ${sourceRate}`);
  }
  const targetLength = Math.max(1, Math.round(audio.length * targetRate / sourceRate));
  const output = new Float32Array(targetLength);
  const ratio = sourceRate / targetRate;
  for (let index = 0; index < targetLength; index += 1) {
    const position = index * ratio;
    const leftIndex = Math.floor(position);
    const rightIndex = Math.min(leftIndex + 1, audio.length - 1);
    const fraction = position - leftIndex;
    output[index] = audio[leftIndex] * (1 - fraction) + audio[rightIndex] * fraction;
  }
  return output;
}

export function decodeWavFile(audioPath, targetSampleRate) {
  const buffer = readFileSync(audioPath);
  if (
    buffer.length < 44 ||
    readAscii(buffer, 0, 4) !== "RIFF" ||
    readAscii(buffer, 8, 4) !== "WAVE"
  ) {
    throw new Error(
      "The ONNX runtime can decode WAV input only. Use a WAV benchmark sample or the app's last recording.",
    );
  }
  const riffEnd = buffer.readUInt32LE(4) + 8;
  if (riffEnd < 12 || riffEnd > buffer.length) {
    throw new Error("Invalid WAV file: RIFF size exceeds the file bounds.");
  }

  const fmt = findChunk(buffer, "fmt ");
  const data = findChunk(buffer, "data");
  if (!fmt || !data) {
    throw new Error("Invalid WAV file: missing fmt or data chunk.");
  }
  if (fmt.offset + fmt.size > riffEnd || data.offset + data.size > riffEnd) {
    throw new Error("Invalid WAV file: chunk exceeds the declared RIFF size.");
  }
  if (fmt.size < 16) {
    throw new Error("Invalid WAV file: fmt chunk is too small.");
  }

  const audioFormat = buffer.readUInt16LE(fmt.offset);
  const channelCount = buffer.readUInt16LE(fmt.offset + 2);
  const sampleRate = buffer.readUInt32LE(fmt.offset + 4);
  const blockAlign = buffer.readUInt16LE(fmt.offset + 12);
  const bitsPerSample = buffer.readUInt16LE(fmt.offset + 14);
  if (channelCount <= 0 || blockAlign <= 0) {
    throw new Error("Invalid WAV file: channel count or block alignment is zero.");
  }
  if (data.size > MAX_WAV_DATA_BYTES) {
    throw new Error("WAV input is too large for the local ONNX runtime.");
  }
  if (!Number.isInteger(blockAlign / channelCount)) {
    throw new Error("Invalid WAV file: block alignment does not match channel count.");
  }
  const bytesPerSample = Math.ceil(bitsPerSample / 8);
  const bytesPerChannel = blockAlign / channelCount;
  if (bytesPerSample <= 0 || bytesPerChannel < bytesPerSample) {
    throw new Error("Invalid WAV file: sample width exceeds block alignment.");
  }
  const frameCount = Math.floor(data.size / blockAlign);
  if (data.size % blockAlign !== 0) {
    throw new Error("Invalid WAV file: data chunk ends with a partial audio frame.");
  }
  const isPcm = audioFormat === 1 || audioFormat === 65534;
  const isFloat = audioFormat === 3;
  if (!isPcm && !isFloat) {
    throw new Error(`Unsupported WAV encoding: ${audioFormat}. Use PCM or float WAV.`);
  }
  const supportedBits = isFloat ? [32, 64] : [8, 16, 24, 32];
  if (!supportedBits.includes(bitsPerSample)) {
    throw new Error(`Unsupported WAV bit depth: ${bitsPerSample}.`);
  }
  if (frameCount <= 0) {
    throw new Error("Invalid WAV file: data chunk contains no complete audio frame.");
  }
  if (frameCount > MAX_WAV_FRAMES) {
    throw new Error("WAV input contains too many audio frames.");
  }
  const mono = new Float32Array(frameCount);

  for (let frame = 0; frame < frameCount; frame += 1) {
    const frameOffset = data.offset + frame * blockAlign;
    let sum = 0;
    for (let channel = 0; channel < channelCount; channel += 1) {
      const sampleOffset = frameOffset + channel * bytesPerChannel;
      const sample = isFloat
        ? decodeFloatSample(buffer, sampleOffset, bitsPerSample)
        : decodePcmSample(buffer, sampleOffset, bitsPerSample);
      sum += Number.isFinite(sample) ? sample : 0;
    }
    mono[frame] = Math.max(-1, Math.min(1, sum / channelCount));
  }

  return resampleLinear(mono, sampleRate, targetSampleRate);
}

function findQuietestSplitPoint(audio, start, end, windowSamples) {
  let bestIndex = start;
  let bestEnergy = Infinity;
  const step = Math.max(1, Math.floor(windowSamples / 2));
  for (let index = start; index < end; index += step) {
    const windowEnd = Math.min(index + windowSamples, end);
    if (windowEnd <= index) {
      break;
    }
    let energy = 0;
    for (let sampleIndex = index; sampleIndex < windowEnd; sampleIndex += 1) {
      const sample = audio[sampleIndex] || 0;
      energy += sample * sample;
    }
    energy /= windowEnd - index;
    if (energy < bestEnergy) {
      bestEnergy = energy;
      bestIndex = index + Math.floor((windowEnd - index) / 2);
    }
  }
  return bestIndex;
}

function splitAudioAtQuietBoundaries(audio, sampleRate, maxChunkSeconds) {
  const maxSamples = Math.max(1, Math.round(maxChunkSeconds * sampleRate));
  if (audio.length <= maxSamples) {
    return [audio];
  }

  const boundaryContextSamples = Math.max(
    1,
    Math.round(GRANITE_BOUNDARY_CONTEXT_SECONDS * sampleRate),
  );
  const chunks = [];
  let offset = 0;
  while (offset < audio.length) {
    const hardEnd = Math.min(offset + maxSamples, audio.length);
    if (hardEnd >= audio.length) {
      chunks.push(audio.slice(offset, audio.length));
      break;
    }

    const searchStart = Math.max(offset + 1, hardEnd - boundaryContextSamples);
    const splitPoint = findQuietestSplitPoint(
      audio,
      searchStart,
      hardEnd,
      GRANITE_MIN_ENERGY_WINDOW_SAMPLES,
    );
    const safeSplitPoint = Math.max(offset + 1, Math.min(splitPoint, audio.length));
    chunks.push(audio.slice(offset, safeSplitPoint));
    offset = safeSplitPoint;
  }
  return chunks;
}

async function hasWebGpuAdapter() {
  const gpu = globalThis.navigator?.gpu;
  if (!gpu) {
    return false;
  }
  try {
    const adapter = await gpu.requestAdapter();
    return Boolean(adapter);
  } catch {
    return false;
  }
}

function resolveDevice(requestedDevice) {
  const requested = String(requestedDevice || "auto").toLowerCase();
  if (requested === "wasm") {
    throw new Error(
      "The Transformers.js Node runtime does not support device \"wasm\". Use \"cpu\" for CPU inference.",
    );
  }

  const gpuDevices = ["webgpu"];
  if (process.platform === "win32") {
    gpuDevices.push("dml");
  }

  if (requested === "gpu") {
    return gpuDevices;
  }

  if (["webgpu", "dml", "cpu"].includes(requested)) {
    return [requested];
  }

  if (requested !== "auto") {
    throw new Error(
      `Unsupported device policy: "${requestedDevice}". Use auto, gpu, webgpu, dml, or cpu.`,
    );
  }

  const devices = [];
  devices.push(...gpuDevices);
  devices.push("cpu");
  return devices;
}

function graniteDtype(dtype) {
  return {
    embed_tokens: dtype,
    audio_encoder: dtype,
    decoder_model_merged: dtype,
  };
}

const GRANITE_LANGUAGE_NAMES = {
  de: "German",
  en: "English",
  es: "Spanish",
  fr: "French",
  ja: "Japanese",
  pt: "Portuguese",
};

function granitePrompt(language) {
  const languageName = GRANITE_LANGUAGE_NAMES[language];
  if (languageName) {
    return `<|audio|>transcribe the ${languageName} speech into a written format.`;
  }
  return "<|audio|>can you transcribe the speech into a written format?";
}

function joinTranscriptChunks(texts) {
  return texts
    .map((text) => String(text || "").trim())
    .filter(Boolean)
    .join(" ")
    .replace(/\s+([,.;:!?])/g, "$1");
}

async function transcribeGraniteChunk(processor, model, audio, language, maxNewTokens) {
  const messages = [
    {
      role: "user",
      content: granitePrompt(language || ""),
    },
  ];
  const prompt = processor.apply_chat_template(messages, {
    add_generation_prompt: false,
    tokenize: false,
  });
  const inputs = await processor(prompt, audio);
  const generatedIds = await model.generate({
    ...inputs,
    max_new_tokens: maxNewTokens || 1024,
  });
  const inputLength = inputs.input_ids.dims.at(-1);
  const generatedTexts = processor.batch_decode(
    generatedIds.slice(null, [inputLength, null]),
    { skip_special_tokens: true },
  );
  return String(generatedTexts?.[0] || "");
}

async function loadRuntimeForDevice(options, device, webgpuAvailable) {
  const modelPath = modelPathForTransformers(options.modelPath);
  const accelerated = ["webgpu", "dml"].includes(device);
  const runtimeWebGpuAvailable = webgpuAvailable || device === "webgpu";

  if (options.model === "cohere-transcribe-03-2026") {
    const transcriber = await pipeline(
      "automatic-speech-recognition",
      modelPath,
      { dtype: options.dtype, device },
    );
    return {
      device,
      gpuAvailable: accelerated,
      webgpuAvailable: runtimeWebGpuAvailable,
      async transcribe(request) {
        const audio = request.audio;
        const result = await transcriber(audio, {
          max_new_tokens: request.maxNewTokens || 1024,
          language: request.language || "en",
        });
        return typeof result === "string" ? result : String(result?.text || "");
      },
    };
  }

  if (GRANITE_PIPELINE_MODELS.has(options.model)) {
    const processor = await AutoProcessor.from_pretrained(modelPath);
    const model = await GraniteSpeechForConditionalGeneration.from_pretrained(
      modelPath,
      { dtype: graniteDtype(options.dtype), device },
    );
    return {
      device,
      gpuAvailable: accelerated,
      webgpuAvailable: runtimeWebGpuAvailable,
      async transcribe(request) {
        const audio = request.audio;
        const audioChunks = splitAudioAtQuietBoundaries(
          audio,
          TARGET_SAMPLE_RATE,
          GRANITE_MAX_CHUNK_SECONDS,
        );
        const chunkTexts = [];
        for (const chunk of audioChunks) {
          chunkTexts.push(
            await transcribeGraniteChunk(
              processor,
              model,
              chunk,
              request.language || "",
              request.maxNewTokens,
            ),
          );
        }
        return joinTranscriptChunks(chunkTexts);
      },
    };
  }

  throw new Error(`Unsupported model: ${options.model}`);
}

async function loadRuntime(options) {
  const webgpuAvailable = await hasWebGpuAdapter();
  const candidateDevices = resolveDevice(options.device);
  const errors = [];
  for (let index = 0; index < candidateDevices.length; index += 1) {
    const device = candidateDevices[index];
    try {
      return {
        runtime: await loadRuntimeForDevice(options, device, webgpuAvailable),
        candidateDevices,
        index,
        webgpuAvailable,
        fallbackErrors: errors,
      };
    } catch (error) {
      errors.push(`${device}: ${conciseError(error)}`);
    }
  }
  throw new Error(
    `Failed to load ${options.model}. Tried devices: ${candidateDevices.join(", ")}\n\n${errors.join("\n\n")}`,
  );
}

async function runServer(options) {
  let runtime;
  let candidateDevices = [];
  let runtimeIndex = -1;
  let webgpuAvailable = false;
  let fallbackErrors = [];
  try {
    await loadRuntimeDependencies();
    const loaded = await loadRuntime(options);
    runtime = loaded.runtime;
    candidateDevices = loaded.candidateDevices;
    runtimeIndex = loaded.index;
    webgpuAvailable = loaded.webgpuAvailable;
    fallbackErrors = loaded.fallbackErrors;
    writeJson({
      type: "ready",
      ok: true,
      model: options.model,
      device: runtime.device,
      gpuAvailable: runtime.gpuAvailable,
      webgpuAvailable: runtime.webgpuAvailable,
      fallbackErrors,
    });
  } catch (error) {
    writeJson({ type: "ready", ok: false, error: formatError(error) });
    process.exitCode = 1;
    return;
  }

  async function transcribeWithFallback(request) {
    const audio = decodeWavFile(request.audioPath, TARGET_SAMPLE_RATE);
    const preparedRequest = { ...request, audio };
    try {
      return {
        text: await runtime.transcribe(preparedRequest),
        fallbackErrors,
      };
    } catch (error) {
      if (!["auto", "gpu"].includes(options.device)) {
        throw error;
      }
      const errors = [`${runtime.device}: ${conciseError(error)}`];
      for (let index = runtimeIndex + 1; index < candidateDevices.length; index += 1) {
        const nextDevice = candidateDevices[index];
        try {
          runtime = await loadRuntimeForDevice(options, nextDevice, webgpuAvailable);
          runtimeIndex = index;
          fallbackErrors = [...fallbackErrors, ...errors];
          return {
            text: await runtime.transcribe(preparedRequest),
            fallbackErrors,
          };
        } catch (nextError) {
          errors.push(`${nextDevice}: ${conciseError(nextError)}`);
        }
      }
      throw new Error(
        `ONNX runtime failed during transcription on all fallback devices.\n\n${errors.join("\n\n")}`,
      );
    }
  }

  // The async iterator provides strict request serialization. A second stdin
  // line cannot enter inference or mutate fallback runtime state until the
  // first request has produced its response.
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const rawLine of lines) {
    let request;
    try {
      request = parseProtocolRequestLine(rawLine);
    } catch (error) {
      writeJson({ ok: false, error: formatError(error) });
      continue;
    }
    if (request === null) {
      continue;
    }

    if (request.command === "shutdown") {
      break;
    }

    if (request.command !== "transcribe") {
      writeJson({
        id: request.id,
        ok: false,
        error: `Unsupported command: ${request.command}`,
      });
      continue;
    }

    try {
      const result = await transcribeWithFallback(request);
      writeJson({
        id: request.id,
        ok: true,
        text: result.text,
        device: runtime.device,
        gpuAvailable: runtime.gpuAvailable,
        webgpuAvailable: runtime.webgpuAvailable,
        fallbackErrors: result.fallbackErrors,
      });
    } catch (error) {
      writeJson({ id: request.id, ok: false, error: formatError(error) });
    }
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.server) {
    writeJson({ ok: false, error: "Only --server mode is supported." });
    process.exitCode = 2;
    return;
  }

  await runServer(args);
}

if (
  !process.execArgv.includes("-e") &&
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  await main();
}
